#!/usr/bin/env bash
# fleet-panel basic installer — one clean Ubuntu 24.04 VPS, one subdomain.
#
#   sudo ./install.sh --domain node.example.com [options]
#
# Options:
#   --domain NAME          node subdomain (required); its A record must
#                          already point at this VPS
#   --accounts a,b,c,d     up to 4 account slots (default keeps the
#                          template names: alice_mac,alice_desktop,
#                          alice_iphone,bob); credentials are generated
#   --cert http|dns-cf     certificate path (default http: HTTP-01 on
#                          TCP 80; dns-cf needs CF_Token in the env)
#   --panel loopback|public  panel bind (default loopback; reach it via
#                          an SSH tunnel — see the final summary)
#   --residential HOST:PORT:USER:PASS
#                          also install the residential-exit chain
#                          (advanced; skipped when omitted)
#   --quota-gb N           subscription traffic quota shown to clients
#                          (default 1000)
#   --dry-run              print every action, change nothing
#
# Re-running on an installed node is a no-op: it prints the node status
# and exits 0 without touching credentials or configs.
set -euo pipefail

DOMAIN=""; ACCOUNTS="alice_mac,alice_desktop,alice_iphone,bob"
CERT_MODE="http"; PANEL_BIND="loopback"; RESIDENTIAL=""
QUOTA_GB=1000; DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN=$2; shift 2;;
    --accounts) ACCOUNTS=$2; shift 2;;
    --cert) CERT_MODE=$2; shift 2;;
    --panel) PANEL_BIND=$2; shift 2;;
    --residential) RESIDENTIAL=$2; shift 2;;
    --quota-gb) QUOTA_GB=$2; shift 2;;
    --dry-run) DRY=1; shift;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done
[ -n "$DOMAIN" ] || { echo "error: --domain is required" >&2; exit 2; }
REPO_DIR=$(cd "$(dirname "$0")" && pwd)
HY2_VERSION="v2.14.0"; XRAY_VERSION="v26.11.9"
MARKER=/etc/fleet-panel/credentials.json

run() { if [ "$DRY" = 1 ]; then echo "[dry-run] $*"; else echo "+ $*"; "$@"; fi; }
note() { echo "== $*"; }

# ---------------------------------------------------------------- idempotent
if [ -f "$MARKER" ]; then
  note "existing installation detected ($MARKER) — nothing changed."
  python3 - "$MARKER" <<'PYEOF'
import json, sys
c = json.load(open(sys.argv[1]))
print("domain:", c["domain"])
for name, url in c["subscriptions"].items():
    print(f"  {name}: {url}")
print("panel bind:", c["panel_bind"], " port: 2096")
PYEOF
  for u in fleet-hy2 xray fleet-subscription fleet-panel; do
    printf "%-20s %s\n" "$u" "$(systemctl is-active $u 2>/dev/null || echo unknown)"
  done
  exit 0
fi

# ---------------------------------------------------------------- preflight
note "preflight"
[ "$(id -u)" = 0 ] || { echo "error: run as root (sudo)" >&2; exit 1; }
. /etc/os-release
[ "${ID:-}" = ubuntu ] && [ "${VERSION_ID:-}" = "24.04" ] \
  || { echo "error: basic installer supports Ubuntu 24.04 only (found ${PRETTY_NAME:-unknown})" >&2; exit 1; }
command -v python3 >/dev/null || { echo "error: python3 missing" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "error: systemd missing" >&2; exit 1; }

PUBIP=$(curl -s -m 10 https://api.ipify.org || curl -s -m 10 https://icanhazip.com || true)
[ -n "$PUBIP" ] || { echo "error: cannot determine this host's public IP" >&2; exit 1; }
DNSIP=$(python3 -c "import socket; print(socket.gethostbyname('$DOMAIN'))" 2>/dev/null || true)
if [ "$DNSIP" != "$PUBIP" ]; then
  cat >&2 <<EOF
error: $DOMAIN resolves to '${DNSIP:-nothing}', this host's public IP is $PUBIP.
Set the A record for $DOMAIN to $PUBIP first (DNS only / grey cloud if you
use Cloudflare), wait for it to propagate, then re-run.
EOF
  exit 1
fi
echo "dns ok: $DOMAIN -> $PUBIP"

PORTS_TCP="443 2053 2096 2097"; PORTS_UDP="443"
[ "$CERT_MODE" = http ] && PORTS_TCP="$PORTS_TCP 80"
[ -n "$RESIDENTIAL" ] && { PORTS_TCP="$PORTS_TCP 2054"; PORTS_UDP="$PORTS_UDP 2054"; }
BUSY=$(python3 - $PORTS_TCP --- $PORTS_UDP <<'PYEOF'
import socket, sys
args = sys.argv[1:]
sep = args.index('---')
tcp, udp = args[:sep], args[sep+1:]
busy = []
for p in tcp:
    s = socket.socket()
    try:
        s.bind(('0.0.0.0', int(p)))
    except OSError:
        busy.append(f"tcp/{p}")
    s.close()
for p in udp:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(('0.0.0.0', int(p)))
    except OSError:
        busy.append(f"udp/{p}")
    s.close()
print(' '.join(busy))
PYEOF
)
if [ -n "$BUSY" ]; then
  echo "error: required ports already in use: $BUSY" >&2
  echo "The basic installer targets a clean VPS; free those ports or" >&2
  echo "follow docs/deployment.md for a manual install alongside them." >&2
  exit 1
fi
echo "ports free: tcp[$PORTS_TCP] udp[$PORTS_UDP]"
cat <<EOF
note: this installer cannot open your cloud provider's security group.
Make sure these inbound rules exist in the provider panel, or clients
will not reach the node:
  TCP 443, UDP 443, UDP 20000-30000, TCP 2053, TCP 2097, SSH
  $([ -n "$RESIDENTIAL" ] && echo "TCP 2054, UDP 2054 (residential)")
  $([ "$CERT_MODE" = http ] && echo "TCP 80 (certificate issuance only; may be closed afterwards)")
  TCP 2096 is NOT needed when the panel binds loopback (the default).
EOF

# ---------------------------------------------------------------- binaries
note "binaries (hysteria $HY2_VERSION, xray $XRAY_VERSION)"
if [ "$DRY" = 0 ]; then
  TMPD=$(mktemp -d)
  curl -fsSL -o "$TMPD/hysteria" "https://github.com/apernet/hysteria/releases/download/$HY2_VERSION/hysteria-linux-amd64"
  install -m 755 "$TMPD/hysteria" /usr/local/bin/hysteria
  curl -fsSL -o "$TMPD/xray.zip" "https://github.com/XTLS/Xray-core/releases/download/$XRAY_VERSION/Xray-linux-64.zip"
  python3 -c "import zipfile; zipfile.ZipFile('$TMPD/xray.zip').extractall('$TMPD/xray')"
  install -m 755 "$TMPD/xray/xray" /usr/local/bin/xray
  mkdir -p /usr/local/share/xray
  cp "$TMPD/xray/"*.dat /usr/local/share/xray/ 2>/dev/null || true
  cp "$TMPD/xray/"*.dat /usr/local/bin/ 2>/dev/null || true
  rm -rf "$TMPD"
  /usr/local/bin/hysteria version >/dev/null
  /usr/local/bin/xray version >/dev/null
else
  echo "[dry-run] download + install hysteria/xray binaries"
fi

# ---------------------------------------------------------------- render
note "render configs (credentials are generated, never printed)"
RENDER_ENV=()
[ "$DRY" = 1 ] && RENDER_ENV+=("FLEET_DRY=1")
env "${RENDER_ENV[@]}" python3 - "$REPO_DIR" "$DOMAIN" "$ACCOUNTS" "$RESIDENTIAL" "$PANEL_BIND" "$QUOTA_GB" <<'PYEOF'
import base64, json, os, secrets, string, subprocess, sys, uuid
from pathlib import Path

repo, domain, accounts_csv, residential, panel_bind, quota_gb = sys.argv[1:7]
dry = os.environ.get("FLEET_DRY") == "1"
slots_tpl = ["alice_mac", "alice_desktop", "alice_iphone", "bob"]
slots = [s.strip() for s in accounts_csv.split(",") if s.strip()]
assert 1 <= len(slots) <= 4, "basic installer supports 1-4 accounts"
slots += slots_tpl[len(slots):]           # unused slots stay template-named
used = slots[:len([s for s in accounts_csv.split(',') if s.strip()])]
quota = int(quota_gb) * 1024 ** 3

def pw(n=24):
    return secrets.token_hex(n // 2)

def new_uuid():
    return "00000000-0000-4000-8000-000000000000" if dry else str(uuid.uuid4())

if dry:
    priv = pub = "DRYRUN-REALITY-KEY"
else:
    out = subprocess.run(["/usr/local/bin/xray", "x25519"], capture_output=True, text=True).stdout
    kv = dict(l.split(": ", 1) for l in out.splitlines() if ": " in l)
    priv = kv.get("PrivateKey") or kv["Private key"]
    pub = kv.get("PublicKey") or kv["Public key"]
short_id = secrets.token_hex(8)

creds = {"domain": domain, "panel_bind": panel_bind, "accounts": {}, "subscriptions": {}}
hy2_pw, uuids, uuids_ws, tokens = {}, {}, {}, {}
for slot in slots:
    hy2_pw[slot] = pw()
    uuids[slot] = new_uuid()
    uuids_ws[slot] = new_uuid()
    tokens[slot] = secrets.token_hex(32)
stats_secret = pw(32)

def slot_map(text, slot):
    tpl = slots_tpl[slots.index(slot)]
    text = text.replace("PASSWORD_" + tpl.upper(), hy2_pw[slot])
    text = text.replace("UUID_" + tpl.upper() + "_WS", uuids_ws[slot])
    text = text.replace("UUID_" + tpl.upper(), uuids[slot])
    text = text.replace(tpl, slot)
    return text

def global_map(text):
    text = text.replace("node.example.com", domain)
    text = text.replace("panel.example.com", domain)
    text = text.replace("TRAFFIC_STATS_SECRET", stats_secret)
    text = text.replace("REALITY_PRIVATE_KEY", priv)
    text = text.replace("REALITY_PUBLIC_KEY", pub)
    text = text.replace("SHORT_ID_HEX", short_id)
    return text

root = Path("/") if not dry else Path("/tmp/fleet-dryrun")
def write(rel, text, mode=0o644):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    os.chmod(p, mode)
    print("wrote", p)

# --- hysteria main (+ residential when requested) ---
tpl = Path(repo) / "templates/hysteria/server-main.yaml"
main_yaml = tpl.read_text()
for slot in slots:
    main_yaml = slot_map(main_yaml, slot)
write("opt/fleet-hy2/server.yaml", global_map(main_yaml))
if residential:
    r_host, r_port, r_user, r_pass = residential.split(":", 3)
    ws_yaml = (Path(repo) / "templates/hysteria/server-webshare.yaml").read_text()
    for slot in slots:
        ws_yaml = slot_map(ws_yaml, slot)
    ws_yaml = ws_yaml.replace("RESIDENTIAL_PROXY_USER", r_user)
    ws_yaml = ws_yaml.replace("RESIDENTIAL_PROXY_PASS", r_pass)
    ws_yaml = ws_yaml.replace("203.0.113.10:8080", f"{r_host}:{r_port}")
    write("opt/fleet-hy2-webshare/server.yaml", global_map(ws_yaml))

# --- xray ---
xray_text = (Path(repo) / "templates/xray/config.json").read_text()
for slot in slots:
    xray_text = slot_map(xray_text, slot)
if residential:
    r_host, r_port, r_user, r_pass = residential.split(":", 3)
    xray_text = xray_text.replace("RESIDENTIAL_PROXY_USER", r_user)
    xray_text = xray_text.replace("RESIDENTIAL_PROXY_PASS", r_pass)
    xray_text = xray_text.replace("203.0.113.10", r_host).replace('"port": 8080', f'"port": {int(r_port)}')
else:
    # no residential chain in the basic install: scrub the template
    # placeholders so no literal example values remain in the config
    xray_text = xray_text.replace("RESIDENTIAL_PROXY_USER", "unused")
    xray_text = xray_text.replace("RESIDENTIAL_PROXY_PASS", pw())
xray_text = global_map(xray_text)
json.loads(xray_text)  # must stay valid JSON after substitution
write("usr/local/etc/xray/config.json", xray_text)

# --- subscription payload: one clash.yaml per account (+ sub.txt for iphone) ---
clash_tpl = (Path(repo) / "templates/clash/clash.yaml").read_text()
payload = {}
for slot in used:
    text = clash_tpl
    display_tpl = "Alice"  # the skeleton file is written for the first slot
    display = slot.split("_")[0].capitalize()
    text = slot_map(text, slot)
    if display != display_tpl:
        text = text.replace(display_tpl, display)
    text = global_map(text)
    if not residential:
        lines = [l for l in text.splitlines() if "Residential" not in l]
        text = "\n".join(lines) + "\n"
    payload[slot] = {"token": tokens[slot], "filename": "clash.yaml",
                     "content": base64.b64encode(text.encode()).decode()}
    creds["subscriptions"][slot] = f"https://{domain}:2097/{tokens[slot]}/clash.yaml"
    creds["accounts"][slot] = {"hy2_password": hy2_pw[slot], "uuid": uuids[slot], "uuid_ws": uuids_ws[slot]}
    if "iphone" in slot:
        uris = []
        uris.append(f"hysteria2://{slot}:{hy2_pw[slot]}@{domain}:443?sni={domain}#US-{display}-HY2")
        uris.append(f"vless://{uuids[slot]}@{domain}:443?encryption=none&security=reality&sni=www.microsoft.com&fp=chrome&pbk={pub}&sid={short_id}&flow=xtls-rprx-vision#US-{display}-Reality")
        body = base64.b64encode("\n".join(uris).encode()).decode()
        payload[slot] = {"token": tokens[slot], "filename": "sub.txt",
                         "content": base64.b64encode(body.encode()).decode()}
        creds["subscriptions"][slot] = f"https://{domain}:2097/{tokens[slot]}/sub.txt"
write("var/lib/fleet-subscriptions/payload.json", json.dumps(payload), 0o640)
import time
write("var/lib/fleet-subscriptions/userinfo.json",
      json.dumps({"total": quota, "expire": int(time.time()) + 30 * 86400}))
links = {"items": [{"label": s, "url": creds["subscriptions"][s]} for s in used]}
write("var/lib/fleet-panel/links.json", json.dumps(links))

# --- panel + subscription code, config ---
for src, dst in [("panel/panel.py", "usr/local/lib/fleet-panel/panel.py"),
                 ("panel/collect.py", "usr/local/lib/fleet-panel/collect.py"),
                 ("subscription/server.py", "usr/local/lib/fleet-subscriptions/server.py"),
                 ("subscription/traffic-account.py", "usr/local/lib/fleet-subscriptions/traffic-account.py"),
                 ("templates/scripts/fleet-sub-healthcheck.sh", "usr/local/bin/fleet-sub-healthcheck")]:
    text = global_map((Path(repo) / src).read_text())
    if residential and src == "panel/collect.py":
        text = text.replace("203.0.113.10", residential.split(":", 3)[0])
    write(dst, text, 0o755 if dst.endswith("healthcheck") else 0o644)
bind = "127.0.0.1" if panel_bind == "loopback" else "0.0.0.0"
write("etc/fleet-panel/config.json", json.dumps(
    {"cert": "/opt/fleet-hy2/public-tls.crt", "key": "/opt/fleet-hy2/public-tls.key",
     "port": 2096, "bind": bind}))
write("etc/fleet-panel/credentials.json", json.dumps(creds, indent=1), 0o600)
print("render complete")
PYEOF

# ---------------------------------------------------------------- cert
note "certificate (mode: $CERT_MODE)"
if [ "$DRY" = 0 ]; then
  if [ ! -x "$HOME/.acme.sh/acme.sh" ]; then
    TMPD=$(mktemp -d)
    curl -fsSL -o "$TMPD/acme.tar.gz" https://github.com/acmesh-official/acme.sh/archive/refs/tags/3.1.0.tar.gz
    tar -xzf "$TMPD/acme.tar.gz" -C "$TMPD"
    (cd "$TMPD/acme.sh-3.1.0" && ./acme.sh --install --noprofile >/dev/null)
    rm -rf "$TMPD"
  fi
  ACME="$HOME/.acme.sh/acme.sh"
  if [ "$CERT_MODE" = http ]; then
    "$ACME" --issue --standalone -d "$DOMAIN" --server letsencrypt \
      || { echo "error: HTTP-01 issuance failed. Usual cause: TCP 80 is filtered by the provider or the security group lacks it. Open TCP 80, or re-run with --cert dns-cf and CF_Token set." >&2; exit 1; }
  else
    [ -n "${CF_Token:-}" ] || { echo "error: --cert dns-cf needs CF_Token in the environment" >&2; exit 1; }
    CF_Token="$CF_Token" "$ACME" --issue --dns dns_cf -d "$DOMAIN" --server letsencrypt
  fi
  mkdir -p /opt/fleet-hy2
  "$ACME" --install-cert -d "$DOMAIN" \
    --cert-file /opt/fleet-hy2/public-tls.crt \
    --key-file /opt/fleet-hy2/public-tls.key \
    --fullchain-file /opt/fleet-hy2/public-tls.fullchain.crt \
    --reloadcmd "systemctl restart fleet-hy2 fleet-subscription fleet-panel xray 2>/dev/null; true"
  echo "acme.sh cron (auto-renewal):"; crontab -l 2>/dev/null | grep -i acme || echo "  (check ~/.acme.sh)"
else
  echo "[dry-run] acme.sh issue ($CERT_MODE) + install-cert with reloadcmd"
fi

# ---------------------------------------------------------------- system
note "system: users, units, sysctl, panel password"
if [ "$DRY" = 0 ]; then
  id fleet-panel >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin fleet-panel
  id fleet-sub >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin fleet-sub
  id xray >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin xray
  mkdir -p /var/lib/fleet-panel /var/lib/fleet-subscriptions /etc/fleet-subscriptions
  chown fleet-panel:fleet-panel /var/lib/fleet-panel
  chown root:fleet-sub /var/lib/fleet-subscriptions/payload.json; chmod 640 /var/lib/fleet-subscriptions/payload.json
  cp "$REPO_DIR/templates/sysctl/99-proxy-tuning.conf" /etc/sysctl.d/99-proxy-tuning.conf
  printf "net.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n" > /etc/sysctl.d/98-bbr.conf
  sysctl --system >/dev/null || true
  UNITS="fleet-hy2.service xray.service fleet-panel.service fleet-panel-collect.service fleet-panel-collect.timer fleet-subscription.service fleet-sub-healthcheck.service fleet-sub-healthcheck.timer fleet-traffic-account.service fleet-traffic-account.timer fleet-porthop.service"
  [ -n "$RESIDENTIAL" ] && UNITS="$UNITS fleet-hy2-webshare.service"
  for u in $UNITS; do install -m 644 "$REPO_DIR/templates/systemd/$u" "/etc/systemd/system/$u"; done
  systemctl daemon-reload
  if [ ! -f /etc/fleet-panel/password.json ]; then
    read -rsp "set the panel password: " PANEL_PW; echo
    printf '%s\n' "$PANEL_PW" | python3 "$REPO_DIR/panel/setpw.py"
    unset PANEL_PW
  fi
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 443/tcp; ufw allow 443/udp; ufw allow 20000:30000/udp
    ufw allow 2053/tcp; ufw allow 2097/tcp
    [ -n "$RESIDENTIAL" ] && { ufw allow 2054/tcp; ufw allow 2054/udp; }
    [ "$PANEL_BIND" = public ] && ufw allow 2096/tcp
    echo "ufw rules added"
  fi
  /usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
  systemctl enable --now fleet-hy2 xray fleet-subscription fleet-panel fleet-panel-collect.timer fleet-sub-healthcheck.timer fleet-traffic-account.timer fleet-porthop
  [ -n "$RESIDENTIAL" ] && systemctl enable --now fleet-hy2-webshare
else
  echo "[dry-run] users, systemd units, sysctl, ufw, setpw, enable services"
fi

# ---------------------------------------------------------------- verify
note "verification"
if [ "$DRY" = 0 ]; then
  sleep 3
  for u in fleet-hy2 xray fleet-subscription fleet-panel; do
    printf "%-20s %s\n" "$u" "$(systemctl is-active $u || true)"
  done
  code=$(curl -s -k -o /dev/null -w "%{http_code}" https://127.0.0.1:2096/ || true)
  echo "panel on loopback: HTTP $code (want 200 login page)"
  if [ "$PANEL_BIND" = loopback ]; then
    ss -tln | grep ":2096" | grep -q "127.0.0.1" && echo "panel bind: loopback only ✓" \
      || echo "WARNING: panel does not appear loopback-only"
  fi
  FIRST_URL=$(python3 -c "import json; c=json.load(open('$MARKER')); print(next(iter(c['subscriptions'].values())))")
  code=$(curl -s -k -o /dev/null -w "%{http_code}" "$FIRST_URL" || true)
  [ "$code" = 200 ] || code=$(curl -s -k --resolve "$DOMAIN:2097:127.0.0.1" -o /dev/null -w "%{http_code}" "$FIRST_URL" || true)
  echo "subscription fetch: HTTP $code (public path, loopback fallback)"
fi

cat <<EOF

== done ==
Node: https://$DOMAIN
Subscriptions (also in $MARKER, mode 600):
$(python3 -c "import json; c=json.load(open('$MARKER')); [print(f'  {k}: {v}') for k, v in c['subscriptions'].items()]" 2>/dev/null || echo "  (dry-run: rendered under /tmp/fleet-dryrun)")
Panel: bound to $PANEL_BIND, port 2096.
$(if [ "$PANEL_BIND" = loopback ]; then echo "  Reach it with:  ssh -L 2096:127.0.0.1:2096 root@$DOMAIN  then open https://127.0.0.1:2096"; fi)
Certificates renew automatically via acme.sh; services restart on renewal.
Re-running this installer is safe: it detects the install and only reports.
EOF
