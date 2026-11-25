"""Shared wire format and traffic calculations. Standard library only."""
import re
from datetime import datetime, timezone

RANGES = {"24h": (3600, 24), "7d": (6 * 3600, 28), "30d": (86400, 30)}
TIMEZONE_OFFSET = 8 * 3600
RETENTION = 90 * 86400
MAX_REPORT = 256 * 1024
NODE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")


def counter(value):
    return type(value) is int and 0 <= value < 2**63


def validate_report(report, now):
    if not isinstance(report, dict) or type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("unsupported_schema")
    captured_at = report.get("captured_at")
    if not counter(captured_at) or not now - RETENTION <= captured_at <= now + 300:
        raise ValueError("invalid_capture_time")
    snapshot = report.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("invalid_snapshot")
    machine = snapshot.get("machine")
    accounts = snapshot.get("users")
    if not isinstance(machine, dict) or not all(counter(machine.get(k)) for k in ("rx", "tx", "total", "expire")):
        raise ValueError("invalid_machine")
    if not isinstance(accounts, dict) or len(accounts) > 200:
        raise ValueError("invalid_accounts")
    for name, counts in accounts.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or not isinstance(counts, dict):
            raise ValueError("invalid_account")
        if not all(counter(counts.get(k)) for k in ("rx", "tx")):
            raise ValueError("invalid_account_counters")
    for field in ("perf", "services", "certs"):
        if not isinstance(snapshot.get(field), dict):
            raise ValueError("invalid_" + field)
    perf = snapshot["perf"]
    if any(key in perf and not counter(perf[key]) for key in
           ("mem_total", "mem_avail", "disk_total", "disk_used", "uptime")):
        raise ValueError("invalid_performance_counters")
    cpu = perf.get("cpu_pct")
    if cpu is not None and (type(cpu) not in (int, float) or not 0 <= cpu <= 100):
        raise ValueError("invalid_cpu_usage")
    if any(type(active) is not bool for active in snapshot["services"].values()):
        raise ValueError("invalid_service_state")
    if any(value is not None and (not counter(value) or value > 4102444800)
           for value in snapshot["certs"].values()):
        raise ValueError("invalid_certificate_time")
    online = snapshot.get("online", [])
    if not isinstance(online, list) or len(online) > 200 or not all(isinstance(name, str) for name in online):
        raise ValueError("invalid_online")
    events = report.get("events")
    if not isinstance(events, list) or len(events) > 200:
        raise ValueError("invalid_events")
    for event in events:
        if not isinstance(event, dict) or not all(isinstance(event.get(k), str) for k in ("kind", "text")):
            raise ValueError("invalid_event")
        if (len(event["kind"]) > 24 or len(event["text"]) > 512
                or not counter(event.get("occurred_at")) or event["occurred_at"] > captured_at):
            raise ValueError("invalid_event")
    report_id = report.get("report_id")
    if not isinstance(report_id, str) or not re.fullmatch(r"[0-9a-f]{32}", report_id):
        raise ValueError("invalid_report_id")


def snapshot_from_collector(snap):
    """Serialize the monitor fields without subscription configuration or secrets."""
    return {
        "machine": {key: snap["machine"][key] for key in ("rx", "tx", "total", "expire")},
        "users": {
            name: {key: counts[key] for key in ("rx", "tx")}
            for name, counts in snap["users"].items()
        },
        "perf": {
            key: value for key, value in snap["perf"].items()
            if key in ("cpu_pct", "mem_total", "mem_avail", "disk_used", "disk_total", "uptime", "load")
        },
        "services": snap["services"],
        "certs": snap["certs"],
        "online": snap.get("online", []),
    }


def clock(value):
    if not isinstance(value, (int, float)) or not 0 < value <= 4102444800:
        return "—"
    return datetime.fromtimestamp(value + TIMEZONE_OFFSET, timezone.utc).strftime("%m-%d %H:%M:%S")


def bucket_start(range_key, now):
    span, count = RANGES[range_key]
    return ((now + TIMEZONE_OFFSET) // span - count + 1) * span - TIMEZONE_OFFSET


def traffic(rows, range_key, now):
    """Rows contain timestamp and cumulative account (tx, rx) pairs."""
    span, count = RANGES[range_key]
    start = bucket_start(range_key, now)
    totals = [None] * count
    accounts = {}
    for (prev_ts, previous), (ts, current) in zip(rows, rows[1:]):
        index = (ts - start) // span
        if not 0 <= index < count or ts > now:
            continue
        if ts - prev_ts > 600:
            continue
        matched = current.keys() & previous.keys()
        if not matched:
            continue
        total = 0
        for name in matched:
            increment = 0
            for value, old in zip(current[name], previous[name]):
                increment += value - old if value >= old else value
            if name not in accounts:
                accounts[name] = [None] * count
            accounts[name][index] = (accounts[name][index] or 0) + increment
            total += increment
        totals[index] = (totals[index] or 0) + total
    return {
        "schema_version": 1,
        "range": range_key,
        "accounts": accounts,
        "buckets": [
            {"start": start + index * span, "bytes": total}
            for index, total in enumerate(totals)
        ],
        "latest_sample_at": rows[-1][0] if rows else None,
    }
