"""SQLite 存储层。

版本一致性设计（对应"上传、发布和查询交错时不得混用两版"）：
  * 全库只有一条 meta 记录保存 active_version_id；
  * 发布 = 单个事务内翻转 active_version_id 并更新版本状态，失败整体回滚，旧版继续供数；
  * 查询 = 单个只读事务内先读 active_version_id、再读该版本的数据，
    同一事务的快照保证一次查询只看到一个版本；
  * 进程内用一把 RLock 串行化所有事务（单写者模型），
    因此上传暂存、发布、查询无论怎样交错都不会出现跨版本混读。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from .changes import DepartureRecord, decode_cursor, diff_departures, encode_cursor
from .csv_import import ParsedData

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active_version_id INTEGER
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL CHECK (status IN ('staged', 'active', 'superseded', 'invalid')),
    created_at TEXT NOT NULL,
    published_at TEXT,
    publish_seq INTEGER,
    stops_count INTEGER NOT NULL DEFAULT 0,
    trips_count INTEGER NOT NULL DEFAULT 0,
    stop_times_count INTEGER NOT NULL DEFAULT 0,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS stops (
    version_id INTEGER NOT NULL REFERENCES versions (id),
    stop_id TEXT NOT NULL,
    stop_name TEXT NOT NULL,
    PRIMARY KEY (version_id, stop_id)
);
CREATE TABLE IF NOT EXISTS trips (
    version_id INTEGER NOT NULL REFERENCES versions (id),
    trip_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    PRIMARY KEY (version_id, trip_id)
);
CREATE TABLE IF NOT EXISTS stop_times (
    version_id INTEGER NOT NULL REFERENCES versions (id),
    trip_id TEXT NOT NULL,
    stop_id TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    departure_minutes INTEGER NOT NULL CHECK (departure_minutes BETWEEN 0 AND 1799),
    PRIMARY KEY (version_id, trip_id, stop_sequence),
    FOREIGN KEY (version_id, trip_id) REFERENCES trips (version_id, trip_id),
    FOREIGN KEY (version_id, stop_id) REFERENCES stops (version_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_stop_times_stop ON stop_times (version_id, stop_id);
CREATE INDEX IF NOT EXISTS idx_trips_date ON trips (version_id, service_date);
-- 发布序号的唯一索引在迁移之后创建（旧库此时才补齐 publish_seq 列）
"""

# 旧库（升级前没有 publish_seq 列）迁移：按既有发布时间/版本号补齐稠密序号
_MIGRATION_ADD_PUBLISH_SEQ = "ALTER TABLE versions ADD COLUMN publish_seq INTEGER"
_MIGRATION_BACKFILL = """
UPDATE versions
SET publish_seq = (
    SELECT COUNT(*) FROM versions v2
    WHERE v2.status IN ('active', 'superseded')
      AND (v2.published_at < versions.published_at
           OR (v2.published_at = versions.published_at AND v2.id <= versions.id))
)
WHERE status IN ('active', 'superseded')
"""


class NoActiveVersion(Exception):
    """当前没有已发布的版本。"""


class StopNotFound(Exception):
    """当前版本中不存在该站点。"""


class StopNeverExisted(Exception):
    """该站号从未在任何已发布版本中出现过。"""


class VersionNotFound(Exception):
    """版本不存在。"""


class VersionNotPublishable(Exception):
    """版本状态不允许发布。"""


class CursorAhead(Exception):
    """游标指向的发布序号晚于当前最新发布，设备拿了未来/伪造的游标。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # isolation_level=None => 自动提交关闭，所有事务由本类显式控制
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_publish_seq()
            self._conn.execute("INSERT OR IGNORE INTO meta (id, active_version_id) VALUES (1, NULL)")

    def _migrate_publish_seq(self) -> None:
        """旧库升级：补齐 publish_seq 列并按既有发布顺序回填稠密序号。"""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(versions)")}
        if "publish_seq" not in cols:
            self._conn.execute(_MIGRATION_ADD_PUBLISH_SEQ)
            self._conn.execute(_MIGRATION_BACKFILL)
        # 发布序号唯一且稠密：与自增 id 独立，防止"暂存编号与发布顺序相反"时混淆
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_publish_seq"
            " ON versions (publish_seq) WHERE publish_seq IS NOT NULL"
        )

    @contextmanager
    def _tx(self, immediate: bool = False):
        """显式事务：任何异常都会整体回滚，保证旧版本数据不受污染。"""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ---------------- 写入：暂存 / 发布 ----------------

    def record_invalid_version(self, errors: list[str]) -> int:
        """校验失败的上传也留痕（status=invalid），便于交接员追溯，但不存任何业务数据。"""
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                "INSERT INTO versions (status, created_at, errors) VALUES ('invalid', ?, ?)",
                (_now_iso(), json.dumps(errors, ensure_ascii=False)),
            )
            return int(cur.lastrowid)

    def insert_staged_version(self, data: ParsedData) -> int:
        """校验通过的数据整版写入，status=staged；此时尚不影响线上供数。"""
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """INSERT INTO versions (status, created_at, stops_count, trips_count, stop_times_count)
                   VALUES ('staged', ?, ?, ?, ?)""",
                (_now_iso(), len(data.stops), len(data.trips), len(data.stop_times)),
            )
            vid = int(cur.lastrowid)
            conn.executemany(
                "INSERT INTO stops (version_id, stop_id, stop_name) VALUES (?, ?, ?)",
                [(vid, sid, name) for sid, name in data.stops],
            )
            conn.executemany(
                "INSERT INTO trips (version_id, trip_id, route_id, service_date) VALUES (?, ?, ?, ?)",
                [(vid, tid, route, sdate) for tid, route, sdate in data.trips],
            )
            conn.executemany(
                "INSERT INTO stop_times (version_id, trip_id, stop_id, stop_sequence, departure_minutes)"
                " VALUES (?, ?, ?, ?, ?)",
                [(vid, tid, sid, seq, minutes) for tid, sid, seq, minutes in data.stop_times],
            )
            return vid

    def publish(self, version_id: int) -> dict:
        """整版发布：单事务内完成 旧版下线 + 新版上线 + 分配发布序号 + 切换 active 指针。

        publish_seq 在同一事务内按发布顺序稠密分配（与自增版本号独立）：
        暂存版编号可以与最终发布顺序相反，续领只认发布序号。
        中途任何失败都会回滚，meta 仍指向旧版本，序号不产生空洞，旧版继续供数。
        """
        with self._tx(immediate=True) as conn:
            row = conn.execute("SELECT status FROM versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise VersionNotFound(f"版本 {version_id} 不存在")
            status = row["status"]
            if status == "invalid":
                raise VersionNotPublishable(f"版本 {version_id} 校验未通过，不能发布")
            if status == "active":
                raise VersionNotPublishable(f"版本 {version_id} 已是当前发布版本")
            if status == "superseded":
                raise VersionNotPublishable(f"版本 {version_id} 已被更新版本取代，不允许回滚发布")
            now = _now_iso()
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(publish_seq), 0) + 1 FROM versions"
            ).fetchone()[0]
            conn.execute("UPDATE versions SET status = 'superseded' WHERE status = 'active'")
            conn.execute(
                "UPDATE versions SET status = 'active', published_at = ?, publish_seq = ?"
                " WHERE id = ?",
                (now, next_seq, version_id),
            )
            conn.execute("UPDATE meta SET active_version_id = ? WHERE id = 1", (version_id,))
            return {
                "version_id": version_id,
                "status": "active",
                "published_at": now,
                "publish_seq": next_seq,
            }

    # ---------------- 读取 ----------------

    def list_versions(self) -> list[dict]:
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM versions ORDER BY id DESC"
            ).fetchall()
        return [self._version_dict(r) for r in rows]

    def get_current_version(self) -> dict | None:
        with self._tx() as conn:
            row = conn.execute(
                """SELECT v.* FROM versions v
                   JOIN meta m ON m.active_version_id = v.id WHERE m.id = 1"""
            ).fetchone()
        return self._version_dict(row) if row else None

    def get_stop_departures(self, stop_id: str, from_dt: datetime) -> dict:
        """查询某站点从 from_dt（UTC）起一小时内的发车。

        合并两个运营日：from 当天 + 前一个运营日（其 24:00~29:59 的班次
        实际发生在 from 当天的凌晨）。整个读取在一个事务快照内完成，
        因此 version_id 与数据必然来自同一版本。
        """
        from_dt = from_dt.astimezone(timezone.utc)
        from_ts = from_dt.timestamp()
        to_ts = from_ts + 3600
        day = from_dt.date()
        dates = [(day - timedelta(days=1)).isoformat(), day.isoformat()]

        with self._tx() as conn:
            active = conn.execute(
                "SELECT active_version_id FROM meta WHERE id = 1"
            ).fetchone()["active_version_id"]
            if active is None:
                raise NoActiveVersion
            stop = conn.execute(
                "SELECT stop_name FROM stops WHERE version_id = ? AND stop_id = ?",
                (active, stop_id),
            ).fetchone()
            if stop is None:
                raise StopNotFound(stop_id)
            rows = conn.execute(
                """SELECT tr.route_id AS route_id, st.trip_id AS trip_id,
                          tr.service_date AS service_date, st.departure_minutes AS departure_minutes
                   FROM stop_times st
                   JOIN trips tr ON tr.version_id = st.version_id AND tr.trip_id = st.trip_id
                   WHERE st.version_id = ? AND st.stop_id = ?
                     AND tr.service_date IN (?, ?)""",
                (active, stop_id, dates[0], dates[1]),
            ).fetchall()

        departures = []
        for r in rows:
            d = date.fromisoformat(r["service_date"])
            base = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
            actual_ts = base + r["departure_minutes"] * 60
            if from_ts <= actual_ts < to_ts:
                departures.append(
                    {
                        "route_id": r["route_id"],
                        "trip_id": r["trip_id"],
                        "service_date": r["service_date"],
                        "scheduled_departure": f"{r['departure_minutes'] // 60:02d}:{r['departure_minutes'] % 60:02d}",
                        "departure_time": _iso_z(datetime.fromtimestamp(actual_ts, timezone.utc)),
                    }
                )
        departures.sort(key=lambda x: (x["departure_time"], x["trip_id"]))
        return {
            "stop_id": stop_id,
            "stop_name": stop["stop_name"],
            "from": _iso_z(from_dt),
            "to": _iso_z(datetime.fromtimestamp(to_ts, timezone.utc)),
            "version_id": active,
            "departures": departures,
        }

    # ---------------- 站牌续领 ----------------

    @staticmethod
    def _day_ordinal(service_date: str) -> int:
        d = date.fromisoformat(service_date)
        return (d - date(1970, 1, 1)).days

    def _fetch_departures(self, conn: sqlite3.Connection, version_id: int, stop_id: str):
        """读取某已发布版本中某站点的全部实际发车（跨午夜分钟换算成实际 UTC）。"""
        rows = conn.execute(
            """SELECT st.trip_id AS trip_id, st.stop_sequence AS stop_sequence,
                      tr.route_id AS route_id, tr.service_date AS service_date,
                      st.departure_minutes AS departure_minutes
               FROM stop_times st
               JOIN trips tr ON tr.version_id = st.version_id AND tr.trip_id = st.trip_id
               WHERE st.version_id = ? AND st.stop_id = ?""",
            (version_id, stop_id),
        ).fetchall()
        records = []
        for r in rows:
            actual_minutes = self._day_ordinal(r["service_date"]) * 1440 + r["departure_minutes"]
            records.append(
                DepartureRecord(
                    trip_id=r["trip_id"],
                    stop_sequence=r["stop_sequence"],
                    route_id=r["route_id"],
                    service_date=r["service_date"],
                    departure_minutes=r["departure_minutes"],
                    actual_minutes=actual_minutes,
                )
            )
        return records

    def get_stop_changes(self, stop_id: str, cursor: str) -> dict:
        """按站号与游标领取下一次（可含多次）已发布变更。

        游标 = 设备已领取到的 publish_seq；无状态，同一游标重放结果一致。
        整个过程在一个只读事务快照内完成：并发发布要么整体可见、要么整体不可见，
        续领绝不会读到未发布内容，也不会漏掉已发布的变更。
        """
        seq = decode_cursor(cursor)  # 非法游标由 InvalidCursor 上抛 -> API 400

        with self._tx() as conn:
            active = conn.execute(
                "SELECT active_version_id FROM meta WHERE id = 1"
            ).fetchone()["active_version_id"]
            if active is None:
                raise NoActiveVersion

            latest = conn.execute(
                "SELECT MAX(publish_seq) AS m FROM versions WHERE publish_seq IS NOT NULL"
            ).fetchone()["m"]
            if seq > latest:
                raise CursorAhead(
                    f"游标指向发布序号 {seq}，但当前最新只发布到 {latest}"
                )

            # 该站号是否曾在任何"已发布"版本出现（暂存版不算，杜绝读到未发布内容）。
            # 新版删掉整座站时，旧发布版里仍有该站 -> 不算 404，可继续领撤销。
            ever = conn.execute(
                "SELECT 1 FROM stops s JOIN versions v ON v.id = s.version_id"
                " WHERE s.stop_id = ? AND v.publish_seq IS NOT NULL LIMIT 1",
                (stop_id,),
            ).fetchone()
            if ever is None:
                raise StopNeverExisted(stop_id)

            # 游标之后的全部发布，按发布顺序；含本站无变更的发布（游标照样前进）
            pubs = conn.execute(
                """SELECT v.id AS version_id, v.publish_seq AS publish_seq,
                          v.published_at AS published_at
                   FROM versions v
                   WHERE v.publish_seq > ?
                   ORDER BY v.publish_seq ASC""",
                (seq,),
            ).fetchall()

            # 游标指向的那一版（seq=0 表示"尚未领过任何发布"，基线为空）
            prev_id = None
            if seq > 0:
                prev_row = conn.execute(
                    "SELECT id FROM versions WHERE publish_seq = ?", (seq,)
                ).fetchone()
                prev_id = prev_row["id"]

            # 站名：优先取最新版（可能已删站），否则沿发布链找最近一次出现的名字
            name_row = conn.execute(
                """SELECT s.stop_name AS stop_name FROM stops s
                   JOIN versions v ON v.id = s.version_id
                   WHERE s.stop_id = ? AND v.publish_seq IS NOT NULL
                   ORDER BY v.publish_seq DESC LIMIT 1""",
                (stop_id,),
            ).fetchone()
            stop_name = name_row["stop_name"] if name_row else None

            old_records = (
                self._fetch_departures(conn, prev_id, stop_id) if prev_id else []
            )
            batches: list[dict] = []
            for pub in pubs:
                new_records = self._fetch_departures(conn, pub["version_id"], stop_id)
                changes = diff_departures(old_records, new_records)
                batches.append(
                    {
                        "publish_seq": pub["publish_seq"],
                        "version_id": pub["version_id"],
                        "published_at": pub["published_at"],
                        "change_count": len(changes),
                        "changes": changes,
                    }
                )
                old_records = new_records
            latest_seq = latest
            current_version_id = active

        return {
            "stop_id": stop_id,
            "stop_name": stop_name,
            "cursor": cursor,
            "next_cursor": encode_cursor(latest_seq),
            "caught_up": not batches,
            "current_version_id": current_version_id,
            "latest_publish_seq": latest_seq,
            "publications": batches,
        }

    # ---------------- 工具 ----------------

    @staticmethod
    def _version_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        if d.get("errors"):
            d["errors"] = json.loads(d["errors"])
        else:
            d.pop("errors", None)
        return d

    def close(self) -> None:
        with self._lock:
            self._conn.close()
