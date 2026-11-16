"""SQLite 存储层。

所有状态（报文、段、观测）都持久化在 SQLite 中，服务重启后无需任何
内存态重建：观测只在报文组装并校验通过后才写入，因此任何查询天然只
反映"完整且已确认"的报文。
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    station_id  INTEGER NOT NULL,
    boot_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    seg_total   INTEGER NOT NULL,
    sha256      TEXT    NOT NULL,
    -- collecting: 收集中; published: 已组装校验并发布; pending_review: 待核
    status      TEXT    NOT NULL DEFAULT 'collecting'
                CHECK (status IN ('collecting', 'published', 'pending_review')),
    reason      TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (station_id, boot_id, msg_id)
);

CREATE TABLE IF NOT EXISTS segments (
    station_id  INTEGER NOT NULL,
    boot_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    seg_no      INTEGER NOT NULL,
    payload     BLOB    NOT NULL,
    received_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (station_id, boot_id, msg_id, seg_no)
);

-- 观测按 (站号, 分钟) 唯一：同一分钟无论被多少个报文重传，只入库一次。
CREATE TABLE IF NOT EXISTS observations (
    station_id  INTEGER NOT NULL,
    observed_at TEXT    NOT NULL,  -- ISO UTC 分钟, 如 2026-11-15T08:03:00Z
    level_m     REAL    NOT NULL,
    boot_id     INTEGER NOT NULL,
    msg_id      INTEGER NOT NULL,
    PRIMARY KEY (station_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_obs_msg
    ON observations (station_id, boot_id, msg_id);
"""


def connect(path: str) -> sqlite3.Connection:
    """打开（必要时创建）数据库并保证 schema 存在。"""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    # 并发上传时写锁等待而不是立刻报 SQLITE_BUSY
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con
