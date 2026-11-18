"""站牌设备续领排班变更：游标编解码与相邻两版差异计算。

设计要点（对应需求）：
  * 游标不透明、无状态：只编码"设备已领取到的发布序号"（publish_seq），
    同一游标重放必然得到同一结果，服务端不保存任何设备续领位置；
  * 差异身份键为 (trip_id, stop_sequence) —— 班次号 + 停站顺序；
  * 比较的是该站在两版中的"实际 UTC 发车"（service_date + 跨午夜分钟数），
    同时比较线路号；由此识别 added / cancelled / modified；
  * 新版整座站被删除时，调用方仍会把旧版该站的发车传入，旧站号照常领到撤销。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

CURSOR_PREFIX = "bc1"  # busfeed cursor v1，仅用于识别本系统签发的游标


class InvalidCursor(Exception):
    """游标不是本系统签发或内容非法。"""


def encode_cursor(publish_seq: int) -> str:
    """把已领取到的发布序号编码为不透明游标。"""
    payload = json.dumps({"v": 1, "seq": int(publish_seq)}, separators=(",", ":"))
    raw = CURSOR_PREFIX + ":" + payload
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str) -> int:
    """解码游标，返回已领取到的发布序号；非法游标抛 InvalidCursor。"""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursor("游标不是合法的 base64 字符串") from exc
    if not raw.startswith(CURSOR_PREFIX + ":"):
        raise InvalidCursor("游标不是本系统签发的续领游标")
    try:
        payload = json.loads(raw.split(":", 1)[1])
        seq = payload["seq"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise InvalidCursor("游标内容无法解析") from exc
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise InvalidCursor("游标中的发布序号非法")
    return seq


@dataclass(frozen=True)
class DepartureRecord:
    """某版本中某站点的一条实际发车。

    actual_minutes 为相对 Unix 纪元的分钟数（service_date 00:00 UTC + 跨午夜分钟），
    用整数即可无歧义地表达实际 UTC 发车时刻，并天然覆盖跨午夜（24:00~29:59）。
    """

    trip_id: str
    stop_sequence: int
    route_id: str
    service_date: str
    departure_minutes: int  # 相对运营日 00:00 的分钟（0~1799）
    actual_minutes: int  # 相对 1970-01-01T00:00Z 的分钟

    @property
    def identity(self) -> tuple[str, int]:
        return self.trip_id, self.stop_sequence

    def snapshot(self) -> tuple[str, str, int]:
        """用于比较的内容快照：线路 + 实际 UTC 发车（service_date+分钟）。"""
        return self.route_id, self.service_date, self.actual_minutes


def _format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _actual_iso_z(actual_minutes: int) -> str:
    """相对纪元的分钟 -> 实际 UTC 发车时刻 ISO 8601（天然支持 24:00~29:59 跨午夜）。"""
    dt = datetime.fromtimestamp(actual_minutes * 60, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_payload(rec: DepartureRecord) -> dict[str, Any]:
    return {
        "trip_id": rec.trip_id,
        "stop_sequence": rec.stop_sequence,
        "route_id": rec.route_id,
        "service_date": rec.service_date,
        "scheduled_departure": _format_hhmm(rec.departure_minutes),
        "actual_departure": _actual_iso_z(rec.actual_minutes),
        "_actual_minutes": rec.actual_minutes,  # 内部排序用，返回前剥离
    }


def diff_departures(
    old: list[DepartureRecord],
    new: list[DepartureRecord],
) -> list[dict[str, Any]]:
    """比较相邻两版在同一站点的实际发车，返回变更清单。

    身份键 (trip_id, stop_sequence)：
      * 仅新版有 -> added（新增发车，含撤站后恢复场景）；
      * 仅旧版有 -> cancelled（撤销发车，含整座站被删的场景）；
      * 两版都有且 (线路, service_date, 实际 UTC 时刻) 任一不同 -> modified，
        附带 before/after 与 changed_fields；完全相同则不出现在结果中。
    """
    old_map = {r.identity: r for r in old}
    new_map = {r.identity: r for r in new}
    changes: list[dict[str, Any]] = []

    for ident in sorted(new_map):
        rec = new_map[ident]
        if ident not in old_map:
            changes.append({"change_type": "added", **_base_payload(rec)})

    for ident in sorted(old_map):
        rec = old_map[ident]
        if ident not in new_map:
            changes.append({"change_type": "cancelled", **_base_payload(rec)})

    for ident in sorted(old_map.keys() & new_map.keys()):
        before, after = old_map[ident], new_map[ident]
        if before.snapshot() == after.snapshot():
            continue
        changed_fields = []
        if before.route_id != after.route_id:
            changed_fields.append("route_id")
        if before.actual_minutes != after.actual_minutes:
            changed_fields.append("actual_departure")
        if before.service_date != after.service_date:
            # 实际时刻不同时已归入 actual_departure；仅运营日归属变化也要点出
            if "actual_departure" not in changed_fields:
                changed_fields.append("service_date")
        changes.append(
            {
                "change_type": "modified",
                "trip_id": after.trip_id,
                "stop_sequence": after.stop_sequence,
                "changed_fields": changed_fields,
                "before": _base_payload(before),
                "after": _base_payload(after),
            }
        )

    # 稳定排序：先看实际发车时刻，再看班次与停站顺序，最后按变更类型
    def order_key(chg: dict[str, Any]):
        rec = chg.get("after") or chg
        return (
            rec["_actual_minutes"],
            rec["trip_id"],
            rec["stop_sequence"],
            chg["change_type"],
        )

    changes.sort(key=order_key)
    for chg in changes:
        chg.pop("_actual_minutes", None)
        if "before" in chg:
            chg["before"].pop("_actual_minutes", None)
            chg["after"].pop("_actual_minutes", None)
    return changes
