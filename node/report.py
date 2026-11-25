"""Persist outgoing reports alongside samples and flush from a systemd job."""
import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from common.monitoring import MAX_REPORT, RETENTION, snapshot_from_collector

MAX_PENDING = 1000


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def enqueue(db, node_id, snapshot, events):
    report_id = uuid4().hex
    timestamp = snapshot["ts"]
    payload = json.dumps({
        "schema_version": 1, "node_id": node_id, "captured_at": timestamp,
        "report_id": report_id, "snapshot": snapshot_from_collector(snapshot),
        "events": [{"occurred_at": min(ts, timestamp), "kind": kind, "text": event[:512]}
                   for ts, kind, event in events],
    }, ensure_ascii=False, allow_nan=False).encode()
    if len(payload) > MAX_REPORT:
        raise ValueError("采集上报超过大小限制")
    db.execute(
        """CREATE TABLE IF NOT EXISTS report_queue
           (report_id TEXT PRIMARY KEY, captured_at INTEGER, payload BLOB)"""
    )
    db.execute("INSERT INTO report_queue VALUES (?, ?, ?)", (report_id, timestamp, payload))
    db.execute("DELETE FROM report_queue WHERE captured_at < ?", (timestamp - RETENTION,))
    db.execute(
        """DELETE FROM report_queue WHERE report_id IN
           (SELECT report_id FROM report_queue ORDER BY captured_at DESC, rowid DESC
            LIMIT -1 OFFSET ?)""",
        (MAX_PENDING,),
    )


def flush(db, send):
    table = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'report_queue'").fetchone()
    if not table:
        return 0
    newest = db.execute(
        "SELECT report_id, payload FROM report_queue ORDER BY captured_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if newest is None:
        return 0
    backlog = db.execute(
        """SELECT report_id, payload FROM report_queue WHERE report_id != ?
           ORDER BY captured_at, rowid LIMIT 15""",
        (newest[0],),
    ).fetchall()
    deadline = time.monotonic() + 25
    sent = 0
    for report_id, payload in [newest, *backlog]:
        if time.monotonic() >= deadline:
            break
        if send(payload) != report_id:
            raise ValueError("主管理器未确认本批次")
        with db:
            db.execute("DELETE FROM report_queue WHERE report_id = ?", (report_id,))
        sent += 1
    return sent


def sender(url, token):
    address = urlsplit(url)
    if address.scheme != "https" or not address.hostname or address.username or address.query or address.fragment:
        raise ValueError("上报地址必须是 HTTPS 接口地址")
    opener = build_opener(NoRedirect())

    def send(payload):
        req = Request(url, payload, {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }, method="POST")
        with opener.open(req, timeout=4) as response:
            return json.loads(response.read(4096))["report_id"]

    return send


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--config", type=Path, default=Path("/etc/fleet-panel/report.json"))
    cli.add_argument("--history", type=Path, default=Path("/var/lib/fleet-panel/history.sqlite3"))
    args = cli.parse_args()
    if not args.config.exists() or not args.history.exists():
        return 0
    try:
        config = json.loads(args.config.read_text())
        with closing(sqlite3.connect(args.history)) as db:
            sent = flush(db, sender(config["url"], config["token"]))
        if sent:
            print(f"已送达 {sent} 批")
    except (OSError, URLError, sqlite3.Error, ValueError, KeyError) as error:
        print(f"上报中断，队列留待下次发送（{type(error).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
