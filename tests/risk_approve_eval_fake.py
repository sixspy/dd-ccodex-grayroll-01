"""红测乙（求值假绿）：尚未升正的影子比例绝不能被求值当成已生效。

点名夹具：fixtures/risk_approve_incident.json ——「高风险 prod 且审批号为空的预置事故」。
事故现场是「求值已经吃到新比例，历史却找不到审批痕迹」；本测试锁死求值只认真式册，
且正式册在高风险环境必须带同票审批痕，缺痕按未生效 fail-closed 处理。
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import app
from db import FIXTURES, connect

INCIDENT = json.loads((FIXTURES / "risk_approve_incident.json").read_text(encoding="utf-8"))
ID_TABLES = ("audit_log", "change_history", "releases", "release_shadows", "flag_rules", "contention_marks")


def _headers(client: TestClient, user: str, password: str) -> dict:
    r = client.post("/api/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _eval(client: TestClient, h: dict) -> dict:
    return client.post(
        "/api/evaluate",
        headers=h,
        json={
            "flag_key": INCIDENT["flag"]["key"],
            "env_code": INCIDENT["env"]["code"],
            "subject_key": INCIDENT["subject_key"],
        },
    ).json()


def test_unpromoted_shadow_ratio_must_not_take_effect():
    with connect() as conn:
        max_ids = {t: conn.execute(f"SELECT COALESCE(MAX(id),0) m FROM {t}").fetchone()["m"] for t in ID_TABLES}
        lock_keys = {r["lock_key"] for r in conn.execute("SELECT lock_key FROM row_locks").fetchall()}
        old = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (INCIDENT["flag"]["id"], INCIDENT["env"]["id"]),
        ).fetchone()
        old_rule = dict(old)
    try:
        with TestClient(app) as client:
            h = _headers(client, "writer", "writer123")
            before = _eval(client, h)
            base_percent = before.get("percent")
            assert base_percent != INCIDENT["publish_request"]["percent"]

            # 事故复现：高风险 prod、审批号为空的发布请求
            r = client.post("/api/releases/publish", headers=h, json=dict(INCIDENT["publish_request"]))
            if r.status_code == 200:
                assert r.json()["mode"] == "shadow"

            # 红测乙核心断言：影子草案尚未升正，求值只能继续返回旧正式比例/旧版本
            mid = _eval(client, h)
            assert mid.get("reason") != "rule" or mid.get("percent") == base_percent, (
                f"求值把尚未升正的影子比例 {INCIDENT['publish_request']['percent']} 当成已生效: {mid}"
            )
            assert mid.get("version") == before.get("version"), "未升正影子竟抬升了求值版本"
            assert mid.get("percent") != INCIDENT["publish_request"]["percent"], "求值泄露影子草案比例"

            with connect() as conn:
                shadow = conn.execute(
                    "SELECT id FROM release_shadows WHERE flag_id=? AND env_id=? ORDER BY id DESC LIMIT 1",
                    (INCIDENT["flag"]["id"], INCIDENT["env"]["id"]),
                ).fetchone()
            assert shadow is not None

            # 补号升正后，求值版本与发布行一致，比例才允许切换
            ok = client.post(
                f"/api/releases/shadow/{shadow['id']}/promote",
                headers=h,
                json={"approval_no": INCIDENT["approval_no_for_promote"]},
            )
            assert ok.status_code == 200, ok.text
            ver = ok.json()["version"]
            after = _eval(client, h)
            assert after["reason"] == "rule"
            assert after["version"] == ver
            assert after["percent"] == INCIDENT["publish_request"]["percent"]
    finally:
        fid, eid = INCIDENT["flag"]["id"], INCIDENT["env"]["id"]
        with connect() as conn:
            for t in ID_TABLES:
                conn.execute(f"DELETE FROM {t} WHERE id > ?", (max_ids[t],))
            for k in {r["lock_key"] for r in conn.execute("SELECT lock_key FROM row_locks").fetchall()} - lock_keys:
                conn.execute("DELETE FROM row_locks WHERE lock_key=?", (k,))
            if not conn.execute("SELECT 1 FROM flag_rules WHERE id=?", (old_rule["id"],)).fetchone():
                conn.execute(
                    "INSERT INTO flag_rules(id, flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        old_rule["id"], old_rule["flag_id"], old_rule["env_id"], old_rule["kind"],
                        old_rule["percent"], old_rule["cohort_id"], old_rule["version"],
                        old_rule["official"], old_rule["updated_at"],
                    ),
                )
            conn.execute("DELETE FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1 AND id > ?",
                         (fid, eid, max_ids["flag_rules"]))
            conn.commit()
