#!/usr/bin/env bash
# post-deploy verification for the group-ops bot test server.
# Read-only: probes endpoints and prints state. Exits non-zero on hard failures.
set -uo pipefail

PORT=12334
BASE=http://127.0.0.1:$PORT
PUB=http://219.151.177.33:$PORT
fail=0

check() {  # name, url, expect_http
    local name=$1 url=$2 want=$3 got
    got=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "$url" || echo "000")
    if [ "$got" = "$want" ]; then
        echo "PASS  $name -> $got"
    else
        echo "FAIL  $name -> $got (want $want)"
        fail=1
    fi
}

echo "== listening sockets =="
ss -tlnp | grep -E ":$PORT|:18003" || echo "(no matching sockets)"

echo "== healthz =="
check "healthz local"  "$BASE/healthz" 200
check "healthz public" "$PUB/healthz"  200

echo "== H5 pages =="
check "h5 checkin" "$BASE/h5/checkin" 200
check "h5 report"  "$BASE/h5/report"  200

echo "== callback accepts POST =="
code=$(curl -s -o /tmp/cb_resp.json -w '%{http_code}' --max-time 8 -X POST \
    -H 'Content-Type: application/json' \
    -d '{"group_id":"G-TEST","msg_type":"text","text":"帮助","sender":{"user_id":"22644212509","name":"测试"}}' \
    "$BASE/api/callback" || echo "000")
echo "callback -> $code  body: $(head -c 200 /tmp/cb_resp.json 2>/dev/null)"
[ "$code" = "200" ] || fail=1

echo "== admin backend (must be localhost-only) =="
check "admin local" "http://127.0.0.1:18003/healthz" 200
pub_admin=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://219.151.177.33:18003/healthz" || echo "000")
if [ "$pub_admin" = "000" ]; then
    echo "PASS  admin not reachable from public IP (expected)"
else
    echo "FAIL  admin reachable publicly -> $pub_admin (section 2.10 violation)"
    fail=1
fi

echo "== recent logs =="
journalctl -u group-ops-bot --no-pager --lines=10 || true

echo "== service state =="
systemctl is-enabled group-ops-bot; systemctl is-active group-ops-bot

[ "$fail" = "0" ] && echo "RESULT: PASS" || echo "RESULT: FAIL (see above)"
exit $fail
