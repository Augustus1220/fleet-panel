"""Import the retained traffic history and events from a Fleet Panel node backup."""
import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from common.events import is_panel_restart
from common.monitoring import RETENTION, counter
from manager import store


def import_history(database, node_id, source):
    source = source.resolve(strict=True)
    if source == database.resolve():
        raise ValueError("源文件需要是节点历史库的独立备份")
    now = int(time.time())
    limits = (now - RETENTION, now)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        src.execute("BEGIN")
        rows = src.execute("SELECT ts, users FROM samples WHERE ts BETWEEN ? AND ? ORDER BY ts", limits)
        with closing(store.connect(database)) as dst, dst:
            if store.get_node(dst, node_id) is None:
                raise ValueError("请先在主管理器登记该节点")
            samples = 0
            for captured_at, raw in rows:
                accounts = json.loads(raw)
                if not isinstance(accounts, dict) or not all(
                    isinstance(pair, list) and len(pair) == 2 and all(map(counter, pair))
                    for pair in accounts.values()
                ):
                    raise ValueError("历史库存在无效账号流量记录")
                samples += dst.execute(
                    """INSERT OR IGNORE INTO imported_samples VALUES (?, ?, ?)""",
                    (node_id, captured_at, raw),
                ).rowcount
            events = 0
            if src.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone():
                rows = src.execute("SELECT rowid, ts, kind, text FROM events WHERE ts BETWEEN ? AND ?", limits)
                for rowid, captured_at, kind, text in rows:
                    if is_panel_restart(kind, text):
                        continue
                    identity = json.dumps((rowid, captured_at, kind, text), ensure_ascii=False).encode()
                    event_id = "import:" + hashlib.sha256(identity).hexdigest()
                    events += dst.execute(
                        """INSERT OR IGNORE INTO events(node_id, event_id, captured_at, kind, text)
                           SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (
                               SELECT 1 FROM events WHERE node_id = ? AND captured_at = ?
                               AND kind = ? AND text = ? AND event_id NOT LIKE 'import:%'
                           )""",
                        (node_id, event_id, captured_at, kind, text, node_id, captured_at, kind, text),
                    ).rowcount
    return samples, events


def main():
    parser = argparse.ArgumentParser(description="导入 Fleet Panel 节点的历史 SQLite 备份")
    parser.add_argument("node_id")
    parser.add_argument("backup", type=Path)
    args = parser.parse_args()
    try:
        database = store.open_store(store.load_config())
        samples, events = import_history(database, args.node_id, args.backup)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"导入失败：{error}\n")
    print(f"{args.node_id} 新增 {samples} 条采样、{events} 条事件")


if __name__ == "__main__":
    main()
