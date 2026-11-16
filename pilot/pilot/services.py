"""随机导览试点核心领域服务。

设计要点（与验收口径逐条对应）：

1. 按展区预生成等量 A/B 随机号包，Fisher–Yates 洗牌保证等量且随机；
2. 号段整体分配给设备并 HMAC 签名，已领号段不转拨（池内行只归属一个段）；
3. 回传按 upload_id 幂等，中断重试不重复派号、不重复建分析；
4. 跨设备重复领号 / 同号内容冲突进 disputes 隔离，未解决前拒绝出分析；
5. 所有设备交回未用号后才允许关场；回传合并与关场共用进程锁串行化；
6. ITT 口径：完成者换方案按原分配统计，未完成者保留分母。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import random
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import pilot_home, signing_key
from .stats import chi_square
from .storage import Database

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
VALID_ARMS = ("A", "B")


# ---------------------------------------------------------------- 基础工具
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(conn: sqlite3.Connection, actor: str, action: str,
           entity: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log(at, actor, action, entity, detail) "
        "VALUES (?,?,?,?,?)",
        (now_iso(), actor, action, entity, json.dumps(detail, ensure_ascii=False)),
    )


class PilotError(Exception):
    """可恢复的业务校验错误，消息可直接展示。"""


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


# ---------------------------------------------------------------- 批次与号包
def create_batch(db: Database, zone: str, qty_per_arm: int,
                 seed: Optional[int] = None, actor: str = "admin") -> dict:
    if not zone or not str(zone).strip():
        raise PilotError("展区名称不能为空")
    if not isinstance(qty_per_arm, int) or qty_per_arm < 1:
        raise PilotError("每方案号量必须为正整数")
    if seed is None:
        seed = secrets.randbelow(2**31)
    rng = random.Random(seed)
    total = qty_per_arm * 2

    codes: set[str] = set()
    while len(codes) < total:
        token = "".join(rng.choice(CODE_ALPHABET) for _ in range(8))
        codes.add(f"{zone[:2]}-{token}")
    code_list = list(codes)
    arms = ["A"] * qty_per_arm + ["B"] * qty_per_arm
    rng.shuffle(arms)  # 等量两方案，整体随机化

    batch_id = "B" + uuid.uuid4().hex[:10]
    created_at = now_iso()
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO batches(id, zone, qty_per_arm, seed, status, created_at) "
            "VALUES (?,?,?,?, 'open', ?)",
            (batch_id, zone.strip(), qty_per_arm, seed, created_at),
        )
        conn.executemany(
            "INSERT INTO number_pool(batch_id, code, arm) VALUES (?,?,?)",
            [(batch_id, code, arm) for code, arm in zip(code_list, arms)],
        )
        _audit(conn, actor, "create_batch", batch_id,
               {"zone": zone.strip(), "qty_per_arm": qty_per_arm, "seed": seed})
    return get_batch_detail(db, batch_id)


def allocate_segment(db: Database, batch_id: str, device_id: str,
                     size: Optional[int] = None, actor: str = "admin") -> dict:
    """把一批未分配号码整体号段分给设备，生成签名离线号包。"""
    if not device_id or not str(device_id).strip():
        raise PilotError("设备编号不能为空")
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute("SELECT * FROM batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise PilotError(f"批次 {batch_id} 不存在")
        if batch["status"] != "open":
            raise PilotError("批次已关场，不能再分配号段")
        existed = conn.execute(
            "SELECT id FROM segments WHERE batch_id=? AND device_id=?",
            (batch_id, device_id)).fetchone()
        if existed is not None:
            raise PilotError(f"设备 {device_id} 在本批次已持有号段，已领号段不转拨")

        remaining = conn.execute(
            "SELECT * FROM number_pool WHERE batch_id=? AND segment_id IS NULL "
            "ORDER BY id", (batch_id,)).fetchall()
        if size is None:
            size = len(remaining)
        if not isinstance(size, int) or size <= 0:
            raise PilotError("号段大小必须为正整数")
        if size > len(remaining):
            raise PilotError(
                f"剩余可分配号码 {len(remaining)} 个，无法分配 {size} 个")
        chosen = remaining[:size]
        segment_id = "S" + uuid.uuid4().hex[:10]
        assigned_at = now_iso()
        conn.execute(
            "INSERT INTO segments(id, batch_id, device_id, status, assigned_at) "
            "VALUES (?,?,?, 'assigned', ?)",
            (segment_id, batch_id, device_id, assigned_at),
        )
        conn.executemany(
            "UPDATE number_pool SET segment_id=? WHERE id=?",
            [(segment_id, row["id"]) for row in chosen],
        )
        package = {
            "package_version": 1,
            "batch_id": batch_id,
            "zone": batch["zone"],
            "segment_id": segment_id,
            "device_id": device_id,
            "assigned_at": assigned_at,
            "numbers": [{"code": r["code"], "arm": r["arm"]} for r in chosen],
        }
        package["signature"] = _sign_package(package)
        pkg_dir = pilot_home() / "packages"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_path = pkg_dir / f"{segment_id}.json"
        pkg_path.write_text(json.dumps(package, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        conn.execute(
            "UPDATE segments SET package_path=?, package_sig=? WHERE id=?",
            (str(pkg_path), package["signature"], segment_id))
        _audit(conn, actor, "allocate_segment", segment_id,
               {"batch_id": batch_id, "device_id": device_id, "size": size})
    return package


def _sign_package(package: dict, key: bytes | None = None) -> str:
    body = json.dumps({k: v for k, v in package.items() if k != "signature"},
                      sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hmac.new(key or signing_key(), body, hashlib.sha256).hexdigest()


def verify_package(package: dict, key: bytes | None = None) -> bool:
    sig = package.get("signature", "")
    expected = _sign_package({k: v for k, v in package.items()
                              if k != "signature"}, key)
    return hmac.compare_digest(sig, expected)


# ---------------------------------------------------------------- 回传合并
def merge_upload(db: Database, payload: dict) -> dict:
    """合并一次设备回传；幂等、可重试，冲突隔离而非覆盖。"""
    upload_id = payload.get("upload_id")
    batch_id = payload.get("batch_id")
    segment_id = payload.get("segment_id")
    device_id = payload.get("device_id")
    events = payload.get("events", [])
    if not all([upload_id, batch_id, segment_id, device_id]):
        raise PilotError("回传缺少 upload_id/batch_id/segment_id/device_id")
    if not isinstance(events, list):
        raise PilotError("events 必须为数组")

    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        # 幂等：同一 upload_id 重放直接返回首次结果，不产生任何副作用
        prior = conn.execute("SELECT result FROM uploads WHERE upload_id=?",
                             (upload_id,)).fetchone()
        if prior is not None:
            result = json.loads(prior["result"])
            result["replayed"] = True
            return result

        batch = conn.execute("SELECT * FROM batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise PilotError(f"批次 {batch_id} 不存在")
        if batch["status"] != "open":
            raise PilotError("批次已关场，回传通道关闭；请联系管理员核账后处理")
        segment = conn.execute(
            "SELECT * FROM segments WHERE id=? AND batch_id=? AND device_id=?",
            (segment_id, batch_id, device_id)).fetchone()
        if segment is None:
            raise PilotError("号段与设备/批次不匹配，拒绝回传")
        if segment["status"] != "assigned":
            raise PilotError("号段已交回，交回后的回传不予受理")

        accepted, replayed, disputes = 0, 0, []
        seen_event_ids: set[str] = set()
        for ev in events:
            tag = _process_event(conn, batch_id, segment_id, device_id,
                                 ev, seen_event_ids)
            if tag[0] == "accepted":
                accepted += 1
            elif tag[0] == "replayed":
                replayed += 1
            else:
                disputes.append(tag[1])

        result = {
            "upload_id": upload_id,
            "batch_id": batch_id,
            "segment_id": segment_id,
            "device_id": device_id,
            "accepted": accepted,
            "replayed": replayed,
            "quarantined": len(disputes),
            "disputes": disputes,
        }
        conn.execute(
            "INSERT INTO uploads(upload_id, batch_id, segment_id, device_id, "
            "event_count, received_at, result) VALUES (?,?,?,?,?,?,?)",
            (upload_id, batch_id, segment_id, device_id, len(events),
             now_iso(), json.dumps(result, ensure_ascii=False)))
        _audit(conn, device_id, "merge_upload", upload_id,
               {"accepted": accepted, "replayed": replayed,
                "quarantined": len(disputes)})
        return dict(result)


def _open_dispute(conn: sqlite3.Connection, batch_id: str, code: str,
                  kind: str, detail: dict) -> dict:
    record = {"code": code, "kind": kind, "detail": detail,
              "created_at": now_iso()}
    conn.execute(
        "INSERT INTO disputes(batch_id, code, kind, status, detail, created_at) "
        "VALUES (?,?,?, 'open', ?, ?)",
        (batch_id, code, kind,
         json.dumps(detail, ensure_ascii=False), record["created_at"]))
    return record


def _process_event(conn: sqlite3.Connection, batch_id: str, segment_id: str,
                   device_id: str, ev: dict, seen_event_ids: set) -> tuple:
    event_id = ev.get("event_id")
    code = ev.get("code")
    visit_id = ev.get("visit_id")
    arm_used = ev.get("arm_used")
    event_type = ev.get("event_type")
    occurred_at = ev.get("occurred_at", now_iso())
    if not event_id or not code or event_type not in ("issue", "complete"):
        return ("dispute", _open_dispute(
            conn, batch_id, str(code), "content_conflict",
            {"reason": "事件结构不合法", "event": ev, "device_id": device_id}))
    if arm_used is not None and arm_used not in VALID_ARMS:
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "方案取值非法", "event_id": event_id,
             "arm_used": arm_used, "device_id": device_id}))

    # 同设备同 event_id：重试/重放，幂等跳过
    if event_id in seen_event_ids:
        return ("replayed", None)
    seen_event_ids.add(event_id)
    dup = conn.execute("SELECT 1 FROM events WHERE device_id=? AND event_id=?",
                       (device_id, event_id)).fetchone()
    if dup is not None:
        return ("replayed", None)

    pool = conn.execute("SELECT * FROM number_pool WHERE batch_id=? AND code=?",
                        (batch_id, code)).fetchone()
    if pool is None:
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "号码不属于本批次", "event_id": event_id,
             "device_id": device_id, "visit_id": visit_id}))
    if pool["segment_id"] != segment_id:
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "号码不属于本设备号段，疑似跨设备重复领号",
             "event_id": event_id, "device_id": device_id,
             "owner_segment": pool["segment_id"], "visit_id": visit_id}))

    if event_type == "issue":
        return _process_issue(conn, batch_id, pool, device_id, event_id,
                              code, visit_id, arm_used, occurred_at)
    return _process_complete(conn, batch_id, pool, device_id, event_id,
                             code, visit_id, arm_used, occurred_at)


def _process_issue(conn, batch_id, pool, device_id, event_id, code,
                   visit_id, arm_used, occurred_at) -> tuple:
    if pool["state"] == "reclaimed":
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "号码已随号段交回回收，不能再派",
             "event_id": event_id, "device_id": device_id}))
    if pool["state"] in ("issued", "completed"):
        if visit_id and pool["visit_id"] == visit_id:
            return ("replayed", None)  # 同次到访重传，幂等
        return ("dispute", _open_dispute(
            conn, batch_id, code, "duplicate_issue",
            {"reason": "号码已被领取，再次领号",
             "event_id": event_id, "device_id": device_id,
             "existing_visit": pool["visit_id"], "new_visit": visit_id}))
    # 同次到访是否已在别的号码上领过（含跨设备）
    if visit_id:
        other = conn.execute(
            "SELECT code FROM number_pool WHERE batch_id=? AND visit_id=? "
            "AND state IN ('issued','completed') AND code != ?",
            (batch_id, visit_id, code)).fetchone()
        if other is not None:
            return ("dispute", _open_dispute(
                conn, batch_id, code, "duplicate_issue",
                {"reason": "同次到访跨设备/跨号重复领号",
                 "event_id": event_id, "device_id": device_id,
                 "visit_id": visit_id, "already_issued_code": other["code"]}))
    conn.execute(
        "UPDATE number_pool SET state='issued', visit_id=?, issued_at=? "
        "WHERE id=?", (visit_id, occurred_at, pool["id"]))
    conn.execute(
        "INSERT INTO events(batch_id, segment_id, device_id, event_id, code, "
        "visit_id, arm_used, event_type, occurred_at, received_at) "
        "VALUES (?,?,?,?,?,?,?, 'issue', ?,?)",
        (batch_id, pool["segment_id"], device_id, event_id, code, visit_id,
         arm_used, occurred_at, now_iso()))
    return ("accepted", None)


def _process_complete(conn, batch_id, pool, device_id, event_id, code,
                      visit_id, arm_used, occurred_at) -> tuple:
    if pool["state"] == "unused" or pool["state"] == "reclaimed":
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "未派号先报完成，状态冲突",
             "event_id": event_id, "device_id": device_id,
             "state": pool["state"], "visit_id": visit_id}))
    if pool["state"] == "completed":
        if visit_id and pool["visit_id"] == visit_id:
            return ("replayed", None)
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "号码已完成，完成内容冲突",
             "event_id": event_id, "device_id": device_id,
             "existing_visit": pool["visit_id"], "new_visit": visit_id}))
    if visit_id and pool["visit_id"] and visit_id != pool["visit_id"]:
        return ("dispute", _open_dispute(
            conn, batch_id, code, "content_conflict",
            {"reason": "完成到访编号与派号绑定不一致",
             "event_id": event_id, "device_id": device_id,
             "bound_visit": pool["visit_id"], "sent_visit": visit_id}))
    conn.execute(
        "UPDATE number_pool SET state='completed', completed_at=?, arm_used=? "
        "WHERE id=?", (occurred_at, arm_used, pool["id"]))
    conn.execute(
        "INSERT INTO events(batch_id, segment_id, device_id, event_id, code, "
        "visit_id, arm_used, event_type, occurred_at, received_at) "
        "VALUES (?,?,?,?,?,?,?, 'complete', ?,?)",
        (batch_id, pool["segment_id"], device_id, event_id, code, visit_id,
         arm_used, occurred_at, now_iso()))
    return ("accepted", None)


# ---------------------------------------------------------------- 交回与关场
def return_segment(db: Database, batch_id: str, segment_id: str,
                   actor: Optional[str] = None) -> dict:
    """设备交回号段：未用号回收；已派号保留（分母不丢）。"""
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        segment = conn.execute("SELECT * FROM segments WHERE id=? AND batch_id=?",
                               (segment_id, batch_id)).fetchone()
        if segment is None:
            raise PilotError("号段不存在或不属于该批次")
        batch = conn.execute("SELECT status FROM batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch["status"] != "open":
            raise PilotError("批次已关场")
        if segment["status"] == "returned":
            return _segment_summary(conn, segment_id)  # 幂等
        rows = conn.execute(
            "SELECT state, COUNT(*) AS n FROM number_pool WHERE segment_id=? "
            "GROUP BY state", (segment_id,)).fetchall()
        counts = {r["state"]: r["n"] for r in rows}
        conn.execute(
            "UPDATE number_pool SET state='reclaimed' WHERE segment_id=? "
            "AND state='unused'", (segment_id,))
        conn.execute(
            "UPDATE segments SET status='returned', returned_at=? WHERE id=?",
            (now_iso(), segment_id))
        _audit(conn, actor or segment["device_id"], "return_segment",
               segment_id, {"batch_id": batch_id, "counts": counts})
        return _segment_summary(conn, segment_id)


def _segment_summary(conn: sqlite3.Connection, segment_id: str) -> dict:
    segment = conn.execute("SELECT * FROM segments WHERE id=?",
                           (segment_id,)).fetchone()
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM number_pool WHERE segment_id=? "
        "GROUP BY state", (segment_id,)).fetchall()
    summary = _row_to_dict(segment)
    summary["counts"] = {r["state"]: r["n"] for r in rows}
    return summary


def close_batch(db: Database, batch_id: str, actor: str = "admin") -> dict:
    """关场：所有号段交回后才可关闭；与回传并发时靠锁串行，不漏人。"""
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute("SELECT * FROM batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise PilotError(f"批次 {batch_id} 不存在")
        if batch["status"] == "closed":
            return get_batch_detail(db, batch_id)  # 幂等
        unreturned = conn.execute(
            "SELECT device_id FROM segments WHERE batch_id=? AND status!='returned'",
            (batch_id,)).fetchall()
        if unreturned:
            raise PilotError(
                "仍有设备未交回号段，不能关场："
                + ", ".join(r["device_id"] for r in unreturned))
        conn.execute(
            "UPDATE batches SET status='closed', closed_at=? WHERE id=?",
            (now_iso(), batch_id))
        _audit(conn, actor, "close_batch", batch_id, {})
    return get_batch_detail(db, batch_id)


# ---------------------------------------------------------------- 争议与分析
def resolve_dispute(db: Database, dispute_id: int, resolution: str,
                    actor: str = "admin") -> dict:
    if not resolution or not str(resolution).strip():
        raise PilotError("处理结论不能为空")
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM disputes WHERE id=?",
                           (dispute_id,)).fetchone()
        if row is None:
            raise PilotError("争议不存在")
        if row["status"] == "resolved":
            return _row_to_dict(row)
        conn.execute(
            "UPDATE disputes SET status='resolved', resolved_at=?, resolution=? "
            "WHERE id=?", (now_iso(), resolution.strip(), dispute_id))
        _audit(conn, actor, "resolve_dispute", str(dispute_id),
               {"resolution": resolution.strip()})
        row = conn.execute("SELECT * FROM disputes WHERE id=?",
                           (dispute_id,)).fetchone()
        return _row_to_dict(row)


def create_analysis(db: Database, batch_id: str, actor: str = "analyst") -> dict:
    """关场且无未决争议后生成 ITT 分析；重试幂等，不重复建分析。"""
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        batch = conn.execute("SELECT * FROM batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise PilotError(f"批次 {batch_id} 不存在")
        if batch["status"] != "closed":
            raise PilotError("批次未关场，不能生成分析")
        open_count = conn.execute(
            "SELECT COUNT(*) AS n FROM disputes WHERE batch_id=? AND status='open'",
            (batch_id,)).fetchone()["n"]
        if open_count:
            raise PilotError(f"仍有 {open_count} 条未决争议，未解决前整场不出结论")
        existing = conn.execute("SELECT * FROM analyses WHERE batch_id=?",
                                (batch_id,)).fetchone()
        if existing is not None:
            out = _row_to_dict(existing)
            out["snapshot"] = json.loads(existing["snapshot"])
            out["idempotent_replay"] = True
            return out

        rows = conn.execute(
            "SELECT arm, state, COUNT(*) AS n FROM number_pool "
            "WHERE batch_id=? GROUP BY arm, state",
            (batch_id,)).fetchall()
        tally = {a: {"issued": 0, "completed": 0, "reclaimed": 0,
                     "unused": 0} for a in VALID_ARMS}
        for r in rows:
            tally[r["arm"]][r["state"]] = r["n"]
        # ITT 分母 = 实际派出的号码；未完成者保留分母
        denominators = {a: tally[a]["issued"] + tally[a]["completed"]
                        for a in VALID_ARMS}
        observed = [
            [tally["A"]["completed"], tally["A"]["issued"]],
            [tally["B"]["completed"], tally["B"]["issued"]],
        ]
        stats: dict
        stats_error = None
        if sum(denominators.values()) == 0:
            stats_error = "本批次没有任何已派号码，无法估计统计量"
            stats = {}
        else:
            try:
                stats = chi_square(observed)
            except ValueError as exc:
                stats_error = str(exc)
                stats = {}
        switchers = conn.execute(
            "SELECT e.arm_used, p.arm AS assigned_arm, COUNT(*) AS n "
            "FROM events e JOIN number_pool p ON p.batch_id=e.batch_id "
            "AND p.code=e.code WHERE e.batch_id=? AND e.event_type='complete' "
            "AND e.arm_used IS NOT NULL AND e.arm_used != p.arm "
            "GROUP BY e.arm_used, p.arm", (batch_id,)).fetchall()
        snapshot = {
            "batch_id": batch_id,
            "zone": batch["zone"],
            "generated_at": now_iso(),
            "principle": "ITT：完成者即使换方案也按号码原分配统计，未完成者保留分母",
            "tally": tally,
            "denominators": denominators,
            "contingency_table": {
                "rows": ["A", "B"],
                "columns": ["completed", "incomplete"],
                "observed": observed,
            },
            "completion_rate": {
                a: (round(tally[a]["completed"] / denominators[a], 6)
                    if denominators[a] else None) for a in VALID_ARMS},
            "stats": stats,
            "stats_error": stats_error,
            "arm_switchers": [_row_to_dict(r) for r in switchers],
            "method": "Pearson χ²（无连续性校正），Cramer V=√(χ²/N)",
            "limitations": [
                "现场随机试点样本量有限，注意最小期望频数提示",
                "关联结果不等同于方案因果效果",
                "争议隔离记录不进入统计口径",
            ],
        }
        analysis_id = "R" + uuid.uuid4().hex[:10]
        conn.execute(
            "INSERT INTO analyses(id, batch_id, created_at, snapshot) "
            "VALUES (?,?,?,?)",
            (analysis_id, batch_id, snapshot["generated_at"],
             json.dumps(snapshot, ensure_ascii=False)))
        _audit(conn, actor, "create_analysis", analysis_id,
               {"batch_id": batch_id})
        row = conn.execute("SELECT * FROM analyses WHERE id=?",
                           (analysis_id,)).fetchone()
        out = _row_to_dict(row)
        out["snapshot"] = snapshot
        return out


def approve_analysis(db: Database, batch_id: str, approver: str,
                     note: str = "") -> dict:
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM analyses WHERE batch_id=?",
                           (batch_id,)).fetchone()
        if row is None:
            raise PilotError("分析不存在，请先生成")
        if row["approved_at"] is None:
            conn.execute(
                "UPDATE analyses SET approved_at=?, approver=?, note=? WHERE id=?",
                (now_iso(), approver, note, row["id"]))
            _audit(conn, approver, "approve_analysis", row["id"], {"note": note})
        row = conn.execute("SELECT * FROM analyses WHERE id=?",
                           (row["id"],)).fetchone()
        out = _row_to_dict(row)
        out["snapshot"] = json.loads(row["snapshot"])
        return out


def mark_distributed(db: Database, batch_id: str, actor: str = "admin") -> dict:
    with db.lock, db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM analyses WHERE batch_id=?",
                           (batch_id,)).fetchone()
        if row is None:
            raise PilotError("分析不存在")
        if row["approved_at"] is None:
            raise PilotError("分析未批准，不能分发")
        if row["distributed_at"] is None:
            conn.execute("UPDATE analyses SET distributed_at=? WHERE id=?",
                         (now_iso(), row["id"]))
            _audit(conn, actor, "distribute_analysis", row["id"], {})
        row = conn.execute("SELECT * FROM analyses WHERE id=?",
                           (row["id"],)).fetchone()
        out = _row_to_dict(row)
        out["snapshot"] = json.loads(row["snapshot"])
        return out


# ---------------------------------------------------------------- 查询页面
def list_batches(db: Database) -> list[dict]:
    with db.connect() as conn:
        batches = conn.execute(
            "SELECT * FROM batches ORDER BY created_at DESC").fetchall()
        result = []
        for b in batches:
            item = _row_to_dict(b)
            item["pool"] = {r["state"]: r["n"] for r in conn.execute(
                "SELECT state, COUNT(*) AS n FROM number_pool "
                "WHERE batch_id=? GROUP BY state", (b["id"],)).fetchall()}
            item["segments"] = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM segments "
                "WHERE batch_id=? GROUP BY status", (b["id"],)).fetchall()}
            item["open_disputes"] = conn.execute(
                "SELECT COUNT(*) AS n FROM disputes WHERE batch_id=? "
                "AND status='open'", (b["id"],)).fetchone()["n"]
            item["has_analysis"] = conn.execute(
                "SELECT 1 FROM analyses WHERE batch_id=?",
                (b["id"],)).fetchone() is not None
            result.append(item)
        return result


def get_batch_detail(db: Database, batch_id: str) -> dict:
    with db.connect() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise PilotError(f"批次 {batch_id} 不存在")
        out = _row_to_dict(batch)
        out["pool_by_arm_state"] = [_row_to_dict(r) for r in conn.execute(
            "SELECT arm, state, COUNT(*) AS n FROM number_pool "
            "WHERE batch_id=? GROUP BY arm, state ORDER BY arm, state",
            (batch_id,)).fetchall()]
        out["segments"] = [_segment_summary(conn, r["id"]) for r in conn.execute(
            "SELECT id FROM segments WHERE batch_id=? ORDER BY assigned_at",
            (batch_id,)).fetchall()]
        out["disputes"] = [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM disputes WHERE batch_id=? ORDER BY id",
            (batch_id,)).fetchall()]
        analysis = conn.execute("SELECT * FROM analyses WHERE batch_id=?",
                                (batch_id,)).fetchone()
        if analysis is not None:
            a = _row_to_dict(analysis)
            a["snapshot"] = json.loads(analysis["snapshot"])
            out["analysis"] = a
        out["uploads"] = [
            {k: r[k] for k in ("upload_id", "device_id", "event_count",
                               "received_at", "result")}
            for r in conn.execute(
                "SELECT * FROM uploads WHERE batch_id=? ORDER BY received_at",
                (batch_id,)).fetchall()]
        return out


def list_disputes(db: Database, only_open: bool = False) -> list[dict]:
    sql = "SELECT * FROM disputes"
    if only_open:
        sql += " WHERE status='open'"
    sql += " ORDER BY id DESC"
    with db.connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(sql).fetchall()]


def audit_trail(db: Database, limit: int = 100) -> list[dict]:
    with db.connect() as conn:
        return [_row_to_dict(r) for r in conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()]
