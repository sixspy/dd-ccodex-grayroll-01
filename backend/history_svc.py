"""变更历史：高风险发布的审批号与正式发布同票可追溯，争用痕不在此列。"""
from __future__ import annotations

from fastapi import APIRouter, Header

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/history", tags=["history"])

APPROVAL_PREFIX = "approval_no="


def _approval_no(detail: str | None) -> str:
    if not detail or APPROVAL_PREFIX not in detail:
        return ""
    return detail.split(APPROVAL_PREFIX, 1)[1].split("|", 1)[0].strip()


@router.get("")
def list_history(authorization: str | None = Header(default=None), limit: int = 100):
    current_user(authorization)
    with connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT h.*, f.key AS flag_key, e.code AS env_code, e.risk_level
                FROM change_history h
                LEFT JOIN flags f ON f.id=h.flag_id
                LEFT JOIN environments e ON e.id=h.env_id
                ORDER BY h.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
    for row in rows:
        high_risk = (row.get("risk_level") or 0) >= 3
        row["high_risk"] = high_risk
        row["approval_required"] = high_risk and row["action"] in ("release_promote", "publish", "release_shadow")
        row["approval_no"] = _approval_no(row.get("detail"))
        if row["action"] == "release_shadow":
            row["pending_approval"] = True
        elif row["action"] == "release_promote":
            row["pending_approval"] = False
    return {"items": rows}
