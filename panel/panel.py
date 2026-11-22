#!/usr/bin/env python3
"""Fleet read-only status panel v6. Single file, stdlib only.

v5: anomaly log for collector-detected events, responsive desktop
grid and scrollable mobile tabs.

v6: Komari-inspired dark theme, traffic ring gauge + 24h sparkline,
current-rate readout, per-user sparklines, perf gauges with threshold
colors, soft auto-refresh (snapshot-only; collector cadence unchanged).

- Password-only login (PBKDF2 hash in /etc/fleet-panel/password.json),
  in-memory sessions via HttpOnly cookie, per-IP failure lockout.
- Interactive traffic charts (24h / 7d / 30d, click a point for values).
- Every module card has a manual refresh button: it triggers a fresh
  collector run (via a fixed sudo rule) and swaps in the new fragment.
Data comes from the collector's snapshot + history DB. Read-only.
"""
import base64
import hashlib
import hmac
import html
import http.server
import json
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CFG = json.loads(Path('/etc/fleet-panel/config.json').read_text())
PW_FILE = Path('/etc/fleet-panel/password.json')
BASE = Path('/var/lib/fleet-panel')
TZ8 = 8 * 3600
USER_COLORS = {'alice_mac': '#2563eb', 'alice_desktop': '#12b76a',
               'alice_iphone': '#f79009', 'bob': '#9e77ed'}
PALETTE = ['#0ea5e9', '#ef4444', '#84cc16', '#ec4899']
SESSION_TTL = 7 * 86400  # absolute cap from login; activity never extends it
sessions = {}
fails = {}
_collect_lock = threading.Lock()
_last_collect = [0.0]
SESS_FILE = BASE / 'sessions.json'
_sess_lock = threading.Lock()


def _load_sessions():
    try:
        raw = json.loads(SESS_FILE.read_text())
        now = time.time()
        return {k: v for k, v in raw.items()
                if isinstance(v, list) and len(v) == 2
                and isinstance(v[0], (int, float)) and v[0] > now}
    except Exception:
        return {}


def _save_sessions():
    try:
        tmp = SESS_FILE.with_name('sessions.tmp')
        tmp.write_text(json.dumps(sessions))
        tmp.replace(SESS_FILE)
    except Exception:
        pass


sessions.update(_load_sessions())


_pw_ver_cache = [0.0, None]


def _pw_ver():
    """Version stamp of the current panel password. Setting a new password
    bumps it; sessions minted under an older version stop validating on the
    very next request, with no panel restart."""
    try:
        mt = PW_FILE.stat().st_mtime
        if _pw_ver_cache[0] != mt:
            _pw_ver_cache[0] = mt
            rec = json.loads(PW_FILE.read_text())
            # Fall back to a digest of the stored hash record: a password
            # file without a version stamp (e.g. restored from a backup)
            # must still yield a stable version that changes on every
            # password change -- never None, or no session could validate.
            _pw_ver_cache[1] = rec.get('ver') or hashlib.sha256(
                (rec.get('salt', '') + rec.get('hash', '')).encode()).hexdigest()[:16]
        return _pw_ver_cache[1]
    except Exception:
        return None


def _snap_ts():
    try:
        return load_snap()['ts']
    except Exception:
        return 0


def request_fresh():
    """Ask the root collector (via a systemd path trigger) for a fresh sample
    and wait briefly for the new snapshot. Throttled to once per 10s."""
    if not _collect_lock.acquire(blocking=False):
        ts_start = _snap_ts()
        deadline = time.time() + 7
        while time.time() < deadline and _snap_ts() <= ts_start:
            time.sleep(0.25)
        return
    try:
        if time.time() - _last_collect[0] < 10:
            return
        ts0 = _snap_ts()
        (BASE / '.refresh-request').touch()
        deadline = time.time() + 7
        while time.time() < deadline:
            time.sleep(0.25)
            if _snap_ts() > ts0:
                break
        _last_collect[0] = time.time()
    except Exception:
        pass
    finally:
        _collect_lock.release()


def fmt_bytes(n):
    n = float(n)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(n) < 1024 or unit == 'TB':
            return f'{n:.2f} {unit}' if unit in ('GB', 'TB') else f'{n:.0f} {unit}'
        n /= 1024


def fmt_time(ts, with_date=True):
    return time.strftime('%m-%d %H:%M' if with_date else '%H:%M', time.gmtime(ts + TZ8))


def fmt_clock(ts):
    return time.strftime('%H:%M:%S', time.gmtime(ts + TZ8))


def fmt_dur(secs):
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f'{d} 天 {h} 小时'
    if h:
        return f'{h} 小时 {m} 分'
    return f'{m} 分'


def fmt_dur_short(secs):
    d = int(secs) // 86400
    h = (int(secs) % 86400) // 3600
    if d:
        return f'{d} 天'
    if h:
        return f'{h} 小时'
    return f'{int(secs) // 60} 分'


def delta(cur, prev):
    return cur - prev if cur >= prev else cur


def check_password(pw):
    try:
        rec = json.loads(PW_FILE.read_text())
        salt = base64.b64decode(rec['salt'])
        want = base64.b64decode(rec['hash'])
        got = hashlib.pbkdf2_hmac('sha256', pw.encode('utf-8'), salt, int(rec['iter']))
        return hmac.compare_digest(got, want)
    except Exception:
        return False


BASELINE_TS = 1735689600  # REPLACE: unix time when your per-account counters were last all zero


def load_chain(since):
    """Samples from `since`, preceded by one predecessor row so deltas chain:
    the real previous sample, or — when history starts inside the window —
    a virtual zero baseline at BASELINE_TS (HY2 counters genuinely started
    at 0 then, so early cumulative traffic is not lost). Rows are
    (ts, users, is_virtual)."""
    db = sqlite3.connect('file:/var/lib/fleet-panel/history.sqlite3?mode=ro', uri=True)
    rows = db.execute('SELECT ts, users FROM samples WHERE ts >= ? ORDER BY ts',
                      (since,)).fetchall()
    chain = []
    if rows:
        prev = db.execute('SELECT ts, users FROM samples WHERE ts < ? ORDER BY ts DESC LIMIT 1',
                          (rows[0][0],)).fetchone()
        if prev:
            chain.append((prev[0], json.loads(prev[1]), False))
        else:
            chain.append((BASELINE_TS, {u: [0, 0] for u in json.loads(rows[0][1])}, True))
    chain += [(ts, json.loads(u), False) for ts, u in rows]
    db.close()
    return chain


def bucketize(range_key):
    now = int(time.time())
    span, nb = {'24h': (3600, 24), '7d': (6 * 3600, 28), '30d': (86400, 30)}[range_key]
    cur = (now + TZ8) // span
    ids = [cur - nb + 1 + i for i in range(nb)]
    idset = set(ids)
    users = {}
    chain = load_chain(now - (nb * span + span))
    for (t0, u0, virt), (t1, u1, _f) in zip(chain, chain[1:]):
        if virt:
            # Pre-history cumulative: spread evenly over its true window
            # (node launch -> first recorded sample) instead of one spike.
            b0 = (t0 + TZ8) // span
            b1 = (t1 + TZ8) // span
            targets = [b for b in range(b0, b1 + 1) if b in idset]
            if not targets:
                continue
            for user, (tx, rx) in u1.items():
                p_tx, p_rx = u0.get(user, [0, 0])
                d = (delta(tx, p_tx) + delta(rx, p_rx)) / len(targets)
                if d:
                    users.setdefault(user, dict.fromkeys(ids, 0))
                    for b in targets:
                        users[user][b] += d
            continue
        b = (t1 + TZ8) // span
        if b not in idset:
            continue
        for user, (tx, rx) in u1.items():
            p_tx, p_rx = u0.get(user, [0, 0])
            users.setdefault(user, dict.fromkeys(ids, 0))
            users[user][b] += delta(tx, p_tx) + delta(rx, p_rx)
    total = {b: sum(vals[b] for vals in users.values()) for b in ids}

    def lab(b):
        t = time.gmtime(b * span)
        if range_key == '24h':
            return time.strftime('%H:00', t)
        if range_key == '7d':
            return time.strftime('%m-%d %H:00', t)
        return time.strftime('%m-%d', t)

    colors = {}
    for i, u in enumerate(sorted(users)):
        colors[u] = USER_COLORS.get(u) or PALETTE[i % len(PALETTE)]
    return {'labels': [lab(b) for b in ids],
            'users': {u: [vals[b] for b in ids] for u, vals in sorted(users.items())},
            'total': [total[b] for b in ids],
            'colors': colors}


def load_snap():
    return json.loads((BASE / 'status.json').read_text())


def bar(pct):
    pct = max(0.0, min(100.0, pct))
    return f'<div class="barbg"><div class="barfg" data-w="{pct:.1f}" style="width:0%;background:{pct_color(pct)}"></div></div>'

def pct_color(p, warn=70, bad=90):
    if p >= bad:
        return '#f87171'
    if p >= warn:
        return '#fbbf24'
    return '#4a90f7'


def ring(pct):
    pct = max(0.0, min(100.0, pct))
    arc = 263.9 * pct / 100
    return (f'<div class="ring"><svg width="96" height="96" viewBox="0 0 96 96">'
            f'<circle cx="48" cy="48" r="42" fill="none" stroke="rgba(148,163,184,.16)" stroke-width="9"/>'
            f'<circle cx="48" cy="48" r="42" fill="none" stroke="{pct_color(pct)}" stroke-width="9" '
            f'stroke-linecap="round" stroke-dasharray="{arc:.1f} 263.9"/></svg>'
            f'<div class="ringval">{pct:.1f}%</div></div>')


def sparkline(vals, w=240, h=38, color='#4a90f7'):
    vals = [float(v) for v in (vals or [])]
    if len(vals) < 2:
        return ''
    mx = max(vals) or 1.0
    n = len(vals)
    pts = ' '.join(f'{i / (n - 1) * w:.1f},{h - 2 - (v / mx) * (h - 6):.1f}' for i, v in enumerate(vals))
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polygon points="0,{h} {pts} {w},{h}" fill="{color}" opacity="0.14"/>'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.8" '
            f'stroke-linejoin="round" stroke-linecap="round"/></svg>')


def net_rates():
    """(down, up) bytes/sec from the last two history samples (NIC totals)."""
    try:
        db = sqlite3.connect(f'file:{BASE}/history.sqlite3?mode=ro', uri=True)
        rows = db.execute('SELECT ts, m_rx, m_tx FROM samples ORDER BY ts DESC LIMIT 2').fetchall()
        db.close()
        if len(rows) < 2:
            return None
        (t1, rx1, tx1), (t2, rx2, tx2) = rows[0], rows[1]
        dt = t1 - t2
        if dt <= 0:
            return None
        return (delta(rx1, rx2) / dt, delta(tx1, tx2) / dt)
    except Exception:
        return None


def gauge(name, pct):
    if pct is None:
        return (f'<div class="gauge"><span class="gname">{name}</span>'
                f'<div class="gbar"></div><span class="gval">—</span></div>')
    return (f'<div class="gauge"><span class="gname">{name}</span>'
            f'<div class="gbar"><i data-w="{pct:.0f}" style="width:0%;background:{pct_color(pct)}"></i></div>'
            f'<span class="gval">{pct:.0f}%</span></div>')



ICONS = {
    'home': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.8V20a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V9.8"/><path d="M9.5 21v-6h5v6" stroke="none"/></svg>',
    'users': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3.5"/><path d="M3.5 20c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><path d="M15.5 4.9a3.5 3.5 0 0 1 0 6.2"/><path d="M17.6 15.3c1.6.7 2.6 2.2 2.9 4.7"/></svg>',
    'chart': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4v15.5A.5.5 0 0 0 4.5 20H20"/><path d="M7 14.5 10.5 10l3 2.8L18.5 7"/></svg>',
    'server': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01" stroke-width="2.6"/></svg>',
    'link': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 17H7a5 5 0 0 1 0-10h2"/><path d="M15 7h2a5 5 0 0 1 0 10h-2"/><path d="M8 12h8"/></svg>',
    'refresh': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>',
    'copy': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
}


def icon(name):
    return ICONS[name]


def frag_traffic(snap):
    mach = snap['machine']
    used = mach['rx'] + mach['tx']
    pct = used / mach['total'] * 100 if mach['total'] else 0
    online_n = len(snap.get('online', []))
    rates = net_rates()
    if rates:
        down, up = rates
        rate_html = (f'<div class="rates"><span class="rate ratedn"><span class="rarr">↓</span> {fmt_bytes(down)}/s</span>'
                     f'<span class="rate rateup"><span class="rarr">↑</span> {fmt_bytes(up)}/s</span>'
                     f'<span class="ratelab">当前网速（近两分钟均值）</span></div>')
    else:
        rate_html = ''
    try:
        spark = sparkline(bucketize('24h')['total'], 240, 38, '#4a90f7')
    except Exception:
        spark = ''
    side = (f'<div class="trafside">{ring(pct)}'
            + (f'<div>{spark}<div class="sparkcap">近 24 小时 · 逐小时流量</div></div>' if spark else '')
            + '</div>')
    return (f'<div class="traf"><div class="trafmain">'
            f'<div class="big"><span class="cnt" data-v="{used}">0 B</span> <small>/ {fmt_bytes(mach["total"])}</small></div>'
            f'{rate_html}'
            f'<div class="meta"><span>已用 {pct:.2f}%</span><span>上行 {fmt_bytes(mach["tx"])}</span>'
            f'<span>下行 {fmt_bytes(mach["rx"])}</span><span class="hidesm">当前在线 {online_n} 个账号</span>'
            f'<span>周期到期 {fmt_time(mach["expire"])}</span></div></div>{side}</div>')


def frag_overview(snap):
    """Overview page: traffic card + (mobile-only) KPI tiles, account mini
    chart and service summary, so the first screen is a real dashboard."""
    perf, mach = snap['perf'], snap['machine']
    online = snap.get('online', [])
    cpu = f'{perf["cpu_pct"]}%' if perf.get('cpu_pct') is not None else '—'
    mem_pct = (perf['mem_total'] - perf['mem_avail']) / perf['mem_total'] * 100 if perf['mem_total'] else 0
    tiles = (
        '<div class="tiles mobonly">'
        f'<div class="tile"><div class="tlab">在线账号</div><div class="tval">{len(online)}<span class="tunit"> / 4</span></div></div>'
        f'<div class="tile"><div class="tlab">CPU</div><div class="tval">{cpu}</div></div>'
        f'<div class="tile"><div class="tlab">内存</div><div class="tval">{mem_pct:.0f}<span class="tunit">%</span></div></div>'
        f'<div class="tile"><div class="tlab">已运行</div><div class="tval">{fmt_dur_short(perf["uptime"])}</div></div>'
        '</div>')
    users = dict(snap['users'])
    for u in ['alice_mac', 'alice_desktop', 'alice_iphone', 'bob']:
        users.setdefault(u, {'tx': 0, 'rx': 0})
    max_total = max([v['tx'] + v['rx'] for v in users.values()] + [1])
    mrows = []
    for user in sorted(users, key=lambda u: -(users[u]['tx'] + users[u]['rx'])):
        v = users[user]
        tot = v['tx'] + v['rx']
        w = max(tot / max_total * 100, 1.5) if tot > 0 else 0
        dot = '<span class="dot on"></span>' if user in online else '<span class="dot"></span>'
        color = USER_COLORS.get(user, '#64748b')
        mrows.append(f'<div class="mrow">{dot}<span class="mname">{html.escape(user)}</span>'
                     f'<span class="mbar"><i data-w="{w:.1f}" style="width:0%;background:{color}"></i></span>'
                     f'<span class="mtot">{fmt_bytes(tot)}</span></div>')
    mini_users = ('<div class="card mobonly"><div class="cardhead"><h2>账号速览</h2>'
                  '<button class="rfbtn" data-goto="users"><span>详情 ›</span></button></div>'
                  + ''.join(mrows) + '</div>')
    svc = snap['services']

    def sdot(ok):
        return '<span class="dot on"></span>' if ok else '<span class="dot bad"></span>'

    strip = (f'<span class="sitem">{sdot(svc.get("fleet-hy2"))}节点</span>'
             f'<span class="sitem">{sdot(svc.get("fleet-subscription"))}订阅</span>'
             f'<span class="sitem">{sdot(svc.get("fleet-panel"))}面板</span>')
    cert_vals = [v for v in snap['certs'].values() if v]
    cert_line = f'证书最早到期 {fmt_time(min(cert_vals))} · ' if cert_vals else ''
    mini_svc = ('<div class="card mobonly"><div class="cardhead"><h2>服务状态</h2>'
                '<button class="rfbtn" data-goto="services"><span>详情 ›</span></button></div>'
                f'<div class="svcstrip">{strip}</div>'
                + f'<div class="umeta" style="margin-top:8px">{cert_line}服务器套餐到期 {fmt_time(mach["expire"])}</div></div>')
    return (f'<div class="card z1">{head("整机流量（四账号合计）")}{rfbtn("traffic")}{frag_traffic(snap)}</div>'
            + tiles + mini_users + mini_svc)


def frag_users(snap):
    try:
        b24 = bucketize('24h')['users']
    except Exception:
        b24 = {}
    users = dict(snap['users'])
    for user in ['alice_mac', 'alice_desktop', 'alice_iphone', 'bob']:
        users.setdefault(user, {'tx': 0, 'rx': 0})
    max_total = max([v['tx'] + v['rx'] for v in users.values()] + [1])
    rows = []
    for idx, user in enumerate(sorted(users, key=lambda u: -(users[u]['tx'] + users[u]['rx']))):
        v = users[user]
        tot = v['tx'] + v['rx']
        color = USER_COLORS.get(user, '#64748b')
        w_rx = v['rx'] / max_total * 100
        w_tx = v['tx'] / max_total * 100
        if v['rx'] > 0:
            w_rx = max(w_rx, 0.8)
        if v['tx'] > 0:
            w_tx = max(w_tx, 0.8)
        online = user in snap.get('online', [])
        dot = '<span class="dot on"></span>' if online else '<span class="dot"></span>'
        delay = idx * 70
        rows.append(
            f'<div class="urow"><div class="uhead"><span class="uname">{dot}{html.escape(user)}</span>'
            f'<span class="utotal cnt" data-v="{tot}">0 B</span></div>'
            f'<div class="ubar"><i data-w="{w_rx:.2f}" style="width:0%;background:{color};transition-delay:{delay}ms"></i>'
            f'<i data-w="{w_tx:.2f}" style="width:0%;background:{color};opacity:.35;transition-delay:{delay}ms"></i></div>'
            f'<div class="ufoot"><span class="umeta">下行 {fmt_bytes(v["rx"])} · 上行 {fmt_bytes(v["tx"])} · {"在线" if online else "离线"}</span>'
            f'<span class="uspark">{sparkline(b24.get(user, []), 96, 22, color)}</span></div></div>')
    return '<div class="ulegend2">条形按合计占比 · 深色＝下行 · 浅色＝上行</div>' + ''.join(rows)


def frag_perf(snap):
    perf = snap['perf']
    mem_used = perf['mem_total'] - perf['mem_avail']
    mem_pct = mem_used / perf['mem_total'] * 100 if perf['mem_total'] else 0
    disk_pct = perf['disk_used'] / perf['disk_total'] * 100 if perf['disk_total'] else 0
    cpu = f'{perf["cpu_pct"]}%' if perf.get('cpu_pct') is not None else '—'
    gauges = ('<div class="gauges">'
              + gauge('CPU', perf.get('cpu_pct'))
              + gauge('内存', mem_pct)
              + gauge('磁盘', disk_pct)
              + '</div>')
    return (gauges + '<div class="tw"><table class="t2">'
            f'<tr><td>负载（1/5/15 分钟）</td><td>{perf["load"][0]:.2f} / {perf["load"][1]:.2f} / {perf["load"][2]:.2f}</td></tr>'
            f'<tr><td>CPU 使用率</td><td>{cpu}</td></tr>'
            f'<tr><td>内存</td><td>{fmt_bytes(mem_used)} / {fmt_bytes(perf["mem_total"])}（{mem_pct:.0f}%）</td></tr>'
            f'<tr><td>磁盘</td><td>{fmt_bytes(perf["disk_used"])} / {fmt_bytes(perf["disk_total"])}（{disk_pct:.0f}%）</td></tr>'
            f'<tr><td>已连续运行</td><td>{fmt_dur(perf["uptime"])}</td></tr>'
            '</table></div>')


def frag_services(snap):
    now = int(time.time())
    rows = []
    svc_names = {'fleet-hy2': '节点服务（HY2）', 'fleet-hy2-webshare': '节点服务（HY2·Webshare）',
                 'reality': '节点服务（Reality）', 'reality-ws': '节点服务（Reality·Webshare）',
                 'fleet-subscription': '订阅服务',
                 'fleet-panel': '本面板', 'fleet-sub-healthcheck.timer': '自愈看门狗',
                 'fleet-traffic-account.timer': '流量记账'}
    for key, label in svc_names.items():
        ok = snap['services'].get(key)
        cell = '<span class="dot on"></span>正常' if ok else '<span class="dot bad"></span>异常'
        rows.append(f'<tr><td>{label}</td><td>{cell}</td></tr>')
    for name, exp in snap['certs'].items():
        if exp:
            days = (exp - now) // 86400
            rows.append(f'<tr><td>证书 {html.escape(name)}</td><td>到期 {fmt_time(exp)}（剩 {days} 天，自动续期）</td></tr>')
    return ('<div class="tw"><table class="t2">' + ''.join(rows) + '</table></div>'
            f'<div class="muted" style="margin-top:8px">快照时间 {fmt_time(snap["ts"])} · 历史数据自部署之日起积累</div>')


def frag_events(snap):
    evs = snap.get('events') or []
    if not evs:
        return '<div class="evempty">暂无异常记录</div>'
    dots = {'down': 'bad', 'warn': 'warn', 'heal': 'warn', 'restart': 'warn',
            'up': 'on', 'cert': 'on', 'boot': 'on'}
    rows = []
    for e in evs:
        d = dots.get(e.get('kind'), 'on')
        rows.append(f'<div class="evrow"><span class="dot {d}"></span>'
                    f'<span class="evtime">{fmt_time(e["ts"])}</span>'
                    f'<span class="evtext">{html.escape(e["text"])}</span></div>')
    return ('<div class="evlist">' + ''.join(rows) + '</div>'
            '<div class="evpager"><button class="evbtn" data-ev="-1">‹ 上页</button>'
            '<span class="evpage"></span><button class="evbtn" data-ev="1">下页 ›</button></div>')



def frag_webshare(snap):
    ws = snap.get('webshare')
    if not ws:
        return '<div class="evempty">暂无数据（等待首次同步）</div>'
    pct = ws['pct']
    if ws.get('projected_gb') is None:
        proj = '<tr><td>按当前速度预计整周期</td><td>新周期数据不足，明日再看</td></tr>'
    else:
        proj = (f'<tr><td>按当前速度预计整周期</td><td>约 {ws["projected_gb"]:.0f} GB'
                + ('（会超额度）' if ws['projected_pct'] >= 100 else '（在额度内）') + '</td></tr>')
    renew = '到期自动续费' if ws.get('will_renew') else '未开自动续费'
    place = f'（{html.escape(ws["place"])}）' if ws.get('place') else ''
    return ('<div class="tw"><table class="t2">'
            f'<tr><td>当前出口</td><td>{html.escape(str(ws["ip"]))}:{ws["port"]}{place}</td></tr>'
            f'<tr><td>本周期已用</td><td>{ws["used_gb"]:.2f} GB / {ws["quota_gb"]:.0f} GB（{pct:.1f}%）</td></tr>'
            '</table></div>'
            f'<div style="margin:10px 0 6px">{bar(pct)}</div>'
            '<div class="tw"><table class="t2">'
            + proj
            + f'<tr><td>周期</td><td>{ws["cycle_start"][:10]} 至 {ws["cycle_end"][:10]} · 剩 {ws["days_left"]:.0f} 天 · {renew} ${ws["monthly_price"]:.2f}/月</td></tr>'
            + f'<tr><td>免费换 IP 次数</td><td>已用 {ws["replacements_used"]} / {ws["replacements_total"]}（剩 {ws["replacements_total"] - ws["replacements_used"]}）</td></tr>'
            + '</table></div>'
            + f'<div class="muted" style="margin-top:8px">数据更新于 {fmt_time(ws["updated_ts"])} · ' + ('点刷新实时拉取最新数据' if ws.get('live') else '每小时自动同步，点刷新重读最新同步') + '</div>')

FRAGS = {'traffic': frag_overview, 'users': frag_users, 'perf': frag_perf,
         'services': frag_services, 'events': frag_events, 'webshare': frag_webshare}

CSS = '''
* { box-sizing: border-box; }
:root { color-scheme: dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", sans-serif; margin: 0; background: #0a0e17; background-image: radial-gradient(1100px 520px at 85% -10%, rgba(74,144,247,.13), rgba(74,144,247,0) 60%), radial-gradient(900px 500px at -10% 110%, rgba(34,197,94,.06), rgba(34,197,94,0) 60%); background-attachment: fixed; color: #e6edf7; font-size: 15px; -webkit-font-smoothing: antialiased; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 22px 20px 30px; }
.toprow { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
h1 { font-size: 20px; font-weight: 700; letter-spacing: -.02em; margin: 0; line-height: 1.3; }
.subtitle { color: #8d9db2; font-size: 12.5px; margin-top: 4px; }
.logout { color: #a7b4c6; font-size: 13.5px; text-decoration: none; border: 1px solid rgba(148,163,184,.22); background: rgba(148,163,184,.05); padding: 6px 14px; border-radius: 999px; white-space: nowrap; }
.logout:hover { color: #7db1ff; border-color: rgba(74,144,247,.4); }
.card { background: rgba(148,163,184,.055); border: 1px solid rgba(148,163,184,.14); border-radius: 16px; padding: 20px 22px; margin: 14px 0; box-shadow: 0 8px 24px rgba(0,0,0,.28); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
.cardhead { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 14px; }
h2 { font-size: 15px; font-weight: 670; margin: 0; padding-left: 9px; border-left: 3px solid #4a90f7; line-height: 1.35; overflow-wrap: anywhere; }
.rf { display: inline-flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 6px; }
.upd { color: #7e90a9; font-size: 12px; font-variant-numeric: tabular-nums; }
.upd:empty { display: none; }
.rfbtn { border: 1px solid rgba(148,163,184,.22); background: rgba(148,163,184,.05); color: #a7b4c6; border-radius: 999px; padding: 4px 12px; font-size: 12.5px; cursor: pointer; white-space: nowrap; display: inline-flex; align-items: center; gap: 5px; transition: color .18s, border-color .18s, transform .12s; font-family: inherit; }
.rfbtn svg { width: 13px; height: 13px; }
.rfbtn:hover { color: #7db1ff; border-color: rgba(74,144,247,.4); }
.rfbtn:active { transform: scale(.94); }
.rfbtn:disabled { opacity: .55; cursor: default; }
.rfbtn.loading svg { animation: spin .7s linear infinite; }
.dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #3f5069; margin-right: 6px; vertical-align: 1px; }
.dot.on { background: #22c55e; box-shadow: 0 0 0 3px rgba(34,197,94,.16); }
.dot.bad { background: #ef4444; box-shadow: 0 0 0 3px rgba(239,68,68,.16); }
.muted { color: #8d9db2; font-size: 13px; }
.big { font-size: 30px; font-weight: 750; letter-spacing: -.03em; font-variant-numeric: tabular-nums; }
.big small { font-size: 16px; font-weight: 600; color: #8d9db2; letter-spacing: 0; }
.meta { display: flex; flex-wrap: wrap; gap: 6px 18px; margin-top: 10px; color: #93a3ba; font-size: 13.5px; font-variant-numeric: tabular-nums; }
.barbg { background: rgba(148,163,184,.16); border-radius: 999px; height: 10px; overflow: hidden; margin-top: 12px; }
.barfg { height: 100%; background: #4a90f7; border-radius: 999px; transition: width .9s cubic-bezier(.22,.8,.3,1); }
.tw { overflow-x: auto; -webkit-overflow-scrolling: touch; }
table { width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }
th { text-align: left; color: #8d9db2; font-weight: 500; font-size: 12.5px; padding: 4px 6px 8px; border-bottom: 1px solid rgba(148,163,184,.14); white-space: nowrap; }
td { padding: 9px 6px; border-bottom: 1px solid rgba(148,163,184,.08); vertical-align: top; }
.t2 { table-layout: fixed; }
.t2 td { white-space: normal; overflow-wrap: anywhere; }
.t2 td:first-child { color: #93a3ba; }
.t2 td:last-child { font-weight: 550; }
tr:last-child td { border-bottom: none; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.grid2 .card { margin: 0; }
.grid2 > .card { min-width: 0; }
.rangebtns { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
.rbtn { border: 1px solid rgba(148,163,184,.22); background: rgba(148,163,184,.05); color: #a7b4c6; border-radius: 999px; padding: 6px 16px; font-size: 13.5px; cursor: pointer; font-family: inherit; }
.rbtn.on { background: #2f6fd0; color: #fff; border-color: #2f6fd0; }
.detail { font-size: 13.5px; color: #cfe1ff; background: rgba(74,144,247,.12); border-radius: 10px; padding: 9px 12px; margin: 8px 0 4px; min-height: 20px; font-variant-numeric: tabular-nums; }
.chartsep { height: 14px; }
svg circle.pt { cursor: pointer; }
.legend { font-size: 13px; margin: 2px 0 4px; color: #a7b4c6; }
.urow { padding: 10px 0; border-bottom: 1px solid rgba(148,163,184,.08); }
.urow:last-child { border-bottom: none; }
.uhead { display: flex; justify-content: space-between; align-items: baseline; gap: 12px; }
.uname { min-width: 0; font-weight: 600; font-size: 14.5px; overflow-wrap: anywhere; }
.utotal { flex-shrink: 0; font-weight: 700; font-variant-numeric: tabular-nums; }
.ubar { display: flex; height: 10px; background: rgba(148,163,184,.14); border-radius: 999px; overflow: hidden; margin: 7px 0 5px; }
.ubar i { display: block; height: 100%; transition: width .9s cubic-bezier(.22,.8,.3,1); }
.umeta { font-size: 12.5px; color: #8d9db2; font-variant-numeric: tabular-nums; }
.ufoot { display: flex; justify-content: space-between; align-items: center; gap: 10px; }
.uspark { flex-shrink: 0; opacity: .9; line-height: 0; }
.ulegend2 { font-size: 12.5px; color: #8d9db2; margin-bottom: 2px; }
.linkrow2 { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 9px 0; border-bottom: 1px solid rgba(148,163,184,.08); }
.linkrow2 > div { min-width: 0; overflow-wrap: anywhere; }
.linkrow2:last-of-type { border-bottom: none; }
.lname { display: block; font-size: 14px; color: #e6edf7; }
.lmask { display: block; font-size: 12px; color: #62748e; margin-top: 2px; word-break: break-all; }
.cpbtn { border: 1px solid rgba(74,144,247,.35); background: rgba(74,144,247,.12); color: #7db1ff; border-radius: 999px; padding: 6px 14px; font-size: 13px; cursor: pointer; white-space: nowrap; flex-shrink: 0; display: inline-flex; align-items: center; gap: 5px; transition: background .18s, color .18s, transform .12s; font-family: inherit; }
.cpbtn svg { width: 13px; height: 13px; }
.cpbtn:active { transform: scale(.94); }
.cpbtn.done { background: #2f6fd0; color: #fff; border-color: #2f6fd0; }
.brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
.brandbadge { width: 36px; height: 36px; border-radius: 11px; background: rgba(74,144,247,.15); color: #7db1ff; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.brandbadge svg { width: 20px; height: 20px; }
.brandtxt { min-width: 0; }
.brandtxt h1 { overflow-wrap: anywhere; }
.subtitle { white-space: normal; line-height: 1.4; }
.logout { flex-shrink: 0; }
body.app h1 { font-size: 17.5px; }
.loginbadge { width: 46px; height: 46px; border-radius: 13px; background: rgba(74,144,247,.15); color: #7db1ff; display: flex; align-items: center; justify-content: center; margin-bottom: 14px; }
.loginbadge svg { width: 24px; height: 24px; }
.mobonly { display: none !important; }
.tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 14px 0 0; }
.tile { background: rgba(148,163,184,.055); border: 1px solid rgba(148,163,184,.14); border-radius: 14px; padding: 13px 15px; box-shadow: 0 8px 24px rgba(0,0,0,.28); animation: rise .45s ease both; }
.tlab { font-size: 12px; color: #8d9db2; }
.tval { font-size: 23px; font-weight: 750; letter-spacing: -.02em; margin-top: 3px; font-variant-numeric: tabular-nums; }
.tunit { font-size: 13px; color: #8d9db2; font-weight: 600; }
.mrow { display: flex; align-items: center; gap: 8px; padding: 7px 0; border-bottom: 1px solid rgba(148,163,184,.08); font-size: 13.5px; }
.mrow:last-child { border-bottom: none; }
.mrow .dot { margin-right: 0; flex-shrink: 0; }
.mname { min-width: 0; overflow-wrap: anywhere; }
.mbar { flex: 1; height: 6px; background: rgba(148,163,184,.14); border-radius: 999px; overflow: hidden; }
.mbar i { display: block; height: 100%; border-radius: 999px; transition: width .9s cubic-bezier(.22,.8,.3,1); }
.mtot { font-variant-numeric: tabular-nums; color: #c3cedd; flex-shrink: 0; }
.mstat { margin-left: auto; color: #93a3ba; }
.svcstrip { display: flex; flex-wrap: wrap; gap: 8px 18px; }
.sitem { display: inline-flex; align-items: center; font-size: 13.5px; color: #c3cedd; }
.sitem .dot { margin-right: 5px; }
.cardbody, .cbinner { display: contents; }
.chev { display: none; }
.noted { margin-top: 12px; }
.noted summary { font-size: 13px; color: #8d9db2; cursor: pointer; list-style: none; display: inline-block; }
.noted summary::-webkit-details-marker { display: none; }
.noted summary::after { content: " ›"; }
a { color: #7db1ff; text-decoration: none; }
.foot { text-align: center; color: #62748e; font-size: 12px; margin-top: 18px; }
.view { display: contents; }
.tabbar { display: none; }
.loginbox { max-width: 380px; margin: 11vh auto 0; background: #101827; border: 1px solid rgba(148,163,184,.16); border-radius: 18px; padding: 30px 28px; box-shadow: 0 12px 32px rgba(0,0,0,.4); }
.loginbox h1 { font-size: 19px; }
.loginbox input { width: 100%; padding: 11px 12px; font-size: 16px; border: 1px solid rgba(148,163,184,.25); background: rgba(148,163,184,.05); color: #e6edf7; border-radius: 10px; margin: 14px 0 4px; outline: none; }
.loginbox input:focus { border-color: #4a90f7; box-shadow: 0 0 0 3px rgba(74,144,247,.22); }
.loginbtn { width: 100%; background: #2f6fd0; color: #fff; border: none; border-radius: 10px; padding: 11px; font-size: 15.5px; font-weight: 600; cursor: pointer; margin-top: 10px; font-family: inherit; }
.loginbtn:hover { background: #3b82f6; }
.err { color: #f87171; font-size: 13.5px; min-height: 18px; }
.dot.warn { background: #f59e0b; box-shadow: 0 0 0 3px rgba(245,158,11,.16); }
.evrow { display: flex; align-items: baseline; gap: 8px; padding: 6px 0; border-bottom: 1px solid rgba(148,163,184,.08); font-size: 13.5px; }
.evrow:last-child { border-bottom: none; }
.evrow .dot { flex-shrink: 0; align-self: center; margin-right: 0; }
.evtime { color: #8d9db2; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.evtext { min-width: 0; color: #c3cedd; overflow-wrap: anywhere; }
.evempty { color: #8d9db2; font-size: 13.5px; padding: 6px 0; }
.evpager { display: flex; align-items: center; gap: 14px; margin-top: 8px; }
.evbtn { border: 1px solid rgba(148,163,184,.22); background: rgba(148,163,184,.05); color: #a7b4c6; border-radius: 999px; padding: 3px 12px; font-size: 12.5px; cursor: pointer; font-family: inherit; }
.evbtn:active { transform: scale(.94); }
.evpage { color: #62748e; font-size: 12px; font-variant-numeric: tabular-nums; }
.blks { display: contents; }
body.app { min-height: 100vh; min-height: 100dvh; }
body.app .wrap { max-width: 1540px; }
body.app .card { min-width: 0; margin: 0; }
body.app .tiles { margin: 0; }
.z1 { background: linear-gradient(155deg, rgba(74,144,247,.15), rgba(148,163,184,.04) 55%); border-color: rgba(74,144,247,.25); }
#uchart svg, #mchart svg { display: block; max-width: 100%; }
:is(button, a, summary):focus-visible { outline: 2px solid #4a90f7; outline-offset: 3px; }
.traf { display: flex; gap: 22px; align-items: center; }
.trafmain { flex: 1; min-width: 0; }
.trafside { display: flex; flex-direction: column; align-items: center; gap: 9px; flex-shrink: 0; }
.ring { position: relative; width: 96px; height: 96px; flex-shrink: 0; }
.ring svg { transform: rotate(-90deg); display: block; }
.ringval { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 750; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }
.rates { display: flex; align-items: baseline; flex-wrap: wrap; gap: 6px 16px; margin-top: 9px; font-variant-numeric: tabular-nums; }
.rate { font-size: 21px; font-weight: 700; letter-spacing: -.01em; }
.rate .rarr { font-size: 15px; font-weight: 700; }
.ratedn { color: #38bdf8; }
.rateup { color: #4ade80; }
.ratelab { font-size: 12px; color: #62748e; }
.sparkcap { font-size: 11.5px; color: #62748e; text-align: center; }
.gauges { display: grid; gap: 10px; margin-bottom: 13px; }
.gauge { display: grid; grid-template-columns: 40px 1fr 50px; align-items: center; gap: 10px; font-size: 13px; }
.gname { color: #93a3ba; }
.gval { text-align: right; font-weight: 650; font-variant-numeric: tabular-nums; }
.gbar { height: 12px; background: rgba(148,163,184,.16); border-radius: 999px; overflow: hidden; }
.gbar i { display: block; height: 100%; border-radius: 999px; transition: width .9s cubic-bezier(.22,.8,.3,1); }
@media (min-width: 861px) {
  body.app .wrap {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-template-areas:
      "top top"
      "traffic traffic"
      "users perf"
      "charts charts"
      "services webshare"
      "events links"
      "foot foot";
    gap: 16px;
    padding: 24px clamp(20px, 3vw, 48px) 28px;
  }
  .toprow { grid-area: top; }
  body.app h1 { font-size: 22px; }
  body.app .foot { grid-area: foot; margin: 0; }
  #mod-traffic, .view[data-view="services"] .grid2 { display: contents; }
  .z1 { grid-area: traffic; }
  .z2 { grid-area: users; }
  .z3 { grid-area: services; }
  .z4 { grid-area: perf; }
  .z5 { grid-area: charts; }
  .z6 { grid-area: events; }
  .z7 { grid-area: links; }
  .z8 { grid-area: webshare; }
  .blks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }
  .chartsep { display: none; }
  .blks > div { display: flex; flex-direction: column; gap: 5px; min-width: 0; padding: 12px; border: 1px solid rgba(148,163,184,.12); border-radius: 12px; }
  .blks .legend { min-height: 2.8em; line-height: 1.4; }
  .blks .detail { margin: 0; }
  .t2 td:first-child { width: 42%; }
}
@media (min-width: 1280px) {
  body.app .wrap {
    grid-template-columns: repeat(3, minmax(0, 1fr));
    grid-template-areas:
      "top top top"
      "traffic traffic traffic"
      "users charts charts"
      "perf services events"
      "webshare webshare links"
      "foot foot foot";
  }
}
@media (max-height: 820px) {
  .card { padding: 15px 17px; }
  .urow { padding: 7px 0; }
  .linkrow2 { padding: 7px 0; }
  td { padding: 7px 6px; }
  .big { font-size: 26px; }
  .evrow { padding: 5px 0; }
}
@media (max-height: 680px) {
  .card { padding: 12px 14px; }
  h2 { font-size: 14px; }
  .cardhead { margin-bottom: 9px; }
  .urow { padding: 5px 0; }
  .linkrow2 { padding: 5px 0; }
  td { padding: 5px 6px; font-size: 13px; }
  .big { font-size: 23px; }
  .tile { padding: 9px 10px; }
  .tval { font-size: 19px; }
  .meta { font-size: 12.5px; }
  .evrow { padding: 4px 0; font-size: 13px; }
}
@media (max-width: 860px) {
  .grid2 { grid-template-columns: 1fr; }
  .view { display: none; }
  .view.von { display: block; }
  .mobonly { display: block !important; }
  .tiles.mobonly { display: grid !important; }
  .tabbar { display: flex; position: fixed; left: 0; right: 0; bottom: 0; z-index: 50; background: rgba(10,14,23,.88); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); border-top: 1px solid rgba(148,163,184,.14); padding: 6px 6px calc(6px + env(safe-area-inset-bottom)); }
  .tabbtn { flex: 1; background: none; border: none; display: flex; flex-direction: column; align-items: center; gap: 2px; font-size: 10.5px; color: #8d9db2; padding: 4px 0 2px; cursor: pointer; font-family: inherit; transition: color .18s; }
  .tabbtn svg { width: 22px; height: 22px; transition: transform .18s; }
  .tabbtn:active svg { transform: scale(.88); }
  .tabbtn.on { color: #7db1ff; font-weight: 600; }
  .view.von { animation: viewin .26s ease; }
}
.card { animation: rise .45s ease both; }
svg .ln { stroke-dasharray: 1; stroke-dashoffset: 1; animation: dashmove .9s ease-out forwards; }
svg .dt { opacity: 0; animation: fadein .4s ease .4s forwards; }
@keyframes dashmove { to { stroke-dashoffset: 0; } }
@keyframes fadein { to { opacity: 1; } }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@keyframes viewin { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) {
  * { animation: none !important; transition: none !important; }
  svg .ln { stroke-dashoffset: 0; }
  svg .dt { opacity: 1; }
}
@media (max-width: 860px) {
  body.app .wrap { display: flex; flex-direction: column; gap: 12px; padding: 12px 12px calc(68px + env(safe-area-inset-bottom)); }
  body.app .foot { display: none; }
  body.app .toprow { flex-shrink: 0; }
  body.app .view.von, #mod-traffic { display: flex; flex-direction: column; gap: 12px; }
  .hidesm { display: none; }
  .mrow { padding: 5px 0; }
  .barbg { margin-top: 8px; }
  .meta { margin-top: 8px; }
  .tiles { grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .tile { padding: 11px 10px; }
  .tval { font-size: 20px; }
  .segwrap { display: flex !important; background: rgba(148,163,184,.12); border-radius: 10px; padding: 3px; margin-bottom: 12px; }
  .segbtn { flex: 1; border: none; background: none; padding: 6px 0; font-size: 13.5px; color: #93a3ba; border-radius: 8px; cursor: pointer; font-family: inherit; }
  .segbtn.on { background: rgba(255,255,255,.14); color: #fff; font-weight: 600; box-shadow: 0 1px 3px rgba(0,0,0,.35); }
  #chartscard[data-cv="users"] .blk-total { display: none; }
  #chartscard[data-cv="total"] .blk-users { display: none; }
  .view[data-view="services"] .grid2 { gap: 12px; }
  .acc .cardhead { cursor: pointer; }
  .acc:not(.open) .cardhead { margin-bottom: 0; }
  .acc:not(.open) .cardbody { display: none; }
  .chev { display: inline-flex; align-items: center; justify-content: center; min-width: 30px; min-height: 30px; padding: 0; border: 0; border-radius: 8px; background: rgba(74,144,247,.13); color: #8db4f8; cursor: pointer; }
  .chev svg { transition: transform .2s; }
  .chev svg { width: 16px; height: 16px; }
  .acc.open .chev svg { transform: rotate(180deg); }
  .subtitle { font-size: 11.5px; }
  .traf { flex-direction: column; align-items: stretch; gap: 12px; }
  .trafside { flex-direction: row; justify-content: space-between; align-items: center; }
}
@media (max-width: 380px) { .tiles { grid-template-columns: 1fr 1fr; } }
@media (max-width: 640px) {
  .wrap { padding: 14px 12px 22px; }
  .card { padding: 16px; border-radius: 14px; }
  h1 { font-size: 18px; }
  .big { font-size: 24px; }
  .rate { font-size: 18px; }
  .loginbox { margin: 8vh 14px 0; padding: 24px 20px; }
  .cardhead { flex-wrap: wrap; }
  .tw { overflow-x: visible; }
  table { font-size: 13.5px; }
  table.t2 tr { display: flex; justify-content: space-between; align-items: baseline; gap: 14px; border-bottom: 1px solid rgba(148,163,184,.08); }
  table.t2 tr:last-child { border-bottom: none; }
  table.t2 td { border-bottom: none; padding: 9px 0; white-space: normal; }
  table.t2 td:first-child { flex: 0 1 42%; min-width: 0; }
  table.t2 td:last-child { flex: 1 1 0; min-width: 0; text-align: right; }
}
'''


def head(title):
    return f'''<div class="cardhead"><h2>{title}</h2>'''


def rfbtn(module, chev=False):
    expanded = 'true' if module == 'perf' else 'false'
    c = (f'<button type="button" class="chev" aria-label="展开或收起详情" aria-controls="mod-{module}" '
         f'aria-expanded="{expanded}">'
         '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg></button>' if chev else '')
    return (f'<span class="rf"><span class="upd" id="upd-{module}"></span>'
            f'<button class="rfbtn" data-m="{module}">{icon("refresh")}<span>刷新</span></button>{c}</span></div>')


def login_page(error=''):
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>登录 · 节点面板</title><style>{CSS}</style></head>
<body><div class="loginbox">
<div class="loginbadge">{icon("chart")}</div>
<h1>示例节点 · 状态面板</h1>
<div class="subtitle">请输入密码登录</div>
<form method="post" action="/login">
<input type="password" name="password" autocomplete="current-password" autofocus>
<div class="err">{html.escape(error)}</div>
<button class="loginbtn" type="submit">登 录</button>
</form></div></body></html>'''


def render_panel():
    snap = load_snap()
    links = []
    lf = BASE / 'links.json'
    if lf.exists():
        links = json.loads(lf.read_text()).get('items', [])
    now = int(time.time())
    def mask_url(u):
        p = urlsplit(u)
        fname = p.path.rsplit('/', 1)[-1]
        return f'{p.scheme}://{p.netloc}/••••••/{fname}'

    link_rows = ''.join(
        f'<div class="linkrow2"><div><span class="lname">{html.escape(it["label"])}</span>'
        f'<span class="lmask">{html.escape(mask_url(it["url"]))}</span></div>'
        f'<button class="cpbtn" data-i="{i}">{icon("copy")}<span>复制链接</span></button></div>'
        for i, it in enumerate(links))
    links_json = json.dumps([{'url': it['url']} for it in links], ensure_ascii=False).replace('</', '<\\/')
    data = {rk: bucketize(rk) for rk in ('24h', '7d', '30d')}
    data_json = json.dumps(data, ensure_ascii=False).replace('</', '<\\/')

    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>示例节点 · 状态面板</title><style>{CSS}</style></head>
<body class="app"><div class="wrap">
<div class="toprow"><div class="brand"><span class="brandbadge">{icon("chart")}</span><div class="brandtxt"><h1>示例节点 · 状态面板</h1>
<div class="subtitle">只读面板</div></div></div>
<a class="logout" href="/logout">退出</a></div>

<section class="view von" data-view="overview">
<div id="mod-traffic">{frag_overview(snap)}</div>
</section>

<section class="view" data-view="users">
<div class="card grow z2">{head('各账号用量（自新节点上线起累计）')}{rfbtn('users')}
<div id="mod-users">{frag_users(snap)}</div></div>
</section>

<section class="view" data-view="charts">
<div class="card z5" id="chartscard" data-cv="users">{head('流量曲线')}{rfbtn('charts')}
<div class="segwrap mobonly"><button class="segbtn on" data-cv="users">各账号</button><button class="segbtn" data-cv="total">合计</button></div>
<div class="rangebtns">
<button data-range="24h" class="rbtn on">24 小时</button><button data-range="7d" class="rbtn">一周</button><button data-range="30d" class="rbtn">一月</button>
<span class="muted">点曲线上的点看具体数值</span></div>
<div class="blks">
<div class="blk-users">
<div class="legend" id="ulegend"></div>
<div id="uchart"></div><div class="detail" id="udet">各账号流量 · 点图上的点查看</div>
</div>
<div class="blk-total">
<div class="chartsep"></div>
<div class="legend">四账号合计（等于上方各账号相加）</div>
<div id="mchart"></div><div class="detail" id="mdet">四账号合计 · 点图上的点查看</div>
</div>
</div>
<details class="noted"><summary>口径说明</summary><div class="muted" style="margin-top:6px">本卡曲线按账号代理流量统计，与「各账号用量」一致（自新节点上线起算；上线初期未记录时段的累计已按时段均摊）；顶部整机流量是网卡口径（含转发两端），数值更大，属正常。</div></details></div>
</section>

<section class="view" data-view="services">
<div class="grid2">
<div class="card acc open z4">{head('机器性能')}{rfbtn('perf', True)}
<div class="cardbody"><div class="cbinner"><div id="mod-perf">{frag_perf(snap)}</div></div></div></div>
<div class="card acc z3">{head('服务与证书')}{rfbtn('services', True)}
<div class="cardbody"><div class="cbinner"><div id="mod-services">{frag_services(snap)}</div></div></div></div>
<div class="card acc z6">{head('异常记录')}{rfbtn('events', True)}
<div class="cardbody"><div class="cbinner"><div id="mod-events">{frag_events(snap)}</div></div></div></div>
<div class="card acc z8">{head('住宅出口 · Webshare')}{rfbtn('webshare', True)}
<div class="cardbody"><div class="cbinner"><div id="mod-webshare">{frag_webshare(snap)}</div></div></div></div>
</div>
</section>

<section class="view" data-view="links">
<div class="card grow z7">{head('订阅链接（新域名）')}</div>{link_rows}
<div class="muted" style="margin-top:8px">链接已隐藏显示防外泄，点「复制链接」后直接粘贴到客户端导入；链接即钥匙，请勿外传。</div></div>
</section>

<div class="foot">Fleet Panel</div>
</div>
<nav class="tabbar">
<button class="tabbtn on" data-t="overview">{icon("home")}概览</button>
<button class="tabbtn" data-t="users">{icon("users")}账号</button>
<button class="tabbtn" data-t="charts">{icon("chart")}曲线</button>
<button class="tabbtn" data-t="services">{icon("server")}服务</button>
<button class="tabbtn" data-t="links">{icon("link")}链接</button>
</nav>
<script id="chartdata" type="application/json">{data_json}</script>
<script id="linksdata" type="application/json">{links_json}</script>
<script>
var DATA = JSON.parse(document.getElementById('chartdata').textContent);
var cur = '24h';
function fmtB(n) {{ if (n >= 1e9) return (n/1e9).toFixed(2)+' GB'; if (n >= 1e6) return (n/1e6).toFixed(1)+' MB'; if (n >= 1e3) return (n/1e3).toFixed(1)+' KB'; return n+' B'; }}
function draw(elId, detId, seriesMap, labels, single) {{
  var el0 = document.getElementById(elId);
  var W = el0.clientWidth;
  if (!W) return;
  var H = W < 480 ? 175 : 205;
  var pl = 6, pr = 6, pt = 20, pb = 24, iw = W-pl-pr, ih = H-pt-pb;
  var names = Object.keys(seriesMap), maxv = 1;
  names.forEach(function(n) {{ seriesMap[n].forEach(function(v) {{ if (v > maxv) maxv = v; }}); }});
  var n = labels.length, s = '<svg viewBox="0 0 '+W+' '+H+'" width="'+W+'" height="'+H+'">';
  [0.25, 0.5, 0.75, 1].forEach(function(f) {{ var y = pt + ih - ih*f; s += '<line x1="'+pl+'" y1="'+y+'" x2="'+(W-pr)+'" y2="'+y+'" stroke="rgba(148,163,184,.10)"/>'; }});
  s += '<line x1="'+pl+'" y1="'+(pt+ih)+'" x2="'+(W-pr)+'" y2="'+(pt+ih)+'" stroke="rgba(148,163,184,.22)"/>';
  s += '<text x="'+pl+'" y="'+(pt-7)+'" font-size="11" fill="#8d9db2">峰值 '+fmtB(maxv)+'</text>';
  s += '<text x="'+pl+'" y="'+(pt + ih*0.5 - 4)+'" font-size="11" fill="#5f7288">'+fmtB(maxv/2)+'</text>';
  names.forEach(function(name) {{
    var vals = seriesMap[name], color = single ? '#5ea0fa' : (DATA[cur].colors[name] || '#64748b');
    var pts = vals.map(function(v, j) {{ return (pl + iw*(n>1 ? j/(n-1) : 0)).toFixed(1)+','+(pt + ih - v/maxv*ih).toFixed(1); }}).join(' ');
    s += '<polyline class="ln" pathLength="1" fill="none" stroke="'+color+'" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round" points="'+pts+'"/>';
    vals.forEach(function(v, j) {{
      var x = pl + iw*(n>1 ? j/(n-1) : 0), y = pt + ih - v/maxv*ih;
      s += '<circle class="pt" data-j="'+j+'" cx="'+x.toFixed(1)+'" cy="'+y.toFixed(1)+'" r="11" fill="transparent"/>';
      s += '<circle class="dt" cx="'+x.toFixed(1)+'" cy="'+y.toFixed(1)+'" r="3" fill="'+color+'" pointer-events="none"/>';
    }});
  }});
  var spacing = labels.some(function(label) {{ return label.length > 5; }}) ? 95 : 58;
  var ticks = Math.min(n, Math.max(2, Math.floor(W / spacing) + 1));
  for (var i = 0; i < ticks; i++) {{
    var j = ticks > 1 ? Math.round(i * (n-1) / (ticks-1)) : 0;
    var x = pl + iw*(n>1 ? j/(n-1) : 0), anchor = i === 0 ? 'start' : i === ticks-1 ? 'end' : 'middle';
    s += '<text x="'+x.toFixed(0)+'" y="'+(H-6)+'" font-size="11" fill="#7e90a9" text-anchor="'+anchor+'">'+labels[j]+'</text>';
  }}
  s += '</svg>';
  var el = el0; el.innerHTML = s;
  el.querySelectorAll('circle.pt').forEach(function(c) {{
    c.addEventListener('click', function() {{
      var j = +c.getAttribute('data-j'), parts = [];
      names.forEach(function(name) {{ parts.push(name+'：<b>'+fmtB(seriesMap[name][j])+'</b>'); }});
      document.getElementById(detId).innerHTML = '<b>'+labels[j]+'</b> &nbsp; '+parts.join(' &nbsp;·&nbsp; ');
    }});
  }});
}}
function renderCharts() {{
  var d = DATA[cur];
  document.getElementById('ulegend').innerHTML = Object.keys(d.users).map(function(u) {{
    var tot = d.users[u].reduce(function(a, b) {{ return a+b; }}, 0);
    return '<span style="color:'+(d.colors[u] || '#64748b')+'">●</span> '+u+'（'+fmtB(tot)+'）';
  }}).join(' &nbsp; ');
  draw('uchart', 'udet', d.users, d.labels, false);
  draw('mchart', 'mdet', {{'四账号合计': d.total}}, d.labels, true);
  document.getElementById('udet').textContent = '各账号流量 · 点图上的点查看';
  document.getElementById('mdet').textContent = '四账号合计 · 点图上的点查看';
}}
document.querySelectorAll('.rbtn').forEach(function(b) {{
  b.addEventListener('click', function() {{
    document.querySelectorAll('.rbtn').forEach(function(x) {{ x.classList.remove('on'); }});
    b.classList.add('on'); cur = b.getAttribute('data-range'); renderCharts();
  }});
}});
function clock() {{ var d = new Date(); return d.toLocaleTimeString('zh-CN', {{hour12: false}}); }}
function fmtBytes(n) {{ var u = ['B','KB','MB','GB','TB'], i = 0; n = +n; while (Math.abs(n) >= 1024 && i < 4) {{ n /= 1024; i++; }} return (u[i] === 'GB' || u[i] === 'TB') ? n.toFixed(2)+' '+u[i] : Math.round(n)+' '+u[i]; }}
var REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
function animateNum(el) {{
  var to = parseFloat(el.getAttribute('data-v')) || 0, from = parseFloat(el.getAttribute('data-cur') || '0');
  if (REDUCED || to === from) {{ el.textContent = fmtBytes(to); el.setAttribute('data-cur', to); return; }}
  var t0 = null, dur = 850;
  function step(ts) {{ if (!t0) t0 = ts; var p = Math.min(1, (ts - t0)/dur), e = 1 - Math.pow(1 - p, 3); el.textContent = fmtBytes(from + (to - from)*e); if (p < 1) {{ requestAnimationFrame(step); }} else {{ el.setAttribute('data-cur', to); }} }}
  requestAnimationFrame(step);
}}
function animateWithin(root) {{
  root.querySelectorAll('.cnt').forEach(animateNum);
  root.querySelectorAll('[data-w]').forEach(function(el) {{ requestAnimationFrame(function() {{ requestAnimationFrame(function() {{ el.style.width = el.getAttribute('data-w') + '%'; }}); }}); }});
}}
function bindGoto(root) {{
  root.querySelectorAll('[data-goto]').forEach(function(b) {{
    if (b.getAttribute('data-bound')) return;
    b.setAttribute('data-bound', '1');
    b.addEventListener('click', function() {{ applyView(b.getAttribute('data-goto')); }});
  }});
}}
bindGoto(document);
function bindRefresh(root) {{
  root.querySelectorAll('.rfbtn').forEach(function(b) {{
    if (b.getAttribute('data-bound-rf')) return;
    b.setAttribute('data-bound-rf', '1');
    b.addEventListener('click', function() {{
    if (b.getAttribute('data-goto')) return;
    var m = b.getAttribute('data-m');
    b.disabled = true; b.classList.add('loading');
    var done = function() {{ b.disabled = false; b.classList.remove('loading'); var u = document.getElementById('upd-'+m); if (u) u.textContent = '已更新 '+clock(); }};
    if (m === 'charts') {{
      fetch('/api/charts').then(function(r) {{ if (r.status === 401) {{ location.reload(); return null; }} return r.json(); }}).then(function(j) {{ if (j) {{ DATA = j; renderCharts(); }} done(); }}).catch(function() {{ done(); }});
    }} else {{
      fetch('/api/frag?m='+m).then(function(r) {{ if (r.status === 401) {{ location.reload(); return null; }} return r.text(); }}).then(function(t) {{ var el = document.getElementById('mod-'+m); if (t !== null && el) {{ el.innerHTML = t; animateWithin(el); bindGoto(el); bindRefresh(el); initEvPager(el); }} done(); }}).catch(function() {{ done(); }});
    }}
    }});
  }});
}}
bindRefresh(document);
function applyView(name) {{
  document.querySelectorAll('.view').forEach(function(v) {{ v.classList.toggle('von', v.getAttribute('data-view') === name); v.style.transform = ''; }});
  document.querySelectorAll('.tabbtn').forEach(function(b) {{ b.classList.toggle('on', b.getAttribute('data-t') === name); }});
  window.scrollTo(0, 0);
  if (name === 'charts') {{ renderCharts(); }}
}}
document.querySelectorAll('.tabbtn').forEach(function(b) {{ b.addEventListener('click', function() {{ applyView(b.getAttribute('data-t')); }}); }});
document.querySelectorAll('.segbtn').forEach(function(b) {{ b.addEventListener('click', function() {{ document.querySelectorAll('.segbtn').forEach(function(x) {{ x.classList.remove('on'); }}); b.classList.add('on'); document.getElementById('chartscard').setAttribute('data-cv', b.getAttribute('data-cv')); renderCharts(); }}); }});
document.querySelectorAll('.acc .cardhead').forEach(function(h) {{
  h.addEventListener('click', function(e) {{
    if (e.target.closest('.rfbtn') || !window.matchMedia('(max-width: 860px)').matches) return;
    var card = h.parentElement, opening = !card.classList.contains('open');
    document.querySelectorAll('.acc').forEach(function(c) {{
      var open = c === card && opening;
      c.classList.toggle('open', open);
      c.querySelector('.chev').setAttribute('aria-expanded', String(open));
    }});
    if (opening) h.scrollIntoView({{block: 'nearest'}});
  }});
}});
renderCharts();
var rszT; window.addEventListener('resize', function() {{ clearTimeout(rszT); rszT = setTimeout(renderCharts, 200); }});
window.matchMedia('(max-width: 860px)').addEventListener('change', function() {{ renderCharts(); }});
animateWithin(document);
var LINKS = JSON.parse(document.getElementById('linksdata').textContent);
document.querySelectorAll('.cpbtn').forEach(function(b) {{
  b.addEventListener('click', function() {{
    var url = LINKS[+b.getAttribute('data-i')].url;
    var orig = b.innerHTML;
    function ok() {{ b.innerHTML = '✓ 已复制'; b.classList.add('done'); setTimeout(function() {{ b.innerHTML = orig; b.classList.remove('done'); }}, 1600); }}
    function fallback() {{ var ta = document.createElement('textarea'); ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0'; document.body.appendChild(ta); ta.select(); try {{ document.execCommand('copy'); ok(); }} catch (e) {{ b.innerHTML = '复制失败'; setTimeout(function() {{ b.innerHTML = orig; }}, 1600); }} document.body.removeChild(ta); }}
    if (navigator.clipboard && navigator.clipboard.writeText) {{ navigator.clipboard.writeText(url).then(ok).catch(fallback); }} else {{ fallback(); }}
  }});
}});
function initEvPager(root) {{
  var host = null;
  if (root.id === 'mod-events') host = root;
  else if (root.querySelector) host = root.querySelector('#mod-events');
  if (!host) return;
  var list = host.querySelector('.evlist');
  if (!list) return;
  var rows = Array.prototype.slice.call(list.querySelectorAll('.evrow'));
  var pager = host.querySelector('.evpager');
  var size = 6;
  var pages = Math.max(1, Math.ceil(rows.length / size));
  var curp = 0;
  function show() {{
    rows.forEach(function(r, i) {{ r.style.display = Math.floor(i / size) === curp ? '' : 'none'; }});
    if (pager) {{
      pager.style.display = pages > 1 ? '' : 'none';
      var lab = pager.querySelector('.evpage');
      if (lab) lab.textContent = (curp + 1) + ' / ' + pages;
    }}
  }}
  if (pager) {{
    pager.querySelectorAll('.evbtn').forEach(function(b) {{
      if (b.getAttribute('data-bound-ev')) return;
      b.setAttribute('data-bound-ev', '1');
      b.addEventListener('click', function() {{ curp = Math.min(pages - 1, Math.max(0, curp + (+b.getAttribute('data-ev')))); show(); }});
    }});
  }}
  show();
}}
var AUTO_MODS = ['traffic', 'users', 'perf', 'services', 'events'];
var autoIdx = 0;
function softSwap(m, t) {{
  var el = document.getElementById('mod-' + m);
  if (!el || t === null) return;
  el.innerHTML = t; animateWithin(el); bindGoto(el); bindRefresh(el); initEvPager(el);
  var u = document.getElementById('upd-' + m); if (u) u.textContent = '自动更新 ' + clock();
}}
function softCharts() {{
  var cv = document.querySelector('.view[data-view="charts"]');
  if (!(window.innerWidth > 860 || (cv && cv.classList.contains('von')))) return;
  fetch('/api/charts?soft=1').then(function(r) {{ return r.status === 401 ? null : r.json(); }}).then(function(j) {{ if (j) {{ DATA = j; renderCharts(); }} }}).catch(function() {{}});
}}
setInterval(function() {{
  if (document.hidden) return;
  var m = AUTO_MODS[autoIdx % AUTO_MODS.length]; autoIdx++;
  fetch('/api/frag?soft=1&m=' + m).then(function(r) {{ if (r.status === 401) {{ location.reload(); return null; }} return r.text(); }}).then(function(t) {{ softSwap(m, t); }}).catch(function() {{}});
  if (autoIdx % AUTO_MODS.length === 0) softCharts();
}}, 20000);
initEvPager(document);
</script>
</body></html>'''


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'FleetPanel'
    sys_version = ''

    def log_message(self, *_args):
        pass

    def _send(self, code, body, ctype='text/html; charset=utf-8', extra=None):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store, private')
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _session(self):
        cookie = self.headers.get('Cookie') or ''
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith('fleet_sid='):
                sid = part[len('fleet_sid='):]
                ent = sessions.get(sid)
                if (isinstance(ent, list) and len(ent) == 2
                        and ent[0] > time.time()
                        and ent[1] is not None and ent[1] == _pw_ver()):
                    return sid
                with _sess_lock:
                    sessions.pop(sid, None)
                    _save_sessions()
        return None

    def do_GET(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == '/logout':
            sid = self._session()
            if sid:
                with _sess_lock:
                    sessions.pop(sid, None)
                    _save_sessions()
            self._send(302, '', extra={'Location': '/',
                                       'Set-Cookie': 'fleet_sid=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0'})
            return
        if path in ('/api/frag', '/api/charts'):
            if not self._session():
                self._send(401, '{"error":"unauthorized"}', ctype='application/json')
                return
            if path == '/api/frag' and (parse_qs(parsed.query).get('m') or [''])[0] == 'webshare':
                try:
                    (BASE / '.ws-force').touch()
                except Exception:
                    pass
            if (parse_qs(parsed.query).get('soft') or [''])[0] != '1':
                request_fresh()
            try:
                if path == '/api/charts':
                    data = {rk: bucketize(rk) for rk in ('24h', '7d', '30d')}
                    self._send(200, json.dumps(data, ensure_ascii=False), ctype='application/json')
                else:
                    m = (parse_qs(parsed.query).get('m') or [''])[0]
                    fn = FRAGS.get(m)
                    if fn is None:
                        self.send_error(404)
                        return
                    self._send(200, fn(load_snap()))
            except Exception:
                self._send(503, '数据生成中，请稍后重试')
            return
        if self._session():
            try:
                self._send(200, render_panel())
            except Exception:
                self._send(503, '数据生成中，请稍后刷新')
        else:
            self._send(200, login_page())

    def do_POST(self):
        path = urlsplit(self.path).path
        if path != '/login':
            self.send_error(404)
            return
        ip = self.client_address[0]
        now = time.time()
        cnt, blocked_until = fails.get(ip, [0, 0])
        if blocked_until > now:
            self._send(429, login_page('尝试次数过多，请 10 分钟后再试'))
            return
        try:
            length = min(int(self.headers.get('Content-Length') or 0), 4096)
            form = parse_qs(self.rfile.read(length).decode('utf-8', 'replace'))
            pw = (form.get('password') or [''])[0]
        except Exception:
            pw = ''
        if pw and check_password(pw):
            fails.pop(ip, None)
            sid = secrets.token_urlsafe(32)
            with _sess_lock:
                sessions[sid] = [now + SESSION_TTL, _pw_ver()]
                _save_sessions()
            self._send(302, '', extra={'Location': '/',
                                       'Set-Cookie': f'fleet_sid={sid}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={SESSION_TTL}'})
        else:
            time.sleep(0.4)
            cnt += 1
            fails[ip] = [cnt, now + 600 if cnt >= 5 else 0]
            self._send(200, login_page('密码不对，再试一次'))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    slots = threading.BoundedSemaphore(32)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
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
        except (OSError, ssl.SSLError, socket.timeout):
            pass
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(tls_socket or request)
            self.slots.release()


context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain(CFG['cert'], CFG['key'])
server = Server((CFG.get('bind', '0.0.0.0'), int(CFG['port'])), Handler)
server.serve_forever()
