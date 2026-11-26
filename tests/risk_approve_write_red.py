"""红测甲（写路径）：高风险 prod 缺审批号，正式册必须零增。

点名夹具：fixtures/risk_approve_incident.json ——「高风险 prod 且审批号为空的预置事故」。
口径（乙路径，当日封死）：缺号只许写影子发布草案；补号后同事务原子升正，
审批号与正式发布同票提交，跨表联查账实；升正前正式区零改。
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


class _Cleaner:
    """按 max(id) 快照回滚测试期间所有写入，并恢复被覆盖的旧正式规则。"""

    def __init__(self) -> None:
        with connect() as conn:
            self.max_ids = {t: conn.execute(f"SELECT COALESCE(MAX(id),0) m FROM {t}").fetchone()["m"] for t in ID_TABLES}
            self.lock_keys = {r["lock_key"] for r in conn.execute("SELECT lock_key FROM row_locks").fetchall()}
            row = conn.execute(
                "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
                (INCIDENT["flag"]["id"], INCIDENT["env"]["id"]),
            ).fetchone()
            self.old_rule = dict(row) if row else None

    def restore(self) -> None:
        fid, eid = INCIDENT["flag"]["id"], INCIDENT["env"]["id"]
        with connect() as conn:
            for t in ID_TABLES:
                conn.execute(f"DELETE FROM {t} WHERE id > ?", (self.max_ids[t],))
            for k in {r["lock_key"] for r in conn.execute("SELECT lock_key FROM row_locks").fetchall()} - self.lock_keys:
                conn.execute("DELETE FROM row_locks WHERE lock_key=?", (k,))
            if self.old_rule and not conn.execute(
                "SELECT 1 FROM flag_rules WHERE id=?", (self.old_rule["id"],)
            ).fetchone():
                conn.execute(
                    "INSERT INTO flag_rules(id, flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        self.old_rule["id"], self.old_rule["flag_id"], self.old_rule["env_id"],
                        self.old_rule["kind"], self.old_rule["percent"], self.old_rule["cohort_id"],
                        self.old_rule["version"], self.old_rule["official"], self.old_rule["updated_at"],
                    ),
                )
            conn.execute("DELETE FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1 AND id > ?",
                         (fid, eid, self.max_ids["flag_rules"]))
            conn.commit()


def test_high_risk_publish_without_approval_official_zero_growth():
    cleaner = _Cleaner()
    try:
        with TestClient(app) as client:
            h = _headers(client, "writer", "writer123")
            fid, eid = INCIDENT["flag"]["id"], INCIDENT["env"]["id"]
            with connect() as conn:
                before = {
                    "releases": conn.execute(
                        "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                    ).fetchone()["c"],
                    "rules": conn.execute(
                        "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                    ).fetchone()["c"],
                    "rule_ver": (conn.execute(
                        "SELECT version FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                    ).fetchone() or {"version": None})["version"],
                }

            body = dict(INCIDENT["publish_request"])  # approval_no 为空，use_shadow=false
            r = client.post("/api/releases/publish", headers=h, json=body)

            # 缺号绝不允许落正式成功：要么被拒，要么被收口为影子草案
            if r.status_code == 200:
                assert r.json()["mode"] == "shadow", f"缺审批号竟返回正式成功: {r.text}"
            else:
                assert r.status_code in (400, 409)

            with connect() as conn:
                after_releases = conn.execute(
                    "SELECT COUNT(*) c FROM releases WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()["c"]
                after_rules = conn.execute(
                    "SELECT COUNT(*) c FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()["c"]
                rule_row = conn.execute(
                    "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()
                shadow = conn.execute(
                    "SELECT * FROM release_shadows WHERE flag_id=? AND env_id=? ORDER BY id DESC LIMIT 1", (fid, eid)
                ).fetchone()

            # 红测甲核心断言：无审批号不得插入正式发布行、正式规则零改
            assert after_releases == before["releases"], "正式发布册在缺审批号时被增行"
            assert after_rules == before["rules"], "正式规则在缺审批号时被增删"
            assert rule_row is not None and rule_row["version"] == before["rule_ver"], "正式规则版本被缺号发布抬升"
            assert shadow is not None and shadow["status"] == "open", "缺号发布必须收口为 open 影子草案"

            # 升正缺号同样必须被拒
            pr = client.post(f"/api/releases/shadow/{shadow['id']}/promote", headers=h, json={"approval_no": "  "})
            assert pr.status_code == 400, f"空审批号升正未被拒: {pr.text}"

            # 补号后原子升正：审批号与正式发布同票，跨表联查账实
            ok = client.post(
                f"/api/releases/shadow/{shadow['id']}/promote",
                headers=h,
                json={"approval_no": INCIDENT["approval_no_for_promote"]},
            )
            assert ok.status_code == 200, ok.text
            rid, ver = ok.json()["id"], ok.json()["version"]
            with connect() as conn:
                ledger = conn.execute(
                    """
                    SELECT r.id AS rid, r.version, r.official, a.detail AS audit_detail, h.detail AS history_detail
                    FROM releases r
                    JOIN audit_log a ON a.entity='release' AND a.entity_id=r.id
                    JOIN change_history h ON h.flag_id=r.flag_id AND h.env_id=r.env_id AND h.action='release_promote'
                    WHERE r.id=?
                    """,
                    (rid,),
                ).fetchone()
                assert ledger is not None, "正式发布与审批痕未在同事务跨表落账"
                assert INCIDENT["approval_no_for_promote"] in ledger["audit_detail"], "审计缺审批号"
                assert INCIDENT["approval_no_for_promote"] in ledger["history_detail"], "历史缺审批号"
                rule_now = conn.execute(
                    "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()
                assert rule_now["version"] == ver == ledger["version"], "正式规则版本与发布行不一致"

            ev = client.post(
                "/api/evaluate",
                headers=h,
                json={
                    "flag_key": INCIDENT["flag"]["key"],
                    "env_code": INCIDENT["env"]["code"],
                    "subject_key": INCIDENT["subject_key"],
                },
            )
            assert ev.status_code == 200
            assert ev.json()["version"] == ver, "补号升正后求值版本未与发布行对齐"
    finally:
        cleaner.restore()
