"""发布生效：高风险环境封死乙路径——缺审批号只写影子草案，补号后原子升正。

正确性口径（当日封死，禁止混用/双清）：
- 高风险环境（risk_level>=HIGH_RISK_LEVEL，即 prod）下，publish 永远不直接写正式册；
  缺审批号只允许落 release_shadows 草案，正式区 releases/flag_rules 零增。
- 审批号与正式发布必须同票提交：promote 在同一事务内写正式规则、releases、
  审计与历史（审批号进 audit_log/change_history），跨表联查账实，整体回滚。
- 互斥占位键「按开关加环境维度的发布互斥」：flag-release:{fid}:{eid}，
  进程内先到先得，落败会话只留 contention_marks 争用痕，绝不记正式成功。
- 低风险环境维持既有甲路径同事务正式发布，保证既有行为不回归。
"""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from audit_svc import write_audit, write_history
from auth import current_user, require_writer
from db import connect
from locks import acquire, release
from policies import assert_flag_publishable, assert_not_killed_or_allow_kill, describe_path
from validators import require_int, require_percent, require_rule_kind, require_str

router = APIRouter(prefix="/api/releases", tags=["releases"])

HIGH_RISK_LEVEL = 3
APPROVAL_PREFIX = "approval_no="


class PublishIn(BaseModel):
    flag_id: int
    env_id: int
    kind: str
    percent: int = 0
    cohort_id: int | None = None
    summary: str
    use_shadow: bool = False
    approval_no: str | None = None


class PromoteIn(BaseModel):
    approval_no: str | None = None


# 进程级发布互斥注册表：lock_key -> 持有者会话；与 DB row_locks 双层，杜绝无锁双成
_PUBLISH_GUARD: dict[str, str] = {}
_GUARD_LOCK = threading.Lock()


def _is_high_risk(conn, env_id: int) -> bool:
    row = conn.execute("SELECT risk_level FROM environments WHERE id=?", (env_id,)).fetchone()
    return bool(row and row["risk_level"] >= HIGH_RISK_LEVEL)


def _approval_of(detail: str | None) -> str:
    """从同票审计/历史 detail 中解析审批号；无审批痕返回空串。"""
    if not detail:
        return ""
    marker = APPROVAL_PREFIX
    if marker not in detail:
        return ""
    tail = detail.split(marker, 1)[1]
    return tail.split("|", 1)[0].strip()


def _require_approval(approval_no: str | None) -> str:
    """升正补号校验：缺号/空白号一律 400 取舍叙述（与 409 争用叙述分开）。"""
    no = (approval_no or "").strip()
    if not no:
        raise HTTPException(400, detail="高风险环境升正必须携带审批号，缺号禁止落正式发布")
    return no


class _GuardHeld(Exception):
    pass


def _record_contention(key: str, loser: str, winner: str) -> None:
    """争用痕用独立短连接落库并提交。

    请求主连接在 HTTPException 传播、with 上下文异常退出时会 rollback，
    复用主连接会把争用痕一并回滚，故必须独立提交，确保败方留痕且零正式写。
    """
    with connect() as mark_conn:
        mark_conn.execute(
            "INSERT INTO contention_marks(lock_key, loser, winner, created_at) VALUES(?,?,?,?)",
            (key, loser, winner, time.time()),
        )
        mark_conn.commit()


def _try_guard(conn, key: str, holder: str) -> None:
    """按开关加环境维度的进程互斥：抢不到只留争用痕并以 409 拒绝，不写任何成功。"""
    with _GUARD_LOCK:
        winner = _PUBLISH_GUARD.get(key)
        if winner and winner != holder:
            _record_contention(key, holder, winner)
            raise HTTPException(409, detail="发布锁争用，请稍后重试")
        _PUBLISH_GUARD[key] = holder


def _drop_guard(key: str, holder: str) -> None:
    with _GUARD_LOCK:
        if _PUBLISH_GUARD.get(key) == holder:
            _PUBLISH_GUARD.pop(key, None)


@router.get("")
def list_releases(authorization: str | None = Header(default=None)):
    current_user(authorization)
    with connect() as conn:
        official = [
            dict(r)
            for r in conn.execute(
                """
                SELECT rel.*, f.key AS flag_key, e.code AS env_code, e.risk_level,
                       (SELECT a.detail FROM audit_log a
                         WHERE a.entity='release' AND a.entity_id=rel.id
                         ORDER BY a.id DESC LIMIT 1) AS audit_detail
                FROM releases rel
                JOIN flags f ON f.id=rel.flag_id
                JOIN environments e ON e.id=rel.env_id
                WHERE rel.official=1
                ORDER BY rel.id DESC
                """
            ).fetchall()
        ]
        for row in official:
            row["approval_no"] = _approval_of(row.pop("audit_detail", None))
            row["approval_required"] = row["risk_level"] >= HIGH_RISK_LEVEL
        shadows = [dict(r) for r in conn.execute("SELECT * FROM release_shadows ORDER BY id DESC").fetchall()]
    return {"official": official, "shadows": shadows}


@router.post("/publish")
def publish(body: PublishIn, authorization: str | None = Header(default=None)):
    """高风险封死乙路径：缺号只落影子草案；低风险维持甲路径同事务正式发布。"""
    user = current_user(authorization)
    require_writer(user)
    fid = require_int(body.flag_id, "开关", 1)
    eid = require_int(body.env_id, "环境", 1)
    kind = require_rule_kind(body.kind)
    pct = require_percent(body.percent)
    summary = require_str(body.summary, "发布说明", 4)
    key = f"flag-release:{fid}:{eid}"
    holder = f"{user['username']}:{threading.get_ident()}"
    with connect() as conn:
        _try_guard(conn, key, holder)
        try:
            acquire(conn, key, user["username"])
            try:
                fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
                if not fl:
                    raise HTTPException(404, detail="开关不存在")
                assert_flag_publishable(fl["status"])
                assert_not_killed_or_allow_kill(fl["status"])
                if not conn.execute("SELECT 1 FROM environments WHERE id=?", (eid,)).fetchone():
                    raise HTTPException(404, detail="环境不存在")
                if body.cohort_id and not conn.execute(
                    "SELECT 1 FROM cohorts WHERE id=?", (body.cohort_id,)
                ).fetchone():
                    raise HTTPException(404, detail="人群不存在")
                now = time.time()
                high_risk = _is_high_risk(conn, eid)

                # 乙路径（高风险强制 + 低风险显式勾选）：只写影子草案，正式区零改
                if body.use_shadow or high_risk:
                    cur = conn.execute(
                        "INSERT INTO release_shadows(flag_id, env_id, summary, kind, percent, cohort_id, status, created_at, created_by) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (fid, eid, summary, kind, pct, body.cohort_id, "open", now, user["username"]),
                    )
                    sid = cur.lastrowid
                    if high_risk:
                        detail = (
                            f"{describe_path('release', True)}|高风险环境缺审批号，仅落影子草案，正式区零改"
                        )
                    else:
                        detail = describe_path("release", True)
                    write_audit(conn, user["username"], "release_shadow", "release_shadow", sid, detail)
                    write_history(
                        conn, user["username"], "release_shadow",
                        f"影子草案(待审批){summary}", flag_id=fid, env_id=eid,
                    )
                    conn.commit()
                    return {
                        "mode": "shadow",
                        "id": sid,
                        "path": describe_path("release", True),
                        "high_risk": high_risk,
                        "approval_required": high_risk,
                    }

                # 低风险甲路径：同事务正式发布（登录与库路径不变）
                old = conn.execute(
                    "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()
                ver = (old["version"] + 1) if old else 1
                if old:
                    conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
                conn.execute(
                    "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                    "VALUES(?,?,?,?,?,?,1,?)",
                    (fid, eid, kind, pct, body.cohort_id, ver, now),
                )
                cur = conn.execute(
                    "INSERT INTO releases(flag_id, env_id, version, summary, status, official, published_at, published_by) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (fid, eid, ver, summary, "published", 1, now, user["username"]),
                )
                rid = cur.lastrowid
                if fl["status"] in ("draft", "killed"):
                    conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))
                write_audit(conn, user["username"], "publish", "release", rid, describe_path("release", False))
                write_history(conn, user["username"], "publish", f"v{ver} {summary}", flag_id=fid, env_id=eid)
                conn.commit()
                return {"mode": "official", "id": rid, "version": ver, "path": describe_path("release", False)}
            finally:
                release(conn, key, user["username"])
                conn.commit()
        finally:
            _drop_guard(key, holder)


@router.post("/shadow/{shadow_id}/promote")
def promote_release_shadow(
    shadow_id: int,
    body: PromoteIn | None = None,
    authorization: str | None = Header(default=None),
):
    """补号后原子升正：审批号与正式发布同票提交，跨表联查账实，缺号正式区零改。"""
    user = current_user(authorization)
    require_writer(user)
    payload = body or PromoteIn()
    with connect() as conn:
        sh = conn.execute("SELECT * FROM release_shadows WHERE id=?", (shadow_id,)).fetchone()
        if not sh or sh["status"] != "open":
            raise HTTPException(400, detail="影子发布不可升正")
        fid, eid = sh["flag_id"], sh["env_id"]
        high_risk = _is_high_risk(conn, eid)
        # 高风险升正必须带审批号（400 取舍叙述，区别于 409 争用）
        approval_no = _require_approval(payload.approval_no) if high_risk else (payload.approval_no or "").strip()
        key = f"flag-release:{fid}:{eid}"
        holder = f"{user['username']}:{threading.get_ident()}"
        _try_guard(conn, key, holder)
        try:
            acquire(conn, key, user["username"])
            try:
                fl = conn.execute("SELECT * FROM flags WHERE id=?", (fid,)).fetchone()
                assert_flag_publishable(fl["status"])
                assert_not_killed_or_allow_kill(fl["status"])
                now = time.time()
                old = conn.execute(
                    "SELECT * FROM flag_rules WHERE flag_id=? AND env_id=? AND official=1", (fid, eid)
                ).fetchone()
                ver = (old["version"] + 1) if old else 1
                if old:
                    conn.execute("DELETE FROM flag_rules WHERE id=?", (old["id"],))
                conn.execute(
                    "INSERT INTO flag_rules(flag_id, env_id, kind, percent, cohort_id, version, official, updated_at) "
                    "VALUES(?,?,?,?,?,?,1,?)",
                    (fid, eid, sh["kind"], sh["percent"], sh["cohort_id"], ver, now),
                )
                cur = conn.execute(
                    "INSERT INTO releases(flag_id, env_id, version, summary, status, official, published_at, published_by) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (fid, eid, ver, sh["summary"], "published", 1, now, user["username"]),
                )
                rid = cur.lastrowid
                conn.execute("UPDATE release_shadows SET status='promoted' WHERE id=?", (shadow_id,))
                if fl["status"] in ("draft", "killed"):
                    conn.execute("UPDATE flags SET status='active' WHERE id=?", (fid,))
                # 同票提交：审批号同时进审计与历史，跨表可联查
                approval_tail = f"{APPROVAL_PREFIX}{approval_no}" if approval_no else "approval_no="
                write_history(
                    conn, user["username"], "release_promote",
                    f"影子升正 v{ver} {approval_tail}", flag_id=fid, env_id=eid,
                )
                write_audit(
                    conn, user["username"], "release_promote", "release", rid,
                    f"from_shadow={shadow_id}|{approval_tail}",
                )
                # 同事务跨表账实联查：正式行与审批痕必须同票可见，否则整体回滚
                ledger = conn.execute(
                    """
                    SELECT r.id, a.detail AS audit_detail, h.detail AS history_detail
                    FROM releases r
                    JOIN audit_log a ON a.entity='release' AND a.entity_id=r.id
                    JOIN change_history h ON h.flag_id=r.flag_id AND h.env_id=r.env_id
                        AND h.action='release_promote'
                    WHERE r.id=?
                    """,
                    (rid,),
                ).fetchone()
                if not ledger or _approval_of(ledger["audit_detail"]) != approval_no:
                    raise HTTPException(500, detail="审批号与正式发布未同票落账，事务回滚")
                conn.commit()
                return {
                    "id": rid,
                    "version": ver,
                    "mode": "official",
                    "approval_no": approval_no,
                }
            finally:
                release(conn, key, user["username"])
                conn.commit()
        finally:
            _drop_guard(key, holder)
