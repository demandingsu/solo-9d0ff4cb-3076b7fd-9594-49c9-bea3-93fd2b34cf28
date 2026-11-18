"""站牌续领排班变更 API 的自测。

覆盖需求点：
  * 首次续领（起始游标）与逐次游标推进；
  * added / cancelled / modified（时刻、线路）三类变更，身份键 = 班次号 + 停站顺序；
  * 跨午夜（24:00~29:59）实际 UTC 发车的差异；
  * 新版删掉整座站：旧站号仍可领到撤销，再领游标照样前进；
  * 某次发布不涉及该站：changes 为空但游标前进；同一游标重放结果完全相同；
  * 暂存编号与发布顺序相反：续领只认发布序号；
  * 发布失败回滚：供数与续领位置都不变；暂存内容不会被续领读到；
  * 并发拉取与发布交错：不漏变更、不读未发布、每次响应自洽；
  * 游标非法 / 超前 / 站号从未发布 / 尚无发布版本 的错误码。
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.changes import decode_cursor, encode_cursor
from app.main import INITIAL_CURSOR, create_app

STOPS = "stop_id,stop_name\nS1,中央车站\nS2,东城口\n"
TRIPS_V1 = (
    "trip_id,route_id,service_date\n"
    "T1,R1,2026-11-18\n"
    "T2,R1,2026-11-19\n"
    "T3,R2,2026-11-18\n"
)
# T1: S1 23:50 -> S2 25:10（次日 01:10，跨午夜）；T2: S1 00:20；T3: S1 24:30
ST_V1 = (
    "trip_id,stop_id,stop_sequence,departure_time\n"
    "T1,S1,1,23:50\n"
    "T1,S2,2,25:10\n"
    "T2,S1,1,00:20\n"
    "T3,S1,1,24:30\n"
)
# V2：T2@S1 时刻 00:20 -> 00:25；新增 T4@S1（R2, 2026-11-19 06:00）
TRIPS_V2 = TRIPS_V1 + "T4,R2,2026-11-19\n"
ST_V2 = (
    "trip_id,stop_id,stop_sequence,departure_time\n"
    "T1,S1,1,23:50\n"
    "T1,S2,2,25:10\n"
    "T2,S1,1,00:25\n"
    "T3,S1,1,24:30\n"
    "T4,S1,1,06:00\n"
)
# V3：删掉 S2；撤销 T4；T2 改线 R1 -> R9（时刻不变）
TRIPS_V3 = (
    "trip_id,route_id,service_date\n"
    "T1,R1,2026-11-18\n"
    "T2,R9,2026-11-19\n"
    "T3,R2,2026-11-18\n"
)
ST_V3 = (
    "trip_id,stop_id,stop_sequence,departure_time\n"
    "T1,S1,1,23:50\n"
    "T2,S1,1,00:25\n"
    "T3,S1,1,24:30\n"
)
# V4：与 V3 完全相同的排班（无任何站变更，游标仍应前进）
STOPS_V4 = "stop_id,stop_name\nS1,中央车站\n"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def upload(client, stops=STOPS, trips=TRIPS_V1, stop_times=ST_V1):
    return client.post(
        "/versions",
        files={
            "stops": ("stops.csv", stops.encode(), "text/csv"),
            "trips": ("trips.csv", trips.encode(), "text/csv"),
            "stop_times": ("stop_times.csv", stop_times.encode(), "text/csv"),
        },
    )


def publish(client, version_id):
    return client.post(f"/versions/{version_id}/publish")


def stage(client, stops=STOPS, trips=TRIPS_V1, stop_times=ST_V1):
    r = upload(client, stops, trips, stop_times)
    assert r.status_code == 201, r.text
    return r.json()["version_id"]


def stage_and_publish(client, stops=STOPS, trips=TRIPS_V1, stop_times=ST_V1):
    vid = stage(client, stops, trips, stop_times)
    r = publish(client, vid)
    assert r.status_code == 200, r.text
    return vid, r.json()["publish_seq"]


def changes(client, stop_id, cursor=None):
    if cursor is None:
        return client.get(f"/stops/{stop_id}/changes")
    return client.get(f"/stops/{stop_id}/changes", params={"cursor": cursor})


def index(changelist):
    return {(c["change_type"], c["trip_id"], c["stop_sequence"]): c for c in changelist}


# ---------------- 首次续领与逐步推进 ----------------


def test_initial_cursor_constant_is_seq_zero():
    assert decode_cursor(INITIAL_CURSOR) == 0


def test_first_fetch_returns_all_added_and_cursor_advances(client):
    vid, seq = stage_and_publish(client)
    r = changes(client, "S1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cursor"] == INITIAL_CURSOR
    assert decode_cursor(body["next_cursor"]) == seq == 1
    assert body["caught_up"] is False
    assert len(body["publications"]) == 1
    batch = body["publications"][0]
    assert batch["publish_seq"] == 1 and batch["version_id"] == vid
    assert batch["change_count"] == 3
    got = index(batch["changes"])
    assert set(got) == {("added", "T1", 1), ("added", "T2", 1), ("added", "T3", 1)}
    # 跨午夜班次的实际 UTC 时刻已换算
    assert got[("added", "T3", 1)]["actual_departure"] == "2026-11-19T00:30:00Z"
    assert got[("added", "T3", 1)]["scheduled_departure"] == "24:30"


def test_follow_up_picks_modified_and_added_only(client):
    _, s1 = stage_and_publish(client)
    _, s2 = stage_and_publish(client, trips=TRIPS_V2, stop_times=ST_V2)

    # 先把游标定位到第 1 版（模拟设备已领过首发），再续领第 2 版的增量
    r = changes(client, "S1", encode_cursor(s1))
    assert r.status_code == 200
    body = r.json()
    assert decode_cursor(body["cursor"]) == s1
    assert decode_cursor(body["next_cursor"]) == s2 == 2
    batch = body["publications"][0]
    got = index(batch["changes"])
    assert set(got) == {("modified", "T2", 1), ("added", "T4", 1)}

    t2 = got[("modified", "T2", 1)]
    assert t2["changed_fields"] == ["actual_departure"]
    assert t2["before"]["scheduled_departure"] == "00:20"
    assert t2["after"]["scheduled_departure"] == "00:25"
    assert t2["before"]["actual_departure"] == "2026-11-19T00:20:00Z"
    assert t2["after"]["actual_departure"] == "2026-11-19T00:25:00Z"


def test_caught_up_response_advances_nowhere_until_next_publish(client):
    stage_and_publish(client)
    cur = changes(client, "S1").json()["next_cursor"]
    r = changes(client, "S1", cur)
    assert r.status_code == 200
    body = r.json()
    assert body["caught_up"] is True
    assert body["publications"] == []
    assert body["next_cursor"] == cur  # 没有新发布时游标不动


# ---------------- 撤销、改线与撤站 ----------------


def test_cancellation_and_route_modification(client):
    stage_and_publish(client, trips=TRIPS_V2, stop_times=ST_V2)
    stage_and_publish(client, stops=STOPS_V4, trips=TRIPS_V3, stop_times=ST_V3)

    body = changes(client, "S1", encode_cursor(1)).json()
    got = index(body["publications"][0]["changes"])
    assert set(got) == {("cancelled", "T4", 1), ("modified", "T2", 1)}
    t2 = got[("modified", "T2", 1)]
    assert t2["changed_fields"] == ["route_id"]
    assert t2["before"]["route_id"] == "R1" and t2["after"]["route_id"] == "R9"
    # 时刻未变
    assert t2["before"]["actual_departure"] == t2["after"]["actual_departure"]


def test_deleted_stop_still_receives_cancellations_then_advances(client):
    # 发布 V1（含 S2）、V3（删 S2）、V4（同样没有 S2）
    stage_and_publish(client)
    _, seq3 = stage_and_publish(client, stops=STOPS_V4, trips=TRIPS_V3, stop_times=ST_V3)
    _, seq4 = stage_and_publish(client, stops=STOPS_V4, trips=TRIPS_V3, stop_times=ST_V3)

    # 已在 V1 之后的设备：V3 应领到 S2 上 T1 的撤销
    cur1 = encode_cursor(1)
    body = changes(client, "S2", cur1).json()
    assert decode_cursor(body["next_cursor"]) == seq4
    pubs = body["publications"]
    assert [p["publish_seq"] for p in pubs] == [seq3, seq4]
    first = index(pubs[0]["changes"])
    assert set(first) == {("cancelled", "T1", 2)}
    assert first[("cancelled", "T1", 2)]["actual_departure"] == "2026-11-19T01:10:00Z"
    # V4 不涉及 S2：空变更但游标继续前进
    assert pubs[1]["changes"] == [] and pubs[1]["change_count"] == 0

    # 用最终游标重领：已追平，无批次
    again = changes(client, "S2", body["next_cursor"]).json()
    assert again["caught_up"] is True and again["publications"] == []


def test_stop_that_never_existed_is_404(client):
    stage_and_publish(client)
    r = changes(client, "NOPE")
    assert r.status_code == 404


# ---------------- 游标幂等与空发布推进 ----------------


def test_same_cursor_replays_identical_result(client):
    stage_and_publish(client, trips=TRIPS_V2, stop_times=ST_V2)
    stage_and_publish(client, stops=STOPS_V4, trips=TRIPS_V3, stop_times=ST_V3)
    r1 = changes(client, "S1", INITIAL_CURSOR)
    r2 = changes(client, "S1", INITIAL_CURSOR)
    assert r1.json() == r2.json()


def test_noop_publication_still_advances_cursor(client):
    _, s1 = stage_and_publish(client)
    _, s2 = stage_and_publish(client)  # 与 V1 完全相同
    body = changes(client, "S1", encode_cursor(s1)).json()
    assert [p["publish_seq"] for p in body["publications"]] == [s2]
    assert body["publications"][0]["changes"] == []
    assert decode_cursor(body["next_cursor"]) == s2


def test_catching_up_multiple_publishes_in_one_call(client):
    stage_and_publish(client)
    stage_and_publish(client, trips=TRIPS_V2, stop_times=ST_V2)
    stage_and_publish(client, stops=STOPS_V4, trips=TRIPS_V3, stop_times=ST_V3)
    body = changes(client, "S1").json()  # 从 0 一次领三版
    assert [p["publish_seq"] for p in body["publications"]] == [1, 2, 3]
    assert decode_cursor(body["next_cursor"]) == 3


# ---------------- 暂存编号与发布顺序相反 ----------------


def test_publish_seq_follows_publish_order_not_version_id(client):
    # 先暂存两版：版本号 1、2；逆序发布 2 再 1
    v_a = stage(client, stop_times=ST_V1)
    v_b = stage(client, trips=TRIPS_V2, stop_times=ST_V2)
    assert v_a == 1 and v_b == 2
    pub_b = publish(client, v_b).json()
    pub_a = publish(client, v_a).json()
    # 逆序发布：版本 2 拿到发布序号 1，版本 1 拿到发布序号 2
    assert pub_b["publish_seq"] == 1 and pub_b["version_id"] == 2
    assert pub_a["publish_seq"] == 2 and pub_a["version_id"] == 1

    # 续领按发布顺序：从起始游标一次领两版，第一版（实际是版本 2）先到，S1 有 4 条 added
    first = changes(client, "S1").json()
    assert [p["version_id"] for p in first["publications"]] == [2, 1]
    assert first["publications"][0]["change_count"] == 4

    # 第二次发布切回版本 1：T4 被撤销、T2 时刻回退
    second = changes(client, "S1", encode_cursor(1)).json()
    got = index(second["publications"][0]["changes"])
    assert ("cancelled", "T4", 1) in got
    assert ("modified", "T2", 1) in got


# ---------------- 发布失败与暂存不可见 ----------------


def test_failed_publish_keeps_feed_and_cursor_position(client):
    _, seq = stage_and_publish(client)
    # 重复发布当前 active 版本 -> 409 回滚
    current = client.get("/versions/current").json()["id"]
    r = publish(client, current)
    assert r.status_code == 409
    # 续领位置不变：仍追平在原序号
    body = changes(client, "S1", encode_cursor(seq)).json()
    assert body["caught_up"] is True
    assert body["latest_publish_seq"] == seq


def test_staged_version_is_never_visible_to_changes(client):
    stage_and_publish(client)
    staged_vid = stage(client, trips=TRIPS_V2, stop_times=ST_V2)  # 只暂存
    body = changes(client, "S1").json()
    # 只看到已发布的一版；T4 未发布，不可见
    assert all(p["version_id"] != staged_vid for p in body["publications"])
    assert body["latest_publish_seq"] == 1
    # 暂存版里才有的站号不能被领
    assert changes(client, "S9").status_code == 404


# ---------------- 并发拉取与发布交错 ----------------


def test_concurrent_pulls_never_skip_or_read_unpublished(client):
    stage_and_publish(client)
    failures: list[str] = []
    seen_final: list[int] = []
    stop = threading.Event()

    def puller():
        cursor = INITIAL_CURSOR
        try:
            while not stop.is_set():
                body = changes(client, "S1", cursor).json()
                # 自洽性：游标链连续、批次序号严格递增、无空洞
                seqs = [p["publish_seq"] for p in body["publications"]]
                assert seqs == sorted(set(seqs))
                if seqs:
                    assert decode_cursor(cursor) + 1 == seqs[0]
                    cursor = body["next_cursor"]
                    # 幂等：同一游标重放，已发布批次的内容必须逐字节一致
                    replay = changes(client, "S1", body["cursor"]).json()
                    assert replay["publications"][: len(seqs)] == body["publications"]
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc))

    threads = [threading.Thread(target=puller) for _ in range(6)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    # 交错发布两版
    stage_and_publish(client, trips=TRIPS_V2, stop_times=ST_V2)
    time.sleep(0.2)
    stage_and_publish(client, stops=STOPS_V4, trips=TRIPS_V3, stop_times=ST_V3)
    time.sleep(0.5)
    stop.set()
    for t in threads:
        t.join()
    assert not failures, failures

    # 最终所有设备都能领到全部三版，且 T4 的 added 与 cancelled 成对出现
    final = changes(client, "S1").json()
    assert [p["publish_seq"] for p in final["publications"]] == [1, 2, 3]
    all_types = {
        (c["change_type"], c["trip_id"])
        for p in final["publications"]
        for c in p["changes"]
    }
    assert ("added", "T4") in all_types and ("cancelled", "T4") in all_types
    seen_final.append(decode_cursor(final["next_cursor"]))
    assert seen_final == [3]


# ---------------- 错误处理 ----------------


def test_no_published_version_is_409(client):
    assert changes(client, "S1").status_code == 409


def test_invalid_cursor_is_400(client):
    stage_and_publish(client)
    assert changes(client, "S1", "not-a-cursor!!!").status_code == 400
    tampered = encode_cursor(2) + "xx"
    assert changes(client, "S1", tampered).status_code == 400


def test_cursor_ahead_of_latest_publish_is_400(client):
    stage_and_publish(client)
    assert changes(client, "S1", encode_cursor(99)).status_code == 400


def test_cursor_roundtrip():
    for seq in (0, 1, 42, 10_000):
        assert decode_cursor(encode_cursor(seq)) == seq


# ---------------- 旧库迁移 ----------------


def test_legacy_database_without_publish_seq_is_migrated(client, tmp_path):
    """模拟升级前的旧库（versions 无 publish_seq 列），重开后应回填稠密序号且续领可用。"""
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    # 先用当前应用发布两版，再删掉新列模拟旧库结构
    app = create_app(db_path)
    with TestClient(app) as c:
        stage_and_publish(c)
        stage_and_publish(c, trips=TRIPS_V2, stop_times=ST_V2)
    app.state.db.close()

    con = sqlite3.connect(db_path)
    # sqlite 不支持 DROP COLUMN 旧版本兼容写法：重建无新列的 versions 表
    rows = con.execute(
        "SELECT id, status, created_at, published_at, stops_count, trips_count,"
        " stop_times_count, errors FROM versions"
    ).fetchall()
    con.execute("DROP INDEX IF EXISTS idx_versions_publish_seq")
    con.execute("ALTER TABLE versions RENAME TO versions_old")
    con.execute(
        "CREATE TABLE versions ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, status TEXT NOT NULL,"
        " created_at TEXT NOT NULL, published_at TEXT,"
        " stops_count INTEGER NOT NULL DEFAULT 0, trips_count INTEGER NOT NULL DEFAULT 0,"
        " stop_times_count INTEGER NOT NULL DEFAULT 0, errors TEXT)"
    )
    con.executemany(
        "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)", rows
    )
    con.execute("DROP TABLE versions_old")
    con.commit()
    con.close()

    # 重新打开应自动迁移：回填 1、2 两个发布序号
    app2 = create_app(db_path)
    with TestClient(app2) as c2:
        body = c2.get("/stops/S1/changes").json()
        assert [p["publish_seq"] for p in body["publications"]] == [1, 2]
        assert decode_cursor(body["next_cursor"]) == 2
    app2.state.db.close()
