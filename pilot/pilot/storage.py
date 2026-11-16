"""SQLite 持久化与建表。

现场断网使用，单机文件库即可；所有写操作通过短事务 + busy_timeout 保证
回传与关场并发时的一致性。表结构对应业务：

- batches       一次试点批次（按展区），等量 A/B 号包，状态机驱动关场
- number_pool   全量随机号：号码、方案（盲化前的真实分配）、状态
- segments      号段：一批号整体分配给一台设备，未回收不得转拨
- events        设备离线事件（派号/到访/完成），回传合并后落库
- uploads       回传批次幂等键：重试不重复派号、不重复建分析
- disputes      争议隔离区：跨设备重复领号、同号内容冲突
- analyses      关场后分析（ITT：完成者按原分配统计，未完成保留分母）
- approvals     分析批准/分发记录；旧批次快照照常可核验
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    zone TEXT NOT NULL,
    qty_per_arm INTEGER NOT NULL CHECK (qty_per_arm > 0),
    seed INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','closing','closed')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS number_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    code TEXT NOT NULL,
    arm TEXT NOT NULL CHECK (arm IN ('A','B')),
    state TEXT NOT NULL DEFAULT 'unused'
        CHECK (state IN ('unused','issued','completed','reclaimed')),
    segment_id TEXT,
    visit_id TEXT,
    arm_used TEXT,
    issued_at TEXT,
    completed_at TEXT,
    UNIQUE(batch_id, code)
);

CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(id),
    device_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'assigned'
        CHECK (status IN ('assigned','returned')),
    assigned_at TEXT NOT NULL,
    returned_at TEXT,
    package_path TEXT,
    package_sig TEXT,
    UNIQUE(batch_id, device_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    code TEXT NOT NULL,
    visit_id TEXT,
    arm_used TEXT,
    event_type TEXT NOT NULL CHECK (event_type IN ('issue','complete')),
    occurred_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(device_id, event_id)
);

CREATE TABLE IF NOT EXISTS uploads (
    upload_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    result TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS disputes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    code TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('duplicate_issue','content_conflict')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','resolved')),
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    approved_at TEXT,
    distributed_at TEXT,
    approver TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pool_batch_state ON number_pool(batch_id, state);
CREATE INDEX IF NOT EXISTS idx_events_batch ON events(batch_id);
CREATE INDEX IF NOT EXISTS idx_disputes_batch ON disputes(batch_id, status);
"""


class Database:
    """线程安全的 SQLite 连接封装。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self.lock, self.connect() as conn:
            conn.executescript(SCHEMA)

    @property
    def lock(self) -> threading.RLock:
        """回传合并与关场共用同一把进程锁，串行化关键区段。"""
        return self._lock
