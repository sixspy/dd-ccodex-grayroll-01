"""环境目录：标注高风险维度，高风险环境发布必须携带审批号。"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from auth import current_user
from db import connect

router = APIRouter(prefix="/api/envs", tags=["envs"])

HIGH_RISK_LEVEL = 3


def _with_risk_flags(row) -> dict:
    item = dict(row)
    item["high_risk"] = item["risk_level"] >= HIGH_RISK_LEVEL
    item["approval_required"] = item["high_risk"]
    return item


@router.get("")
def list_envs(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        rows = [
            _with_risk_flags(r)
            for r in conn.execute("SELECT * FROM environments ORDER BY risk_level, id").fetchall()
        ]
    return {"items": rows}


@router.get("/{env_id}")
def get_env(env_id: int, authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        row = conn.execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail="环境不存在")
    return _with_risk_flags(row)
