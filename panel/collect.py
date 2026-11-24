#!/usr/bin/env python3
"""Fleet panel collector (root, every 2 min via systemd timer).

Reads live state (HY2 stats API, /proc, subscription traffic accounting,
systemd, cert files) and stores:
  - /var/lib/fleet-panel/status.json    latest snapshot for the panel page
  - /var/lib/fleet-panel/history.sqlite3  cumulative samples (90 days) for charts
Read-only with respect to the services it observes.
"""
import calendar
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
from pathlib import Path

BASE = Path('/var/lib/fleet-panel')
DB = BASE / 'history.sqlite3'
SNAP = BASE / 'status.json'
STATE = BASE / 'collector_state.json'
HY2_CONFS = [Path('/opt/fleet-hy2/server.yaml'),
             Path('/opt/fleet-hy2-webshare/server.yaml')]
SUB_STATE = Path('/var/lib/fleet-subscriptions/traffic_state.json')
SUB_CFG = Path('/var/lib/fleet-subscriptions/userinfo.json')
CERTS = {
    'node.example.com 节点': '/opt/fleet-hy2/public-tls.crt',
    'panel.example.com': '/opt/fleet-panel/public-tls.crt',
}
SERVICES = ['fleet-hy2', 'fleet-hy2-webshare', 'xray', 'fleet-subscription', 'fleet-panel',
            'fleet-sub-healthcheck.timer', 'fleet-traffic-account.timer']
KEEP_SECS = 90 * 86400
EV_STATE = BASE / 'events_state.json'
TZ8 = 8 * 3600
SVC_LABELS = {
    'fleet-hy2': '节点服务（HY2）',
    'fleet-hy2-webshare': '节点服务（HY2·Webshare）',
    'xray': '节点服务（VLESS）',
    'reality': '节点服务（Reality）',
    'reality-ws': '节点服务（Reality·Webshare）',
    'fleet-subscription': '订阅服务',
    'fleet-panel': '本面板',
    'fleet-panel-collect.timer': '数据采集',
    'fleet-sub-healthcheck.timer': '自愈看门狗',
    'fleet-traffic-account.timer': '流量记账',
}
PORT_PROBES = {'reality': 443, 'reality-ws': 443}
RESTART_TRACK = ['fleet-hy2', 'fleet-hy2-webshare', 'xray', 'fleet-subscription', 'fleet-panel']
CERT_WARN_LEVELS = [14, 7, 3]
WS_KEY_FILE = Path('/etc/fleet-panel/webshare.key')

WS_PLACES = {'203.0.113.10': 'Example City · Example ISP'}  # 203.0.113.0/24 is TEST-NET-3, reserved for documentation

# Per-machine overrides from /etc/fleet-panel/collector.json (cert labels,
# watched services). Absent on a single-machine install, whose defaults
# are the constants above.
try:
    _ovr = json.loads(Path('/etc/fleet-panel/collector.json').read_text())
    if isinstance(_ovr.get('certs'), dict):
        CERTS = _ovr['certs']
    if isinstance(_ovr.get('services'), list):
        SERVICES = _ovr['services']
except Exception:
    pass


def _ws_api(path, key):
    req = urllib.request.Request('https://proxy.webshare.io' + path,
                                 headers={'Authorization': key})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def fetch_webshare():
    """Live Webshare usage via the API key stored on this server
    (/etc/fleet-panel/webshare.key). Returns the module dict or None."""
    from datetime import datetime, timezone
    try:
        key = WS_KEY_FILE.read_text().strip()
        if not key:
            return None
        sub = _ws_api('/api/v2/subscription/', key)
        plans = _ws_api('/api/v2/subscription/plan/', key)
        plan = next(p for p in plans['results'] if p['id'] == sub['plan'])
        stats = _ws_api('/api/v2/stats/', key)
        nowd = datetime.now(timezone.utc)
        start = datetime.fromisoformat(sub['start_date'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(sub['end_date'].replace('Z', '+00:00'))
        used = sum(b['bandwidth_total'] for b in stats
                   if not b.get('is_projected')
                   and datetime.fromisoformat(b['timestamp'].replace('Z', '+00:00')) <= nowd)
        quota_gb = float(plan['bandwidth_limit'])
        used_gb = used / 1e9
        hours_elapsed = max((nowd - start).total_seconds() / 3600, 0.01)
        hours_total = (end - start).total_seconds() / 3600
        projected_gb = used_gb / hours_elapsed * hours_total
        ip, port = '', 0
        try:
            text = HY2_CONFS[1].read_text()
            m = re.search(r'@(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})\b', text)
            if m:
                ip, port = m.group(1), int(m.group(2))
        except Exception:
            pass
        return {
            'used_gb': round(used_gb, 3), 'quota_gb': quota_gb,
            'pct': round(used_gb / quota_gb * 100, 2) if quota_gb else 0,
            'projected_gb': round(projected_gb, 1) if hours_elapsed >= 24 else None,
            'projected_pct': (round(projected_gb / quota_gb * 100, 1) if quota_gb else 0) if hours_elapsed >= 24 else None,
            'cycle_start': sub['start_date'], 'cycle_end': sub['end_date'],
            'days_left': round((end - nowd).total_seconds() / 86400, 1),
            'will_renew': sub['will_renew'], 'monthly_price': plan['monthly_price'],
            'replacements_used': plan['proxy_replacements_used'],
            'replacements_total': plan['proxy_replacements_total'],
            'ip': ip, 'port': port, 'place': WS_PLACES.get(ip, ''),
            'updated_ts': int(time.time()), 'live': True,
        }
    except Exception:
        return None


def load_webshare(now):
    cache = None
    try:
        cache = json.loads((BASE / 'webshare.json').read_text())
    except Exception:
        cache = None
    force_f = BASE / '.ws-force'
    forced = force_f.exists()
    stale = cache is None or now - int(cache.get('updated_ts', 0)) > 600
    if WS_KEY_FILE.exists() and (forced or stale):
        fresh = fetch_webshare()
        if fresh:
            try:
                (BASE / 'webshare.json').write_text(json.dumps(fresh, ensure_ascii=False))
            except Exception:
                pass
            cache = fresh
    if forced:
        try:
            force_f.unlink()
        except Exception:
            pass
    return cache



def _hy2_one(conf_path):
    text = conf_path.read_text()
    m = re.search(r'trafficStats:\s*\n\s*listen:\s*127\.0\.0\.1:(\d+)\s*\n\s*secret:\s*(\S+)', text)
    port, secret = m.group(1), m.group(2)
    if len(secret) >= 2 and secret[0] == secret[-1] and secret[0] in '"\'':
        secret = secret[1:-1]

    def get(path):
        req = urllib.request.Request(f'http://127.0.0.1:{port}{path}',
                                     headers={'Authorization': secret})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    traffic = get('/traffic')
    try:
        online_raw = get('/online')
        online = sorted(online_raw.keys()) if isinstance(online_raw, dict) else sorted(map(str, online_raw))
    except Exception:
        online = []
    users = {u: {'tx': int(v.get('tx', 0)), 'rx': int(v.get('rx', 0))} for u, v in traffic.items()}
    return users, online


def hy2_stats():
    users, online, ok = {}, set(), False
    for conf in HY2_CONFS:
        try:
            u, o = _hy2_one(conf)
        except Exception:
            continue
        ok = True
        for name, v in u.items():
            cur = users.setdefault(name, {'tx': 0, 'rx': 0})
            cur['tx'] += v['tx']
            cur['rx'] += v['rx']
        online.update(o)
    if not ok:
        raise RuntimeError('no HY2 stats endpoint responded')
    return users, sorted(online)


USER_STATE = BASE / 'user_traffic_state.json'
XRAY_EMAIL_MAP = {
    'mac': 'alice_mac', 'mac-ws': 'alice_mac',
    'desktop': 'alice_desktop', 'desktop-ws': 'alice_desktop',
    'iphone': 'alice_iphone', 'iphone-ws': 'alice_iphone',
    'bob': 'bob', 'bob-ws': 'bob',
}


def xray_stats():
    """Per-user counters from the Xray StatsService (127.0.0.1:10085),
    keyed by panel user name. tx = downlink (to device), rx = uplink,
    matching the HY2 stats field meanings."""
    out = subprocess.run(['/usr/local/bin/xray', 'api', 'statsquery',
                          '--server=127.0.0.1:10085'],
                         capture_output=True, text=True, timeout=10)
    data = json.loads(out.stdout)
    users = {}
    for entry in data.get('stat', []):
        parts = entry.get('name', '').split('>>>')
        if len(parts) != 4 or parts[0] != 'user' or parts[2] != 'traffic':
            continue
        user = XRAY_EMAIL_MAP.get(parts[1])
        if not user:
            continue
        cur = users.setdefault(user, {'tx': 0, 'rx': 0})
        val = int(entry.get('value', 0))
        if parts[3] == 'downlink':
            cur['tx'] += val
        elif parts[3] == 'uplink':
            cur['rx'] += val
    return users


def accumulate_users(source, raw):
    """Fold one source's raw (since-process-start) counters into persistent
    per-user totals, tolerating counter resets when the daemon restarts."""
    try:
        st = json.loads(USER_STATE.read_text())
    except Exception:
        st = {}
    acc = st.get(source + '_acc', {})
    if not raw:
        return acc
    prev = st.get(source + '_raw', {})
    for user, v in raw.items():
        a = acc.setdefault(user, {'tx': 0, 'rx': 0})
        p = prev.get(user, {'tx': 0, 'rx': 0})
        for k in ('tx', 'rx'):
            cur_v, prev_v = int(v.get(k, 0)), int(p.get(k, 0))
            a[k] += cur_v - prev_v if cur_v >= prev_v else cur_v
    st[source + '_raw'] = raw
    st[source + '_acc'] = acc
    try:
        USER_STATE.write_text(json.dumps(st))
    except Exception:
        pass
    return acc


def machine_traffic():
    cfg = json.loads(SUB_CFG.read_text())
    st = json.loads(SUB_STATE.read_text())
    rx, tx = int(st['acc_rx']), int(st['acc_tx'])
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if boot == st.get('boot_id'):
            for line in Path('/proc/net/dev').read_text().splitlines():
                if line.strip().startswith(str(st.get('iface', 'ens17')) + ':'):
                    p = line.split(':', 1)[1].split()
                    rx += max(0, int(p[0]) - int(st['last_rx']))
                    tx += max(0, int(p[8]) - int(st['last_tx']))
    except Exception:
        pass
    return rx, tx, int(cfg['total']), int(cfg['expire'])


def perf():
    load = [float(x) for x in Path('/proc/loadavg').read_text().split()[:3]]
    mem = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        k, v = line.split(':', 1)
        mem[k] = int(v.split()[0]) * 1024
    nums = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    idle, total = nums[3] + nums[4], sum(nums)
    cpu_pct = None
    prev = {}
    if STATE.exists():
        try:
            prev = json.loads(STATE.read_text())
        except Exception:
            prev = {}
    if prev.get('cpu_total'):
        dt, di = total - prev['cpu_total'], idle - prev['cpu_idle']
        if dt > 0:
            cpu_pct = round(100.0 * (dt - di) / dt, 1)
    STATE.write_text(json.dumps({'cpu_idle': idle, 'cpu_total': total}))
    disk = shutil.disk_usage('/')
    uptime = int(float(Path('/proc/uptime').read_text().split()[0]))
    return {'load': load, 'cpu_pct': cpu_pct,
            'mem_total': mem.get('MemTotal', 0), 'mem_avail': mem.get('MemAvailable', 0),
            'disk_total': disk.total, 'disk_used': disk.used, 'uptime': uptime}


def cert_expiry(path):
    try:
        out = subprocess.run(['openssl', 'x509', '-in', path, '-noout', '-enddate'],
                             capture_output=True, text=True, timeout=10).stdout
        s = out.strip().split('=', 1)[1].replace('  ', ' ')
        return calendar.timegm(time.strptime(s, '%b %d %H:%M:%S %Y %Z'))
    except Exception:
        return None


def service_states():
    out = {}
    for name in SERVICES:
        r = subprocess.run(['systemctl', 'is-active', name],
                           capture_output=True, text=True, timeout=10)
        out[name] = (r.stdout.strip() == 'active')
    return out


def port_states():
    out = {}
    for name, port in PORT_PROBES.items():
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=2):
                out[name] = True
        except Exception:
            out[name] = False
    return out


def _ev_state():
    try:
        return json.loads(EV_STATE.read_text())
    except Exception:
        return {}


def _active_enter(name):
    try:
        r = subprocess.run(['systemctl', 'show', name, '-p', 'ActiveEnterTimestamp', '--value'],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ''


def _journal_health(since_ts):
    """fleet-sub-health journal lines since since_ts -> (fail_count, [restart_ts])."""
    fails, restarts = 0, []
    try:
        since = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(since_ts))
        r = subprocess.run(['journalctl', '-t', 'fleet-sub-health', '--since', since,
                            '--no-pager', '-o', 'short-iso'],
                           capture_output=True, text=True, timeout=15)
        for line in r.stdout.splitlines():
            m = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})', line)
            ts = calendar.timegm(time.strptime(m.group(1), '%Y-%m-%dT%H:%M:%S')) if m else None
            if 'check failed' in line:
                if ts is None or ts > since_ts:
                    fails += 1
            elif 'restarting fleet-subscription' in line:
                restarts.append(ts)
    except Exception:
        pass
    return fails, restarts


def main():
    now = int(time.time())
    ev = _ev_state()
    first_run = not ev
    events = []  # (ts, kind, text) anomaly-log entries detected this run

    hy2_ok = True
    try:
        hy2_raw, online = hy2_stats()
    except Exception:
        hy2_raw, online, hy2_ok = {}, [], False
    try:
        xray_raw = xray_stats()
    except Exception:
        xray_raw = {}
    users = {}
    for src_acc in (accumulate_users('hy2', hy2_raw),
                    accumulate_users('xray', xray_raw)):
        for name, v in src_acc.items():
            cur = users.setdefault(name, {'tx': 0, 'rx': 0})
            cur['tx'] += v['tx']
            cur['rx'] += v['rx']
    if not first_run:
        if ev.get('hy2_ok', True) and not hy2_ok:
            events.append((now, 'down', '节点服务统计接口无响应'))
        elif not ev.get('hy2_ok', True) and hy2_ok:
            events.append((now, 'up', '节点服务统计接口已恢复'))
    ev['hy2_ok'] = hy2_ok

    p = perf()
    m_rx, m_tx, m_total, m_expire = machine_traffic()
    services = service_states()
    services.update(port_states())
    certs = {name: cert_expiry(path) for name, path in CERTS.items()}

    boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    booted = (not first_run) and bool(ev.get('boot_id')) and ev['boot_id'] != boot_id
    if booted:
        events.append((now, 'boot', '整机已重启'))
    ev['boot_id'] = boot_id

    # Self-heal journal: failure streaks and automatic subscription restarts.
    heal_ts = []
    if not first_run and not booted:
        fails, heal_ts = _journal_health(int(ev.get('last_run', now)))
        seen = set(ev.get('hc_restarts_seen', []))
        for ts in heal_ts:
            if ts and ts not in seen:
                events.append((ts, 'heal', '订阅服务连续探测失败，自愈已自动重启'))
                seen.add(ts)
        ev['hc_restarts_seen'] = sorted(seen)[-10:]
        pending = int(ev.get('hc_pending_fails', 0)) + fails
        if heal_ts:
            pending = 0
        elif pending and fails == 0:
            events.append((now, 'warn', f'订阅服务探测曾连续失败 {pending} 次，其后恢复正常'))
            pending = 0
        ev['hc_pending_fails'] = pending

    # Service stop/start transitions and restarts (suppressed on boot runs:
    # the boot event already explains them).
    if not first_run and not booted:
        prev_svc = ev.get('services', {})
        for name in list(SERVICES) + list(PORT_PROBES):
            if name in prev_svc and prev_svc[name] != services[name]:
                lab = SVC_LABELS.get(name, name)
                if services[name]:
                    events.append((now, 'up', f'{lab} 已恢复运行'))
                else:
                    events.append((now, 'down', f'{lab} 已停止'))
        prev_ae = ev.get('active_enter', {})
        new_ae = {}
        for name in RESTART_TRACK:
            cur_ae = _active_enter(name)
            new_ae[name] = cur_ae
            if (prev_ae.get(name) and cur_ae and cur_ae != prev_ae[name]
                    and services.get(name)
                    and not (name == 'fleet-subscription' and heal_ts)):
                events.append((now, 'restart', f'{SVC_LABELS.get(name, name)} 已重启'))
        ev['active_enter'] = new_ae
    else:
        ev['active_enter'] = {name: _active_enter(name) for name in RESTART_TRACK}
    ev['services'] = services

    # Certs: renewal success (expiry moved out) + near-expiry warnings.
    prev_certs = ev.get('certs', {})
    warned = ev.get('cert_warned', {})
    for name, exp in certs.items():
        if exp is None:
            continue
        prev_exp = prev_certs.get(name)
        if prev_exp and exp > prev_exp:
            new_date = time.strftime('%m-%d', time.gmtime(exp + TZ8))
            events.append((now, 'cert', f'证书续期成功：{name}（新到期 {new_date}）'))
            warned[name] = 99
        else:
            days = (exp - now) // 86400
            for lv in CERT_WARN_LEVELS:
                if days <= lv and int(warned.get(name, 99)) > lv:
                    events.append((now, 'warn', f'证书临近到期：{name}（剩 {days} 天）'))
                    warned[name] = lv
                    break
    ev['certs'] = certs
    ev['cert_warned'] = warned
    # Resource spikes: CPU / memory / disk suddenly running hot. Entry
    # and recovery are both logged, with hysteresis so a value hovering
    # at the threshold does not spam the log. On the first run and on
    # boot runs only the baseline state is recorded.
    spike = ev.get('spike', {})

    def _spike(key, pct, enter_at, exit_below, label, jump=False):
        if pct is None:
            return
        if not spike.get(key) and (pct >= enter_at or jump):
            if not first_run and not booted:
                events.append((now, 'warn', f'{label}占用突升：{pct:.0f}%'))
            spike[key] = True
        elif spike.get(key) and pct < exit_below:
            if not first_run and not booted:
                events.append((now, 'up', f'{label}占用已恢复：{pct:.0f}%'))
            spike[key] = False

    mem_pct = (100.0 * (p['mem_total'] - p['mem_avail']) / p['mem_total']) if p['mem_total'] else None
    disk_pct = (100.0 * p['disk_used'] / p['disk_total']) if p['disk_total'] else None
    cpu_jump = (p.get('cpu_pct') is not None and ev.get('prev_cpu') is not None
                and p['cpu_pct'] - ev['prev_cpu'] >= 40 and p['cpu_pct'] >= 80)
    _spike('cpu', p.get('cpu_pct'), 90, 85, 'CPU', jump=cpu_jump)
    _spike('mem', mem_pct, 90, 85, '内存')
    _spike('disk', disk_pct, 90, 88, '磁盘')
    ev['spike'] = spike
    if p.get('cpu_pct') is not None:
        ev['prev_cpu'] = p['cpu_pct']
    ev['last_run'] = now
    try:
        EV_STATE.write_text(json.dumps(ev))
    except Exception:
        pass

    webshare = load_webshare(now)

    snap = {
        'ts': now,
        'perf': p,
        'machine': {'rx': m_rx, 'tx': m_tx, 'total': m_total, 'expire': m_expire},
        'users': users,
        'online': online,
        'services': services,
        'certs': certs,
        'webshare': webshare,
    }

    db = sqlite3.connect(DB)
    db.execute('CREATE TABLE IF NOT EXISTS samples (ts INTEGER PRIMARY KEY, m_rx INTEGER, m_tx INTEGER, users TEXT)')
    db.execute('INSERT OR REPLACE INTO samples VALUES (?, ?, ?, ?)',
               (now, m_rx, m_tx, json.dumps({u: [v['tx'], v['rx']] for u, v in users.items()})))
    db.execute('DELETE FROM samples WHERE ts < ?', (now - KEEP_SECS,))
    db.execute('CREATE TABLE IF NOT EXISTS events (ts INTEGER, kind TEXT, text TEXT)')
    for ts, kind, text in sorted(events):
        db.execute('INSERT INTO events VALUES (?, ?, ?)', (ts, kind, text))
    db.execute('DELETE FROM events WHERE ts < ?', (now - KEEP_SECS,))
    rows = db.execute('SELECT ts, kind, text FROM events ORDER BY ts DESC, rowid DESC LIMIT 60').fetchall()
    snap['events'] = [{'ts': t, 'kind': k, 'text': x} for t, k, x in rows]
    db.commit()
    db.close()

    tmp = SNAP.with_suffix('.tmp')
    tmp.write_text(json.dumps(snap))
    tmp.replace(SNAP)

    # Consistent history copy for an aggregating panel on another
    # machine (sqlite backup, swapped in atomically, pulled over SSH).
    try:
        src = sqlite3.connect(DB)
        etmp = BASE / 'history-export.tmp.sqlite3'
        if etmp.exists():
            etmp.unlink()
        dst = sqlite3.connect(etmp)
        src.backup(dst)
        dst.close()
        src.close()
        etmp.replace(BASE / 'history-export.sqlite3')
        os.chmod(BASE / 'history-export.sqlite3', 0o644)
    except Exception:
        pass


if __name__ == '__main__':
    main()
