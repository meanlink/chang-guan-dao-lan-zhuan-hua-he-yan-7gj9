"""现场设备端：完全离线运行的派号终端。

- 号包启动时验签，防篡改；
- 本地 SQLite 记录事件，event_id 由 (device, 本地序号) 决定，
  中断恢复、重复操作都不会重复派号；
- upload 游标记录已确认回传位置，网络重试只重发未确认事件，
  服务端再按 upload_id / event_id 做第二层幂等。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .services import PilotError, verify_package


def _load_verify_key(device_home: Path, key_file: str | Path | None) -> bytes:
    """读取预置在设备侧的共享验签密钥。

    key_file 优先；否则在设备目录找 package.key；都没有则给出明确指引，
    绝不静默生成新密钥（那会让所有合法号包验签失败）。
    """
    candidates = [Path(key_file)] if key_file else [device_home / "package.key"]
    for path in candidates:
        if path.exists():
            return path.read_bytes()
    raise PilotError(
        "设备缺少验签密钥：请先用 `cli export-key` 导出 package.key，"
        "通过可信通道预置到设备目录后再载入号包")

DEVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    package_json TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    next_pos INTEGER NOT NULL DEFAULT 0,
    upload_cursor INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS local_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL,
    visit_id TEXT,
    arm_used TEXT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DeviceClient:
    def __init__(self, device_id: str, home: str | Path,
                 key_file: str | Path | None = None):
        self.device_id = device_id
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path = self.home / f"device_{device_id}.sqlite"
        self.key_file = key_file
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(DEVICE_SCHEMA)

    # ------------------------------------------------------------ 载入号包
    def load_package(self, package: dict) -> dict:
        key = _load_verify_key(self.home, self.key_file)
        if not verify_package(package, key):
            raise PilotError("号包签名校验失败，拒绝载入")
        if package["device_id"] != self.device_id:
            raise PilotError("号包不属于本设备")
        with self._conn() as conn:
            row = conn.execute("SELECT package_json FROM state").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO state(package_json, device_id, batch_id, "
                    "segment_id) VALUES (?,?,?,?)",
                    (json.dumps(package, ensure_ascii=False), self.device_id,
                     package["batch_id"], package["segment_id"]))
            elif json.loads(row["package_json"])["segment_id"] != package["segment_id"]:
                raise PilotError("设备已载入其他号段，已领号段不转拨")
        return self.status()

    def _package(self, conn: sqlite3.Connection) -> dict:
        row = conn.execute("SELECT package_json FROM state").fetchone()
        if row is None:
            raise PilotError("设备尚未载入号包")
        return json.loads(row["package_json"])

    def status(self) -> dict:
        with self._conn() as conn:
            package = self._package(conn)
            st = conn.execute("SELECT * FROM state").fetchone()
            rows = conn.execute(
                "SELECT event_type, COUNT(*) AS n FROM local_events "
                "GROUP BY event_type").fetchall()
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM local_events WHERE synced=0"
            ).fetchone()["n"]
            return {
                "device_id": self.device_id,
                "batch_id": package["batch_id"],
                "zone": package["zone"],
                "segment_id": package["segment_id"],
                "total_numbers": len(package["numbers"]),
                "next_pos": st["next_pos"],
                "remaining": len(package["numbers"]) - st["next_pos"],
                "upload_cursor": st["upload_cursor"],
                "pending_upload": pending,
                "events": {r["event_type"]: r["n"] for r in rows},
            }

    # ------------------------------------------------------------ 离线派号
    def issue(self, visit_id: str) -> dict:
        if not visit_id:
            raise PilotError("到访编号不能为空")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            package = self._package(conn)
            # 同次到访重复操作：返回原号，不新增事件、不推进游标
            prior = conn.execute(
                "SELECT * FROM local_events WHERE visit_id=? AND "
                "event_type='issue' ORDER BY seq LIMIT 1", (visit_id,)).fetchone()
            if prior is not None:
                return {"code": prior["code"], "arm": _arm_of(package, prior["code"]),
                        "visit_id": visit_id, "idempotent_replay": True}
            st = conn.execute("SELECT * FROM state").fetchone()
            if st["next_pos"] >= len(package["numbers"]):
                raise PilotError("本设备号段已派完")
            entry = package["numbers"][st["next_pos"]]
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM local_events"
            ).fetchone()["s"]
            event_id = f"{self.device_id}-EV{seq:06d}"
            conn.execute(
                "INSERT INTO local_events(event_id, code, visit_id, event_type, "
                "occurred_at) VALUES (?,?,?, 'issue', ?)",
                (event_id, entry["code"], visit_id, _now()))
            conn.execute("UPDATE state SET next_pos=next_pos+1")
            return {"code": entry["code"], "arm": entry["arm"],
                    "visit_id": visit_id, "event_id": event_id}

    def complete(self, code: str, visit_id: str,
                 arm_used: Optional[str] = None) -> dict:
        if not code or not visit_id:
            raise PilotError("号码与到访编号不能为空")
        if arm_used is not None and arm_used not in ("A", "B"):
            raise PilotError("方案取值必须为 A 或 B")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            package = self._package(conn)
            if not any(n["code"] == code for n in package["numbers"]):
                raise PilotError("号码不属于本设备号段")
            bound = conn.execute(
                "SELECT visit_id FROM local_events WHERE code=? AND "
                "event_type='issue' ORDER BY seq LIMIT 1", (code,)).fetchone()
            if bound is None:
                raise PilotError("该号码尚未派出，不能报完成")
            if bound["visit_id"] != visit_id:
                raise PilotError("到访编号与派号绑定不一致，拒绝上报")
            prior = conn.execute(
                "SELECT * FROM local_events WHERE code=? AND event_type='complete' "
                "ORDER BY seq LIMIT 1", (code,)).fetchone()
            if prior is not None:
                return {"code": code, "visit_id": visit_id,
                        "idempotent_replay": True}
            seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM local_events").fetchone()
            event_id = f"{self.device_id}-EV{seq_row['s']:06d}"
            conn.execute(
                "INSERT INTO local_events(event_id, code, visit_id, arm_used, "
                "event_type, occurred_at) VALUES (?,?,?,?, 'complete', ?)",
                (event_id, code, visit_id, arm_used, _now()))
            return {"code": code, "visit_id": visit_id,
                    "arm_used": arm_used, "event_id": event_id}

    # ------------------------------------------------------------ 回传
    def build_upload(self) -> Optional[dict]:
        """构造一次回传载荷；无待传事件返回 None。"""
        with self._conn() as conn:
            package = self._package(conn)
            st = conn.execute("SELECT * FROM state").fetchone()
            rows = conn.execute(
                "SELECT * FROM local_events WHERE seq > ? ORDER BY seq",
                (st["upload_cursor"],)).fetchall()
            if not rows:
                return None
            upload_id = f"{self.device_id}-UP{st['upload_cursor'] + 1:06d}-" \
                        f"{st['upload_cursor'] + len(rows):06d}-{uuid.uuid4().hex[:6]}"
            return {
                "upload_id": upload_id,
                "batch_id": package["batch_id"],
                "segment_id": package["segment_id"],
                "device_id": self.device_id,
                "events": [
                    {k: row[k] for k in
                     ("event_id", "code", "visit_id", "arm_used",
                      "event_type", "occurred_at")}
                    for row in rows],
                "_last_seq": rows[-1]["seq"],
            }

    def mark_uploaded(self, last_seq: int) -> None:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE local_events SET synced=1 WHERE seq<=?",
                         (last_seq,))
            conn.execute("UPDATE state SET upload_cursor=? WHERE upload_cursor<?",
                         (last_seq, last_seq))

    def unused_codes(self) -> list[str]:
        """交回时清点未用号。"""
        with self._conn() as conn:
            package = self._package(conn)
            st = conn.execute("SELECT next_pos FROM state").fetchone()
            return [n["code"] for n in package["numbers"][st["next_pos"]:]]


def _arm_of(package: dict, code: str) -> str:
    for n in package["numbers"]:
        if n["code"] == code:
            return n["arm"]
    raise PilotError(f"号包内找不到号码 {code}")
