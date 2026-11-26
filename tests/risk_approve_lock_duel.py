"""红测丙（并发争用）：两路抢同一把「按开关加环境维度的发布互斥」不得无锁双成。

占位键：flag-release:{flag_id}:{env_id}。落败会话只许留 contention_marks 争用痕，
绝不允许记正式成功；争用（409）叙述与缺审批号取舍（400）叙述分开。
并发双请求统一走 duel_risk_approve_publish（生成器：逐轮产出，逐轮断言，最后清理）。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app import app
from db import connect

# 高风险 prod(env=3) 上选三个互不干扰的开关，每轮一对，避免跨轮相互污染
DUEL_PAIRS = [(2, 3), (3, 3), (5, 3)]
ID_TABLES = ("audit_log", "change_history", "releases", "release_shadows", "flag_rules", "contention_marks")
USERS = (("admin", "admin123"), ("writer", "writer123"))


def _login(user: str, password: str):
    client = TestClient(app)
    r = client.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return client, {"Authorization": f"Bearer {r.json()['token']}"}


def _cleanup_snapshot():
    with connect() as conn:
        max_ids = {t: conn.execute(f"SELECT COALESCE(MAX(id),0) m FROM {t}").fetchone()["m"] for t in ID_TABLES}
        lock_keys = {r["lock_key"] for r in conn.execute("SELECT lock_key FROM row_locks").fetchall()}
        flag_status = {
            r["id"]: r["status"]
            for r in conn.execute("SELECT id, status FROM flags WHERE id IN (2,3,5)").fetchall()
        }
    return max_ids, lock_keys, flag_status


def _restore(max_ids, lock_keys, flag_status) -> None:
    with connect() as conn:
        for t in ID_TABLES:
            conn.execute(f"DELETE FROM {t} WHERE id > ?", (max_ids[t],))
        for k in {r["lock_key"] for r in conn.execute("SELECT lock_key FROM row_locks").fetchall()} - lock_keys:
            conn.execute("DELETE FROM row_locks WHERE lock_key=?", (k,))
        for fid, status in flag_status.items():
            conn.execute("UPDATE flags SET status=? WHERE id=?", (status, fid))
        conn.commit()


def _one_publish(client: TestClient, headers: dict, fid: int, eid: int, round_no: int,
                 barrier: threading.Barrier, box: dict, slot: str) -> None:
    try:
        barrier.wait()  # 两路同时撞门，最大化争用窗口
        r = client.post(
            "/api/releases/publish",
            headers=headers,
            json={
                "flag_id": fid,
                "env_id": eid,
                "kind": "percent",
                "percent": 40 + round_no,
                "summary": f"并发争用探针 round{round_no} {slot}",
                "use_shadow": False,
            },
        )
        box[slot] = {"status": r.status_code, "body": r.json() if r.content else None}
    except Exception as exc:  # 任何服务端异常都不允许伪装成通过
        box[slot] = {"status": None, "error": repr(exc)}


@contextmanager
def duel_risk_approve_publish():
    """两路并发逐轮抢同一把 flag-release:{fid}:{eid} 互斥键；上下文退出时统一回滚。"""
    max_ids, lock_keys, flag_status = _cleanup_snapshot()
    try:
        def _rounds():
            for round_no, (fid, eid) in enumerate(DUEL_PAIRS, start=1):
                key = f"flag-release:{fid}:{eid}"
                client_a, h_a = _login(*USERS[0])
                client_b, h_b = _login(*USERS[1])
                barrier = threading.Barrier(2)
                box: dict = {}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futs = [
                        pool.submit(_one_publish, client_a, h_a, fid, eid, round_no, barrier, box, "a"),
                        pool.submit(_one_publish, client_b, h_b, fid, eid, round_no, barrier, box, "b"),
                    ]
                    for f in futs:
                        f.result()
                yield {"fid": fid, "eid": eid, "key": key, "box": box}
        yield _rounds()
    finally:
        _restore(max_ids, lock_keys, flag_status)


def test_duel_risk_approve_publish_no_double_success():
    with duel_risk_approve_publish() as rounds:
        for round_no, item in enumerate(rounds, start=1):
            fid, eid, key = item["fid"], item["eid"], item["key"]
            statuses = sorted(v["status"] for v in item["box"].values())
            assert statuses == [200, 409], f"round{round_no} 互斥失效，出现非[一胜一败]结局: {item['box']}"

            winner = next(v for v in item["box"].values() if v["status"] == 200)
            loser = next(v for v in item["box"].values() if v["status"] == 409)
            assert "争用" in str(loser["body"]), "败方叙述必须是争用(409)，不得混同缺号取舍(400)"

            with connect() as conn:
                marks = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM contention_marks WHERE lock_key=? ORDER BY id", (key,)
                    ).fetchall()
                ]
                official_releases = conn.execute(
                    "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()["c"]
                official_rules = conn.execute(
                    "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()["c"]
                shadows = conn.execute(
                    "SELECT COUNT(*) c FROM release_shadows WHERE flag_id=? AND env_id=?", (fid, eid)
                ).fetchone()["c"]
                lock_left = conn.execute(
                    "SELECT COUNT(*) c FROM row_locks WHERE lock_key=?", (key,)
                ).fetchone()["c"]

            assert len(marks) == 1 and marks[0]["loser"] != marks[0]["winner"], "败方须且只留一条争用痕"
            assert official_releases == 0, "高风险缺号争用轮不得有任何正式发布行"
            assert official_rules == 0, "高风险缺号争用轮不得动正式规则"
            assert shadows == 1, "胜方只许落一条影子草案"
            assert winner["body"]["mode"] == "shadow"
            assert lock_left == 0, "胜方收尾必须释放互斥键"
