"""SQLite persistence for the manager and history importer."""
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from common.events import PANEL_RESTART_MESSAGES, VISIBLE_EVENT
from common.monitoring import NODE_ID, TIMEZONE_OFFSET

EVENTS_PER_PAGE = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    tags TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    token_sha256 TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS imported_samples (
    node_id TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    accounts TEXT NOT NULL,
    PRIMARY KEY (node_id, captured_at)
);
CREATE TABLE IF NOT EXISTS reports (
    node_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    snapshot TEXT NOT NULL,
    PRIMARY KEY (node_id, report_id),
    UNIQUE (node_id, captured_at)
);
CREATE TABLE IF NOT EXISTS contacts (
    node_id TEXT PRIMARY KEY,
    received_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (node_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_node_time ON events(node_id, captured_at);
CREATE INDEX IF NOT EXISTS events_time ON events(captured_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS event_acknowledgments (
    event INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
    acknowledged_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    client TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    until INTEGER NOT NULL
);
"""



def load_config():
    path = Path(os.environ.get("FLEET_MANAGER_CONFIG", "/etc/fleet-panel/manager.json"))
    return json.loads(path.read_text())


def connect(database):
    db = sqlite3.connect(database, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def open_store(config):
    database = Path(config["database"])
    nodes = config.get("nodes", {})
    records = []
    for node_id, node in nodes.items():
        token_hash = node["token_sha256"]
        if not NODE_ID.fullmatch(node_id) or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
            raise ValueError("主机编号或凭据摘要格式无效")
        records.append((
            node_id, node.get("label", node_id), json.dumps(node.get("tags", [])),
            int(node.get("enabled", True)), token_hash,
        ))
    with closing(connect(database)) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(SCHEMA)
        with db:
            db.executemany(
                "INSERT INTO nodes VALUES (?, ?, ?, ?, ?) ON CONFLICT(node_id) DO NOTHING",
                records,
            )
    return database


def nodes_in_order(db):
    return db.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()


def get_node(db, node_id):
    return db.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()


def event_page(db, day, page, node_id=None):
    criteria = VISIBLE_EVENT
    params = PANEL_RESTART_MESSAGES
    if day:
        start = (day - date(1970, 1, 1)).days * 86400 - TIMEZONE_OFFSET
        criteria += " AND captured_at >= ? AND captured_at < ?"
        params += (start, start + 86400)
    if node_id:
        criteria += " AND node_id = ?"
        params += (node_id,)

    total = db.execute(f"SELECT COUNT(*) FROM events WHERE {criteria}", params).fetchone()[0]
    pages = max(1, (total + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
    page = max(1, min(page, pages))
    entries = db.execute(
        f"""SELECT * FROM events
           WHERE {criteria}
           ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?""",
        (*params, EVENTS_PER_PAGE, (page - 1) * EVENTS_PER_PAGE),
    ).fetchall()
    return {"entries": entries, "page": page, "pages": pages,
            "day": day.isoformat() if day else None, "node": node_id}
