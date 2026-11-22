#!/usr/bin/env python3
"""Serve private per-account subscription files over HTTPS.

Routes come from payload.json: {route_name: {token, filename, content}},
where content is base64. Files named in USERINFO_FILES also get a
Subscription-Userinfo header built from the traffic accounting state.
Note the payload convention: for sub.txt (URI-list subscriptions) the
stored content is base64(base64(links)) - this server decodes one layer,
so clients receive the standard base64 subscription body.
"""
import base64
import http.server
import json
import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import urlsplit
import sys
import time


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", file=sys.stderr, flush=True)

data = json.loads(Path('/var/lib/fleet-subscriptions/payload.json').read_text())
routes = {}
for item in data.values():
    route = '/' + item['token'] + '/' + item['filename']
    routes[route] = (base64.b64decode(item['content']), item['filename'])

USERINFO_CONFIG = Path('/var/lib/fleet-subscriptions/userinfo.json')
TRAFFIC_STATE = Path('/var/lib/fleet-subscriptions/traffic_state.json')
USERINFO_FILES = {'clash.yaml', 'sub.txt'}


def _subscription_userinfo():
    """Build the standard Subscription-Userinfo header from traffic accounting state."""
    try:
        cfg = json.loads(USERINFO_CONFIG.read_text())
        st = json.loads(TRAFFIC_STATE.read_text())
        rx = int(st['acc_rx'])
        tx = int(st['acc_tx'])
        try:
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            if boot == st.get('boot_id'):
                for line in Path('/proc/net/dev').read_text().splitlines():
                    if line.strip().startswith(str(st.get('iface', 'ens7')) + ':'):
                        parts = line.split(':', 1)[1].split()
                        rx += max(0, int(parts[0]) - int(st['last_rx']))
                        tx += max(0, int(parts[8]) - int(st['last_tx']))
        except Exception:
            pass
        return f"upload={tx}; download={rx}; total={int(cfg['total'])}; expire={int(cfg['expire'])}"
    except Exception:
        return None

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'SubscriptionService'
    sys_version = ''

    def log_message(self, *_args):
        pass

    def do_GET(self):
        result = routes.get(urlsplit(self.path).path)
        if result is None:
            self.send_error(404)
            return
        content, name = result
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8' if name.endswith('.txt') else 'application/yaml; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store, private')
        if name in USERINFO_FILES:
            userinfo = _subscription_userinfo()
            if userinfo:
                self.send_header('Subscription-Userinfo', userinfo)
        self.end_headers()
        self.wfile.write(content)

class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    slots = threading.BoundedSemaphore(32)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            _log(f"rejected connection from {client_address[0]}: concurrency limit reached")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        tls_socket = None
        try:
            request.settimeout(8)
            tls_socket = context.wrap_socket(request, server_side=True)
            self.finish_request(tls_socket, client_address)
        except (OSError, ssl.SSLError, socket.timeout) as e:
            _log(f"connection from {client_address[0]} dropped: {type(e).__name__}")
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(tls_socket or request)
            self.slots.release()

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain('/etc/fleet-subscriptions/fullchain.pem', '/etc/fleet-subscriptions/privkey.pem')
server = Server(('0.0.0.0', 2097), Handler)
server.serve_forever()
