import hashlib
import json
import re
import secrets
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from werkzeug.security import generate_password_hash

from manager.app import create_app
from manager.import_history import import_history
from node.local_api import response
from node.report import enqueue, flush

HTTPS = "https://localhost"


def sample(ts, rx):
    return {
        "ts": ts,
        "machine": {"rx": rx, "tx": 10, "total": 1000, "expire": ts + 7200},
        "users": {"test": {"rx": rx, "tx": 10}},
        "perf": {"cpu_pct": 0, "mem_total": 100, "mem_avail": 70, "disk_used": 1, "disk_total": 15},
        "services": {"proxy": True}, "certs": {"test": ts + 36000}, "online": ["test"],
        "webshare": {"private": "fixture-value"},
    }


class MonitoringTest(unittest.TestCase):
    def setUp(self):
        store = tempfile.TemporaryDirectory()
        self.addCleanup(store.cleanup)
        self.base = Path(store.name)
        self.token = secrets.token_hex(32)
        self.config = {
            "session_key": secrets.token_hex(32),
            "password_hash": generate_password_hash("fixture-password", method="pbkdf2:sha256:1000"),
            "database": str(self.base / "manager.sqlite3"),
            "nodes": {"node-01": {"label": "轻量主机", "token_sha256": hashlib.sha256(self.token.encode()).hexdigest()}},
        }
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.node = sqlite3.connect(self.base / "history.sqlite3")
        self.addCleanup(self.node.close)
        self.now = int(time.time())
        self.node.execute("CREATE TABLE samples(ts INTEGER PRIMARY KEY, users TEXT)")

    def store(self, when, rx, event=False):
        data = sample(when, rx)
        with self.node:
            self.node.execute("INSERT INTO samples VALUES (?, ?)", (when, json.dumps({"test": [10, rx]})))
            enqueue(self.node, "node-01", data, [(when - 1, "warn", "服务测试")] if event else [])
        (self.base / "status.json").write_text(json.dumps(data))

    def send(self, payload):
        reply = self.post_report(payload, self.token)
        self.assertEqual(reply.status_code, 200, reply.get_data(as_text=True))
        return reply.json["report_id"]

    def post_report(self, payload, token):
        return self.client.post("/api/v1/reports", data=payload, content_type="application/json",
                                headers={"Authorization": "Bearer " + token})

    def management_csrf(self):
        return re.search(r'name="csrf" value="([^"]+)"',
                         self.client.get("/manage/nodes", base_url=HTTPS).get_data(as_text=True))[1]

    def login(self):
        page = self.client.get("/", base_url=HTTPS).get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page)[1]
        result = self.client.post("/login", base_url=HTTPS, data={"csrf": csrf, "password": "fixture-password"})
        self.assertEqual(result.status_code, 302)

    def test_outage_recovery_dedup_and_shared_traffic(self):
        self.store(self.now - 125, 100, event=True)
        self.store(self.now - 5, 150)
        payload = self.node.execute("SELECT payload FROM report_queue ORDER BY captured_at").fetchone()[0]
        self.assertNotIn(b"fixture-value", payload)

        def offline(_):
            raise TimeoutError

        with self.assertRaises(TimeoutError):
            flush(self.node, offline)
        self.assertEqual(self.node.execute("SELECT count(*) FROM report_queue").fetchone()[0], 2)
        order = []

        def send(payload):
            order.append(json.loads(payload)["captured_at"])
            return self.send(payload)

        self.assertEqual(flush(self.node, send), 2)
        self.assertEqual(order, [self.now - 5, self.now - 125])
        self.assertEqual(self.node.execute("SELECT count(*) FROM report_queue").fetchone()[0], 0)
        self.send(payload)
        self.login()
        prefix = "/api/v1/nodes/node-01"
        status = self.client.get(prefix + "/status", base_url=HTTPS).json
        self.assertEqual(status["latest_sample_at"], self.now - 5)
        self.assertIsNotNone(status["last_report_at"])
        chart = self.client.get(prefix + "/traffic", base_url=HTTPS).json
        code, lite = response(prefix + "/traffic", {}, self.base, "node-01")
        self.assertEqual(code, 200)
        self.assertEqual(chart, lite)
        self.assertEqual(sum(bucket["bytes"] or 0 for bucket in chart["buckets"]), 50)
        events = self.client.get(prefix + "/events", base_url=HTTPS).json
        self.assertEqual(len(events["items"]), 1)
        self.assertEqual(events["items"][0]["captured_at"], self.now - 126)
        self.assertEqual(self.client.get("/", base_url=HTTPS).status_code, 200)
        self.assertIn("服务测试", self.client.get("/feed", base_url=HTTPS).get_data(as_text=True))

    def test_anonymous_and_missing_history(self):
        self.assertEqual(self.client.get("/api/v1/nodes").status_code, 401)
        self.store(self.now - 1, 100)
        payload = self.node.execute("SELECT payload FROM report_queue").fetchone()[0]
        reply = self.client.post("/api/v1/reports", data=payload, content_type="application/json")
        self.assertEqual(reply.status_code, 401)
        self.assertEqual(flush(self.node, self.send), 1)
        self.login()
        chart = self.client.get("/api/v1/nodes/node-01/traffic", base_url=HTTPS).json
        self.assertTrue(all(bucket["bytes"] is None for bucket in chart["buckets"]))

    def test_bad_ack_keeps_report(self):
        self.store(self.now - 1, 100)
        with self.assertRaises(ValueError):
            flush(self.node, lambda _: "other-report")
        self.assertEqual(self.node.execute("SELECT count(*) FROM report_queue").fetchone()[0], 1)

    def test_site_icon_is_served_on_login_and_manager_pages(self):
        login = self.client.get("/", base_url=HTTPS).get_data(as_text=True)
        self.assertIn('rel="icon" type="image/svg+xml" href="/static/fleet-mark.svg"', login)
        self.login()
        page = self.client.get("/", base_url=HTTPS).get_data(as_text=True)
        self.assertIn('class="brand-icon" src="/static/fleet-mark.svg"', page)
        with self.client.get("/static/fleet-mark.svg", base_url=HTTPS) as icon:
            self.assertEqual(icon.status_code, 200)
            self.assertEqual(icon.mimetype, "image/svg+xml")

    def test_usage_color_thresholds_and_missing_data(self):
        with self.app.test_request_context():
            metric = self.app.jinja_env.get_template("host_cards.html").module.metric
            self.assertIn("--load-color: var(--green)", metric("cpu", "CPU", 60))
            self.assertIn("var(--green), var(--amber) 50.0%", metric("cpu", "CPU", 67.5))
            self.assertIn("var(--amber), var(--red) 0.0%", metric("cpu", "CPU", 75))
            self.assertIn("var(--amber), var(--red) 50.0%", metric("cpu", "CPU", 82.5))
            self.assertIn("--load-color: var(--red)", metric("cpu", "CPU", 90))
            self.assertIn("--load-color: var(--muted)", metric("cpu", "CPU", None))

    def test_node_registration_rotation_and_disable(self):
        self.assertEqual(self.client.get("/manage/nodes", base_url=HTTPS).status_code, 401)
        self.login()
        form = {"csrf": self.management_csrf(), "node_id": "node-02", "label": "主管理器", "tags": "示例区，备用"}
        registered = self.client.post("/manage/nodes", base_url=HTTPS, data=form)
        self.assertEqual(registered.status_code, 201)
        self.assertEqual(registered.headers["Cache-Control"], "no-store")
        self.assertIn('data-cacheable="false"', registered.get_data(as_text=True))
        token = re.search(r'aria-label="上报凭据"[^>]+value="([^"]+)"', registered.get_data(as_text=True))[1]
        self.assertNotIn(token, self.client.get("/manage/nodes", base_url=HTTPS).get_data(as_text=True))
        self.assertEqual(self.client.post("/manage/nodes", base_url=HTTPS, data=form).status_code, 409)
        with self.node:
            enqueue(self.node, "node-02", sample(self.now - 1, 90), [])
        payload = self.node.execute("SELECT payload FROM report_queue").fetchone()[0]
        self.assertEqual(self.post_report(payload, self.token).status_code, 401)
        self.assertEqual(self.post_report(payload, token).status_code, 200)
        rotate = self.client.post("/manage/nodes/node-02/rotate", base_url=HTTPS, data={"csrf": form["csrf"]})
        self.assertEqual(rotate.status_code, 200)
        new_token = re.search(r'aria-label="上报凭据"[^>]+value="([^"]+)"', rotate.get_data(as_text=True))[1]
        self.assertEqual(self.post_report(payload, token).status_code, 401)
        self.assertEqual(self.post_report(payload, new_token).status_code, 200)
        data = {**form, "label": "主管理器 3", "enabled": "0"}
        self.assertEqual(self.client.post("/manage/nodes/node-02", base_url=HTTPS,
                                          data={**data, "csrf": "无效"}).status_code, 400)
        self.assertEqual(self.client.post("/manage/nodes/node-02", base_url=HTTPS, data=data).status_code, 302)
        self.assertEqual(self.post_report(payload, new_token).status_code, 401)
        restarted = create_app(self.config).test_client()
        self.assertEqual(restarted.post("/api/v1/reports", data=payload, content_type="application/json",
                                        headers={"Authorization": "Bearer " + new_token}).status_code, 401)
        status = self.client.get("/api/v1/nodes/node-02/status", base_url=HTTPS).json
        self.assertEqual(status["label"], "主管理器 3")
        self.assertEqual(status["tags"], ["示例区", "备用"])
        self.assertFalse(status["enabled"])

    def test_import_without_faking_contact(self):
        with self.node:
            self.node.executemany("INSERT INTO samples VALUES (?, ?)", [
                (self.now - 245, json.dumps({"test": [10, 50]})),
                (self.now - 125, json.dumps({"test": [10, 100]})),
            ])
            self.node.execute("CREATE TABLE events(ts INTEGER, kind TEXT, text TEXT)")
            self.node.execute("INSERT INTO events VALUES (?, 'warn', '旧事件')", (self.now - 100,))
        database = Path(self.config["database"])
        backup = self.base / "history.sqlite3"
        self.assertEqual(import_history(database, "node-01", backup), (2, 1))
        self.assertEqual(import_history(database, "node-01", backup), (0, 0))
        self.login()
        prefix = "/api/v1/nodes/node-01"
        status = self.client.get(prefix + "/status", base_url=HTTPS).json
        self.assertIsNone(status["latest_sample_at"])
        self.assertIsNone(status["last_report_at"])
        with self.node:
            enqueue(self.node, "node-01", sample(self.now - 5, 175), [(self.now - 100, "warn", "旧事件")])
        self.assertEqual(flush(self.node, self.send), 1)
        chart = self.client.get(prefix + "/traffic", base_url=HTTPS).json
        self.assertEqual(sum(bucket["bytes"] or 0 for bucket in chart["buckets"]), 125)
        self.assertEqual(sum(value or 0 for value in chart["accounts"]["test"]), 125)
        events = self.client.get(prefix + "/events", base_url=HTTPS).json["items"]
        self.assertEqual(len(events), 1)
        self.assertIn("旧事件", self.client.get("/feed", base_url=HTTPS).get_data(as_text=True))

    def test_only_panel_restarts_are_hidden_and_not_stored(self):
        database = Path(self.config["database"])
        legacy_event = (self.now - 120, "restart", "本面板 已重启")
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO events(node_id, event_id, captured_at, kind, text) VALUES ('node-01', 'legacy', ?, ?, ?)",
                       legacy_event)
        with self.node:
            self.node.execute("CREATE TABLE events(ts INTEGER, kind TEXT, text TEXT)")
            self.node.execute("INSERT INTO events VALUES (?, ?, ?)", legacy_event)
            enqueue(self.node, "node-01", sample(self.now - 5, 100), [
                (self.now - 7, "restart", "本面板 已重启"),
                (self.now - 7, "restart", "主管理器 已重启"),
                (self.now - 7, "restart", "订阅服务 已重启"),
                (self.now - 6, "boot", "整机已重启"),
            ])
        code, payload = response("/api/v1/nodes/node-01/events", {}, self.base, "node-01")
        self.assertEqual((code, payload["items"]), (200, []))
        self.assertEqual(import_history(database, "node-01", self.base / "history.sqlite3"), (0, 0))
        self.assertEqual(flush(self.node, self.send), 1)
        self.login()
        events = self.client.get("/api/v1/nodes/node-01/events?after=0", base_url=HTTPS).json["items"]
        self.assertEqual([event["text"] for event in events], ["订阅服务 已重启", "整机已重启"])
        page = self.client.get("/feed", base_url=HTTPS).get_data(as_text=True)
        self.assertNotIn("本面板 已重启", page)
        self.assertIn("整机已重启", page)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 3)

    def test_events_date_pagination_and_filters(self):
        start = int(datetime(2026, 11, 24, tzinfo=timezone(timedelta(hours=8))).timestamp())
        events = [
            ("earlier", start - 1, "warn", "前一天事件"),
            ("later", start + 86400, "warn", "后一天事件"),
            ("hidden", start + 10, "restart", "本面板 已重启"),
        ] + [(f"item-{i}", start + i, "warn", f"日期测试 {i}") for i in range(7)]
        with sqlite3.connect(self.config["database"]) as db:
            db.executemany("INSERT INTO events(node_id, event_id, captured_at, kind, text) VALUES ('node-01', ?, ?, ?, ?)", events)
        self.login()
        query = "?event_date=2026-11-24"
        first = self.client.get("/events" + query, base_url=HTTPS).get_data(as_text=True)
        self.assertEqual(first.count('class="event-row"'), 6)
        self.assertIn("1 / 2 页", first)
        self.assertIn("日期测试 6", first)
        self.assertNotIn("日期测试 0", first)
        self.assertNotIn("前一天事件", first)
        self.assertNotIn("后一天事件", first)
        self.assertNotIn("本面板 已重启", first)
        second = self.client.get("/feed" + query + "&event_page=2", base_url=HTTPS).get_data(as_text=True)
        self.assertEqual(second.count('class="event-row"'), 1)
        self.assertIn("2 / 2 页", second)
        self.assertIn("日期测试 0", second)
        self.assertIn("2 / 2 页", self.client.get("/feed" + query + "&event_page=99", base_url=HTTPS).text)
        self.assertIn("当天暂无事件", self.client.get("/feed?event_date=2026-11-22", base_url=HTTPS).text)
        self.assertEqual(self.client.get("/feed?event_date=2026-02-30", base_url=HTTPS).status_code, 400)
        self.assertNotIn("acknowledge", second)
        filtered = self.client.get("/feed" + query + "&event_node=node-01", base_url=HTTPS).get_data(as_text=True)
        self.assertEqual(filtered.count('class="event-row"'), 6)
        missing = self.client.get("/feed" + query + "&event_node=ghost", base_url=HTTPS).get_data(as_text=True)
        self.assertEqual(missing.count('class="event-row"'), 0)
        self.assertIn("当天暂无事件", missing)
        default = self.client.get("/events", base_url=HTTPS).get_data(as_text=True)
        today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
        self.assertIn(f'name="event_date" value="{today}"', default)
        self.assertIn('name="event_node"', default)
        everything = self.client.get("/events?event_date=all", base_url=HTTPS).get_data(as_text=True)
        self.assertIn("后一天事件", everything)

    def test_dashboard_navigation_renders_only_the_selected_module(self):
        self.assertEqual(self.client.get("/traffic", base_url=HTTPS).headers["Location"], "/")
        self.login()
        modules = {"/": 'id="host-feed"', "/traffic": 'class="traffic-grid"',
                   "/events": 'id="event-feed"', "/manage/nodes": 'class="module-body manage-pane"'}
        for url, marker in modules.items():
            with self.subTest(url=url):
                response = self.client.get(url, base_url=HTTPS)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'href="{url}" aria-current="page"', response.text)
                self.assertIn(marker, response.text)
                for other in modules.values():
                    if other != marker:
                        self.assertNotIn(other, response.text)
                fragment = self.client.get(url, base_url=HTTPS, headers={"X-Fleet-View": "workspace"})
                self.assertEqual(fragment.status_code, 200)
                self.assertEqual(fragment.headers["Cache-Control"], "no-store")
                self.assertIn('class="workspace"', fragment.text)
                self.assertIn('data-cacheable="true"', fragment.text)
                self.assertIn(marker, fragment.text)
                self.assertNotIn('class="primary-nav"', fragment.text)
                self.assertNotIn('navigation.js', fragment.text)
        events = self.client.get("/feed?panel=events&event_date=2026-11-24", base_url=HTTPS).text
        self.assertIn('data-fragment="events"', events)
        self.assertNotIn('data-fragment="hosts"', events)
        hosts = self.client.get("/feed?panel=hosts", base_url=HTTPS).text
        self.assertIn('data-fragment="hosts"', hosts)
        self.assertNotIn('data-fragment="events"', hosts)
        legacy = self.client.get("/?event_date=2026-11-24&event_page=2", base_url=HTTPS)
        self.assertEqual(legacy.headers["Location"], "/events?event_date=2026-11-24&event_page=2")


if __name__ == "__main__":
    unittest.main()
