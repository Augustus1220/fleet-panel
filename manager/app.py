"""Fleet Panel manager. Configure with FLEET_MANAGER_CONFIG before launching."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from functools import wraps

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from common.events import PANEL_RESTART_MESSAGES, VISIBLE_EVENT, is_panel_restart
from common.monitoring import (
    MAX_REPORT, NODE_ID, RANGES, RETENTION, TIMEZONE_OFFSET, bucket_start,
    clock, counter, snapshot_from_collector, traffic, validate_report,
)
from manager import store

def create_app(config=None):
    if config is None:
        config = store.load_config()
    app = Flask(__name__)
    if config.get("behind_proxy") is True:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

    @app.template_filter("traffic_bytes")
    def traffic_bytes(value):
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f}".removesuffix(".0") + " " + unit
            value /= 1024
    app.config.update(
        SECRET_KEY=config["session_key"],
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=MAX_REPORT,
    )
    database = store.open_store(config)
    password_hash = config["password_hash"]
    password_version = hashlib.sha256(password_hash.encode()).hexdigest()
    def connect():
        return store.connect(database)

    def authenticated():
        return (session.get("password_version") == password_version
                and time.time() - session.get("signed_in_at", 0) < 7 * 86400)

    def require_session(fn):
        @wraps(fn)
        def checked(*args, **kwargs):
            if not authenticated():
                return jsonify(error="unauthorized"), 401
            return fn(*args, **kwargs)
        return checked

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        return session["csrf"]

    def verify_csrf():
        expected = session.get("csrf")
        if not expected or not hmac.compare_digest(expected.encode(), request.form.get("csrf", "").encode()):
            abort(400)

    def node_status(db, node):
        node_id = node["node_id"]
        row = db.execute(
            """SELECT captured_at, snapshot FROM reports
               WHERE node_id = ? ORDER BY captured_at DESC LIMIT 1""", (node_id,),
        ).fetchone()
        contact = db.execute("SELECT received_at FROM contacts WHERE node_id = ?", (node_id,)).fetchone()
        latest = row["captured_at"] if row else None
        received = contact["received_at"] if contact else None
        now = time.time()
        return {
            "node_id": node_id, "label": node["label"],
            "tags": json.loads(node["tags"]), "enabled": bool(node["enabled"]),
            "latest_sample_at": latest, "last_report_at": received,
            "data_state": "missing" if latest is None else ("stale" if now - latest > 600 else "current"),
            "contact_state": "missing" if received is None else ("offline" if now - received > 600 else "online"),
            "snapshot": json.loads(row["snapshot"]) if row else None,
        }

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    def event_selection(values):
        raw_day = values.get("event_date")
        if raw_day in (None, ""):
            day = datetime.fromtimestamp(time.time() + TIMEZONE_OFFSET, timezone.utc).date()
        elif raw_day == "all":
            day = None
        else:
            if len(raw_day) != 10:
                abort(400, "日期格式应为 YYYY-MM-DD")
            try:
                day = date.fromisoformat(raw_day)
            except ValueError:
                abort(400, "日期无效")
            if day.isoformat() != raw_day:
                abort(400, "日期格式应为 YYYY-MM-DD")
        node = (values.get("event_node") or "").strip() or None
        return day, values.get("event_page", 1, type=int), node

    def dashboard_context(panel):
        context = {"csrf": csrf_token(), "clock": clock}
        with closing(connect()) as db:
            db.execute("BEGIN")
            if panel in ("all", "hosts"):
                context["nodes"] = [node_status(db, node) for node in store.nodes_in_order(db)]
            if panel in ("all", "events"):
                day, page, node_filter = event_selection(request.args)
                context["event_page"] = store.event_page(db, day, page, node_filter)
                context["event_node"] = node_filter or ""
                context["event_nodes"] = [
                    {"node_id": row["node_id"], "label": row["label"]}
                    for row in db.execute("SELECT node_id, label FROM nodes ORDER BY node_id")
                ]
        return context

    @app.get("/")
    def index():
        if not authenticated():
            return render_template("login.html", csrf=csrf_token())
        if "event_date" in request.args or "event_page" in request.args or "event_node" in request.args:
            day, page, node_filter = event_selection(request.args)
            return redirect(url_for("events_panel",
                                    event_date=day.isoformat() if day else "all",
                                    event_page=page if page > 1 else None,
                                    event_node=node_filter))
        return render_template("index.html", **dashboard_context("hosts"))

    @app.get("/traffic")
    def traffic_panel():
        if not authenticated():
            return redirect(url_for("index"))
        with closing(connect()) as db:
            nodes = db.execute("SELECT node_id, label FROM nodes ORDER BY node_id").fetchall()
        return render_template("traffic.html", nodes=nodes, csrf=csrf_token())

    @app.get("/events")
    def events_panel():
        if not authenticated():
            return redirect(url_for("index"))
        return render_template("events.html", **dashboard_context("events"))

    @app.get("/feed")
    @require_session
    def feed():
        panel = request.args.get("panel", "all")
        if panel not in ("all", "hosts", "events"):
            abort(400)
        return render_template("feed.html", **dashboard_context(panel))

    def management_page(message=None, token=None, node_id=None, status=200):
        with closing(connect()) as db:
            nodes = [dict(node) for node in store.nodes_in_order(db)]
        for node in nodes:
            node.pop("token_sha256")
            node["tags"] = ", ".join(json.loads(node["tags"]))
        return render_template("nodes.html", nodes=nodes, csrf=csrf_token(),
                               message=message, token=token, selected=node_id), status

    def node_form():
        label = request.form.get("label", "").strip()
        tags = [tag.strip() for tag in request.form.get("tags", "").replace("，", ",").split(",") if tag.strip()]
        if not 1 <= len(label) <= 80 or len(tags) > 8 or any(len(tag) > 24 for tag in tags):
            raise ValueError("名称需为 1 至 80 字。最多 8 个标签，每个最多 24 字。")
        return label, json.dumps(list(dict.fromkeys(tags)), ensure_ascii=False)

    @app.get("/manage/nodes")
    @require_session
    def manage_nodes():
        return management_page()

    @app.post("/manage/nodes")
    @require_session
    def register_node():
        verify_csrf()
        node_id = request.form.get("node_id", "").strip()
        if not NODE_ID.fullmatch(node_id):
            return management_page("主机编号需使用小写字母、数字和连字符。", status=400)
        try:
            label, tags = node_form()
        except ValueError as error:
            return management_page(str(error), status=400)
        token = secrets.token_urlsafe(32)
        with closing(connect()) as db, db:
            inserted = db.execute(
                "INSERT INTO nodes VALUES (?, ?, ?, 1, ?) ON CONFLICT(node_id) DO NOTHING",
                (node_id, label, tags, hashlib.sha256(token.encode()).hexdigest()),
            ).rowcount
        if not inserted:
            return management_page("主机编号已登记。", status=409)
        return management_page(token=token, node_id=node_id, status=201)

    @app.post("/manage/nodes/<node_id>")
    @require_session
    def update_node(node_id):
        verify_csrf()
        try:
            label, tags = node_form()
        except ValueError as error:
            return management_page(str(error), node_id=node_id, status=400)
        with closing(connect()) as db, db:
            updated = db.execute(
                "UPDATE nodes SET label = ?, tags = ?, enabled = ? WHERE node_id = ?",
                (label, tags, int(request.form.get("enabled") == "1"), node_id),
            ).rowcount
        if not updated:
            abort(404)
        return redirect(url_for("manage_nodes"))

    @app.post("/manage/nodes/<node_id>/rotate")
    @require_session
    def rotate_token(node_id):
        verify_csrf()
        token = secrets.token_urlsafe(32)
        with closing(connect()) as db, db:
            updated = db.execute(
                "UPDATE nodes SET token_sha256 = ? WHERE node_id = ?",
                (hashlib.sha256(token.encode()).hexdigest(), node_id),
            ).rowcount
        if not updated:
            abort(404)
        return management_page(token=token, node_id=node_id)

    @app.post("/login")
    def login():
        verify_csrf()
        client = request.remote_addr or "local"
        now = int(time.time())
        with closing(connect()) as db, db:
            db.execute("DELETE FROM login_attempts WHERE until <= ?", (now,))
            row = db.execute("SELECT count, until FROM login_attempts WHERE client = ?", (client,)).fetchone()
            if row and row["count"] >= 5:
                return render_template("login.html", csrf=csrf_token(), error="尝试过多，请稍后再试"), 429
            password = request.form.get("password", "")
            if not password or len(password) > 512 or not check_password_hash(password_hash, password):
                db.execute("""INSERT INTO login_attempts VALUES (?, 1, ?)
                           ON CONFLICT(client) DO UPDATE SET count = count + 1""", (client, now + 600))
                return render_template("login.html", csrf=csrf_token(), error="密码错误"), 401
            db.execute("DELETE FROM login_attempts WHERE client = ?", (client,))
        session.clear()
        session.permanent = True
        session["password_version"] = password_version
        session["signed_in_at"] = int(time.time())
        return redirect(url_for("index"))

    @app.post("/logout")
    def logout():
        verify_csrf()
        session.clear()
        return redirect(url_for("index"))

    @app.post("/api/v1/reports")
    def reports():
        report = request.get_json(silent=True)
        if not isinstance(report, dict) or not isinstance(report.get("node_id"), str):
            return jsonify(error="invalid_report"), 400
        node_id = report["node_id"]
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify(error="unauthorized"), 401
        digest = hashlib.sha256(auth[7:].encode()).hexdigest()

        def authorized(db):
            node = store.get_node(db, node_id)
            return node and node["enabled"] and hmac.compare_digest(digest, node["token_sha256"])

        with closing(connect()) as db:
            if not authorized(db):
                return jsonify(error="unauthorized"), 401
        now = int(time.time())
        try:
            validate_report(report, now)
            snapshot = json.dumps(snapshot_from_collector(report["snapshot"]), ensure_ascii=False, allow_nan=False)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        report_id, captured_at = report["report_id"], report["captured_at"]
        with closing(connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not authorized(db):
                return jsonify(error="unauthorized"), 401
            result = db.execute(
                "INSERT OR IGNORE INTO reports VALUES (?, ?, ?, ?)",
                (node_id, report_id, captured_at, snapshot),
            )
            if result.rowcount:
                db.executemany(
                    """INSERT INTO events(node_id, event_id, captured_at, kind, text)
                       SELECT ?, ?, ?, ?, ? WHERE NOT EXISTS (
                           SELECT 1 FROM events WHERE node_id = ? AND captured_at = ?
                           AND kind = ? AND text = ? AND event_id LIKE 'import:%'
                       )""",
                    ((node_id, f"{report_id}:{index}", event["occurred_at"], event["kind"], event["text"],
                      node_id, event["occurred_at"], event["kind"], event["text"])
                     for index, event in enumerate(report["events"])
                     if not is_panel_restart(event["kind"], event["text"])),
                )
            db.execute("INSERT OR REPLACE INTO contacts VALUES (?, ?)", (node_id, now))
            db.execute("DELETE FROM reports WHERE node_id = ? AND captured_at < ?", (node_id, now - RETENTION))
            db.execute("DELETE FROM imported_samples WHERE node_id = ? AND captured_at < ?", (node_id, now - RETENTION))
            db.execute("DELETE FROM events WHERE node_id = ? AND captured_at < ?", (node_id, now - RETENTION))
        return jsonify(report_id=report_id)

    @app.get("/api/v1/nodes")
    @require_session
    def list_nodes():
        with closing(connect()) as db:
            return jsonify([node_status(db, node) for node in store.nodes_in_order(db)])

    @app.get("/api/v1/nodes/<node_id>/status")
    @require_session
    def status(node_id):
        with closing(connect()) as db:
            node = store.get_node(db, node_id)
            if node is None:
                abort(404)
            return jsonify(node_status(db, node))

    @app.get("/api/v1/nodes/<node_id>/traffic")
    @require_session
    def chart(node_id):
        range_key = request.args.get("range", "24h")
        if range_key not in RANGES:
            return jsonify(error="invalid_range"), 400
        now = int(time.time())
        start = bucket_start(range_key, now)
        limits = (node_id, start - 600, now)
        with closing(connect()) as db:
            if store.get_node(db, node_id) is None:
                abort(404)
            imported = db.execute(
                """SELECT captured_at, accounts FROM imported_samples
                   WHERE node_id = ? AND captured_at BETWEEN ? AND ?""", limits,
            ).fetchall()
            snapshots = db.execute(
                """SELECT captured_at, snapshot FROM reports
                   WHERE node_id = ? AND captured_at BETWEEN ? AND ?""", limits,
            ).fetchall()
        merged = {row["captured_at"]: json.loads(row["accounts"]) for row in imported}
        for row in snapshots:
            merged[row["captured_at"]] = {
                name: (count["tx"], count["rx"])
                for name, count in json.loads(row["snapshot"])["users"].items()
            }
        return jsonify(traffic(sorted(merged.items()), range_key, now))

    @app.get("/api/v1/nodes/<node_id>/events")
    @require_session
    def events(node_id):
        raw_cursor = request.args.get("after")
        after = request.args.get("after", type=int)
        if raw_cursor is not None and not counter(after):
            return jsonify(error="invalid_cursor"), 400
        with closing(connect()) as db:
            if store.get_node(db, node_id) is None:
                abort(404)
            if after is None:
                records = db.execute(
                    "SELECT id AS cursor, node_id, event_id, captured_at, kind, text FROM events "
                    f"WHERE node_id = ? AND {VISIBLE_EVENT} ORDER BY id DESC LIMIT 100",
                    (node_id, *PANEL_RESTART_MESSAGES),
                ).fetchall()
                records = list(reversed(records))
            else:
                records = db.execute(
                    f"""SELECT id AS cursor, node_id, event_id, captured_at, kind, text FROM events
                        WHERE node_id = ? AND id > ? AND {VISIBLE_EVENT} ORDER BY id LIMIT 100""",
                    (node_id, after, *PANEL_RESTART_MESSAGES),
                ).fetchall()
        return jsonify(items=[dict(row) for row in records], cursor=records[-1]["cursor"] if records else after)

    return app
