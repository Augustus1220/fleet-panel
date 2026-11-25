"""Versioned read API for the original standard-library panel."""
import json
import time
import sqlite3
from contextlib import closing

from common.events import PANEL_RESTART_MESSAGES, VISIBLE_EVENT
from common.monitoring import RANGES, bucket_start, counter, snapshot_from_collector, traffic


def response(path, query, base, node_id):
    def parameter(name, default):
        return (query.get(name) or [default])[0]

    def current_status():
        latest = None
        snapshot = None
        try:
            data = json.loads((base / "status.json").read_text())
            latest = data["ts"]
            snapshot = snapshot_from_collector(data)
        except FileNotFoundError:
            pass
        return {
            "node_id": node_id, "label": node_id, "tags": [], "enabled": True,
            "latest_sample_at": latest, "last_report_at": None, "contact_state": None,
            "data_state": "missing" if latest is None else ("stale" if time.time() - latest > 600 else "current"),
            "snapshot": snapshot,
        }

    prefix = "/api/v1/nodes"
    if path == prefix:
        return 200, [current_status()]
    if path == f"{prefix}/{node_id}/status":
        return 200, current_status()

    uri = (base / "history.sqlite3").as_uri() + "?mode=ro"
    if path == f"{prefix}/{node_id}/traffic":
        range_key = parameter("range", "24h")
        if range_key not in RANGES:
            return 400, {"error": "invalid_range"}
        now = int(time.time())
        rows = []
        if (base / "history.sqlite3").exists():
            with closing(sqlite3.connect(uri, uri=True)) as db:
                rows = db.execute(
                    "SELECT ts, users FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts",
                    (bucket_start(range_key, now) - 600, now),
                ).fetchall()
        return 200, traffic([(ts, json.loads(accounts)) for ts, accounts in rows], range_key, now)

    if path == f"{prefix}/{node_id}/events":
        after = parameter("after", None)
        try:
            after = int(after) if after is not None else None
        except ValueError:
            return 400, {"error": "invalid_cursor"}
        if after is not None and not counter(after):
            return 400, {"error": "invalid_cursor"}
        rows = []
        if (base / "history.sqlite3").exists():
            with closing(sqlite3.connect(uri, uri=True)) as db:
                if after is None:
                    rows = db.execute(
                        f"SELECT rowid, ts, kind, text FROM events WHERE {VISIBLE_EVENT} ORDER BY rowid DESC LIMIT 100",
                        PANEL_RESTART_MESSAGES,
                    ).fetchall()[::-1]
                else:
                    rows = db.execute(
                        f"SELECT rowid, ts, kind, text FROM events WHERE rowid > ? AND {VISIBLE_EVENT} ORDER BY rowid LIMIT 100",
                        (after, *PANEL_RESTART_MESSAGES),
                    ).fetchall()
        items = [
            {"cursor": rowid, "event_id": str(rowid), "node_id": node_id,
             "captured_at": ts, "kind": kind, "text": content}
            for rowid, ts, kind, content in rows
        ]
        return 200, {"items": items, "cursor": rows[-1][0] if rows else after}
    return 404, {"error": "unknown_route"}
