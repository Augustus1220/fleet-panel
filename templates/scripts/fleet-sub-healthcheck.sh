#!/bin/bash
# Health check for the subscription service: request the local subscription
# endpoint (root path, expected 404). Two consecutive failures -> restart
# the service. CHECK_PORT and DRY_RUN exist only for drills.
# Log lines ("check failed", "restarting fleet-subscription") are parsed by
# the panel collector to build the anomaly log - keep the wording stable.
STATE=/run/fleet-sub-health-failures
PORT="${CHECK_PORT:-2097}"
THRESHOLD=2

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  --resolve "node.example.com:${PORT}:127.0.0.1" \
  "https://node.example.com:${PORT}/" 2>/dev/null)

if [ "$code" = "404" ]; then
  echo 0 > "$STATE"
  exit 0
fi

n=0
[ -f "$STATE" ] && n=$(cat "$STATE" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$STATE"
logger -t fleet-sub-health "check failed ($n/$THRESHOLD): http_code=${code:-none} port=$PORT"

if [ "$n" -ge "$THRESHOLD" ]; then
  echo 0 > "$STATE"
  if [ -n "$DRY_RUN" ]; then
    logger -t fleet-sub-health "DRY_RUN: would restart fleet-subscription now"
    echo "DRY_RUN would restart"
  else
    logger -t fleet-sub-health "restarting fleet-subscription after $n consecutive failures"
    systemctl restart fleet-subscription.service
  fi
fi
exit 0
