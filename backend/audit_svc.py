"""审计与变更历史写入：审批号与正式发布同票落 audit_log，并支持跨表账实联查。

争用痕（contention_marks）属于互斥叙述，由 locks 路由单独呈现，与本审计的取舍/审批叙述分开。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Header

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/audit", tags=["audit"])

APPROVAL_PREFIX = "approval_no="
HIGH_RISK_LEVEL = 3


def _approval_no(detail: str | None) -> str:
    if not detail or APPROVAL_PREFIX not in detail:
        return ""
    return detail.split(APPROVAL_PREFIX, 1)[1].split("|", 1)[0].strip()


def write_audit(conn, actor: str, action: str, entity: str, entity_id: int | None, detail: str) -> None:
    conn.execute(
        "INSERT INTO audit_log(actor, action, entity, entity_id, detail, created_at) VALUES(?,?,?,?,?,?)",
        (actor, action, entity, entity_id, detail, time.time()),
    )


def write_history(conn, actor: str, action: str, detail: str, flag_id: int | None = None, env_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO change_history(flag_id, env_id, action, detail, actor, created_at) VALUES(?,?,?,?,?,?)",
        (flag_id, env_id, action, detail, actor, time.time()),
    )


@router.get("")
def list_audit(authorization: str | None = Header(default=None), limit: int = 80):
    current_user(authorization)
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    items = [dict(r) for r in rows]
    for row in items:
        row["approval_no"] = _approval_no(row.get("detail"))
    return {"items": items}


@router.get("/approvals")
def approval_ledger(authorization: str | None = Header(default=None)):
    """跨表账实联查：高风险正式发布行必须与同票审批痕一一对上。"""
    current_user(authorization)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT r.id AS release_id, r.flag_id, r.env_id, r.version, r.status,
                       r.published_by, f.key AS flag_key, e.code AS env_code, e.risk_level,
                       a.id AS audit_id, a.actor, a.detail AS audit_detail,
                       h.id AS history_id, h.detail AS history_detail
                FROM releases r
                JOIN flags f ON f.id=r.flag_id
                JOIN environments e ON e.id=r.env_id
                LEFT JOIN audit_log a ON a.entity='release' AND a.entity_id=r.id
                LEFT JOIN change_history h ON h.flag_id=r.flag_id AND h.env_id=r.env_id
                    AND h.action='release_promote'
                WHERE r.official=1 AND e.risk_level >= ?
                ORDER BY r.id DESC
                """,
                (HIGH_RISK_LEVEL,),
            ).fetchall()
        ]
    for row in rows:
        row["approval_no"] = _approval_of_ledger(row)
        row["ledger_consistent"] = bool(row["approval_no"]) and row["approval_no"] in (row["history_detail"] or "")
    return {"items": rows}


def _approval_of_ledger(row: dict) -> str:
    return _approval_no(row.get("audit_detail"))
