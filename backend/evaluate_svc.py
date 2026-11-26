"""在线求值：给定主体键，返回开关在某环境是否命中。

高风险环境（risk_level>=3）下，正式规则必须能与 releases 及 audit_log 跨表联查到
同票审批号；查不到审批痕的正式规则按未生效 fail-closed 处理。影子草案永不参与求值。
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from auth import current_user
from db import connect
from policies import combine_rule, eval_percent
from validators import require_str

router = APIRouter(prefix="/api/evaluate", tags=["evaluate"])

HIGH_RISK_LEVEL = 3
APPROVAL_PREFIX = "approval_no="


class EvalIn(BaseModel):
    flag_key: str
    env_code: str
    subject_key: str


def _approval_no(detail: str | None) -> str:
    if not detail or APPROVAL_PREFIX not in detail:
        return ""
    return detail.split(APPROVAL_PREFIX, 1)[1].split("|", 1)[0].strip()


def _official_approval(conn, flag_id: int, env_id: int, version: int) -> str:
    """跨表账实联查：正式发布行同票审计痕里的审批号；无账可对返回空串。"""
    row = conn.execute(
        """
        SELECT a.detail AS audit_detail
        FROM releases r
        JOIN audit_log a ON a.entity='release' AND a.entity_id=r.id
        WHERE r.flag_id=? AND r.env_id=? AND r.official=1 AND r.version=? AND r.status='published'
        ORDER BY r.id DESC, a.id DESC
        LIMIT 1
        """,
        (flag_id, env_id, version),
    ).fetchone()
    return _approval_no(row["audit_detail"]) if row else ""


@router.post("")
def evaluate(body: EvalIn, authorization: str | None = Header(default=None)):
    current_user(authorization)
    flag_key = require_str(body.flag_key, "开关键", 2)
    env_code = require_str(body.env_code, "环境", 2)
    subject = require_str(body.subject_key, "主体键", 1)
    with connect() as conn:
        fl = conn.execute("SELECT * FROM flags WHERE key=?", (flag_key,)).fetchone()
        if not fl:
            raise HTTPException(404, detail="开关不存在")
        env = conn.execute("SELECT * FROM environments WHERE code=?", (env_code,)).fetchone()
        if not env:
            raise HTTPException(404, detail="环境不存在")
        if fl["status"] == "killed":
            return {
                "enabled": False,
                "reason": "flag_killed",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        if fl["status"] == "archived":
            return {
                "enabled": False,
                "reason": "flag_archived",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        ov = conn.execute(
            "SELECT * FROM overrides WHERE flag_id=? AND env_id=? AND subject_key=? AND active=1",
            (fl["id"], env["id"], subject),
        ).fetchone()
        if ov:
            enabled = ov["effect"] == "force_on"
            return {
                "enabled": enabled,
                "reason": f"override:{ov['effect']}",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
                "version": None,
            }
        rule = conn.execute(
            "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1",
            (fl["id"], env["id"]),
        ).fetchone()
        if not rule:
            return {
                "enabled": False,
                "reason": "no_official_rule",
                "flag_key": flag_key,
                "env_code": env_code,
                "subject_key": subject,
            }
        # 高风险账实联查：无同票审批痕的正式比例一律不得生效（fail-closed）
        approval_no = ""
        if env["risk_level"] >= HIGH_RISK_LEVEL:
            approval_no = _official_approval(conn, fl["id"], env["id"], rule["version"])
            if not approval_no:
                return {
                    "enabled": False,
                    "reason": "official_rule_pending_approval",
                    "flag_key": flag_key,
                    "env_code": env_code,
                    "subject_key": subject,
                    "version": rule["version"],
                    "approval_required": True,
                }
        percent_hit = eval_percent(subject, int(rule["percent"]), flag_key)
        cohort_hit = False
        if rule["cohort_id"]:
            cohort_hit = bool(
                conn.execute(
                    "SELECT 1 FROM cohort_members WHERE cohort_id=? AND subject_key=?",
                    (rule["cohort_id"], subject),
                ).fetchone()
            )
        enabled = combine_rule(rule["kind"], percent_hit, cohort_hit)
        return {
            "enabled": enabled,
            "reason": "rule",
            "kind": rule["kind"],
            "percent": rule["percent"],
            "percent_hit": percent_hit,
            "cohort_hit": cohort_hit,
            "version": rule["version"],
            "approval_no": approval_no,
            "flag_key": flag_key,
            "env_code": env_code,
            "subject_key": subject,
        }


@router.get("/board")
def eval_board(authorization: str | None = Header(default=None)):
    """总览：各环境正式规则摘要，附高风险审批账实状态。"""
    current_user(authorization)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT f.key AS flag_key, f.status, e.code AS env_code, e.risk_level,
                       r.kind, r.percent, r.version, r.cohort_id,
                       (SELECT a.detail FROM releases rel
                         JOIN audit_log a ON a.entity='release' AND a.entity_id=rel.id
                         WHERE rel.flag_id=f.id AND rel.env_id=e.id AND rel.official=1
                           AND rel.version=r.version AND rel.status='published'
                         ORDER BY rel.id DESC, a.id DESC LIMIT 1) AS audit_detail
                FROM flag_rules r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                WHERE r.official=1
                ORDER BY f.key, e.risk_level
                """
            ).fetchall()
        ]
        for row in rows:
            row["approval_required"] = row["risk_level"] >= HIGH_RISK_LEVEL
            row["approval_no"] = _approval_no(row.pop("audit_detail", None)) if row["approval_required"] else ""
        by_status = [
            dict(x)
            for x in conn.execute("SELECT status, COUNT(*) c FROM flags GROUP BY status").fetchall()
        ]
    return {"rules": rows, "by_status": by_status}
