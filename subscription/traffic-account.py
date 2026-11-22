#!/usr/bin/env python3
"""Accumulate WAN interface counters into a persistent state file.

Runs every 2 minutes via fleet-traffic-account.timer. Counters in
/proc/net/dev reset at reboot, so deltas are folded into acc_* fields
and the boot_id is tracked; at most one interval of traffic is lost
across a reboot.
"""
import json
import time
from pathlib import Path

STATE = Path('/var/lib/fleet-subscriptions/traffic_state.json')
IFACE = 'ens7'  # REPLACE: your WAN interface name


def counters():
    for line in Path('/proc/net/dev').read_text().splitlines():
        if line.strip().startswith(IFACE + ':'):
            parts = line.split(':', 1)[1].split()
            return int(parts[0]), int(parts[8])
    raise RuntimeError(f'interface {IFACE} not found')


rx, tx = counters()
boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
try:
    st = json.loads(STATE.read_text())
except Exception:
    st = {'boot_id': boot, 'iface': IFACE, 'last_rx': 0, 'last_tx': 0,
          'acc_rx': 0, 'acc_tx': 0}

if st.get('boot_id') == boot:
    st['acc_rx'] = int(st['acc_rx']) + max(0, rx - int(st['last_rx']))
    st['acc_tx'] = int(st['acc_tx']) + max(0, tx - int(st['last_tx']))
else:
    st['boot_id'] = boot
st['iface'] = IFACE
st['last_rx'] = rx
st['last_tx'] = tx
st['updated'] = int(time.time())

tmp = STATE.with_suffix('.json.tmp')
tmp.write_text(json.dumps(st))
tmp.replace(STATE)
print(f"acc_rx={st['acc_rx']} acc_tx={st['acc_tx']}")
