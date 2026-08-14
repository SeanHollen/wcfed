#!/usr/bin/env bash
# End-to-end federation on one machine, no network, no other operator.
#
# Stands up a relay and TWO gateways in different orgs, then drives real
# traffic through the real code path: sign -> relay -> long-poll -> verify ->
# screen -> quarantine -> sink -> ack.
#
# Run this FIRST, on both machines, before either side touches the other. If it
# passes on your box, every failure after that is configuration or network —
# which is a much shorter list to search.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
RELAY_PORT="${RELAY_PORT:-18787}"
GW_A_PORT="${GW_A_PORT:-18801}"
GW_B_PORT="${GW_B_PORT:-18802}"
PIDS=()
PASS=0
FAIL=0

cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
  [ "${KEEP_TMP:-0}" = "1" ] && echo "artifacts kept in $TMP" || rm -rf "$TMP"
}
trap cleanup EXIT

ok()   { PASS=$((PASS+1)); printf '  \033[32mPASS\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m %s\n' "$1"; [ -n "${2:-}" ] && printf '       %s\n' "$2"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# Wait for a URL to answer, rather than sleeping a guessed interval.
wait_http() {
  local url="$1" tries="${2:-60}"
  for _ in $(seq "$tries"); do
    curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && return 0
    sleep 0.25
  done
  return 1
}

SECRET="$(env PYTHONPATH="$ROOT" python3 -c 'import secrets; print(secrets.token_hex(32))')"
TOK_A="tokA-$(env PYTHONPATH="$ROOT" python3 -c 'import secrets; print(secrets.token_hex(8))')"
TOK_B="tokB-$(env PYTHONPATH="$ROOT" python3 -c 'import secrets; print(secrets.token_hex(8))')"

echo "wcfed selftest"
echo "root=$ROOT tmp=$TMP relay=:$RELAY_PORT"

# ---------------------------------------------------------------- unit
head_ "1. envelope: canonical form, signing, tamper detection"
env PYTHONPATH="$ROOT" python3 - <<'PY'
import json, sys
from wcfed.envelope import build, verify, canonical, validate, EnvelopeError, parse_address

fails = []
def check(label, cond):
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond: fails.append(label)

s = "s3cr3t"
e = build(from_org="a", from_handle="x", to_org="b", to_handle="y", text="hello", secret=s)
check("signature verifies", verify(e, s))
check("wrong secret fails", not verify(e, "other"))

for field, val in (("text", "hello!"), ("depth", 5), ("conv", "zzzz")):
    t = dict(e); t[field] = val
    check(f"tampering with {field} is caught", not verify(t, s))

t = dict(e); t["from"] = {"org": "evil", "handle": "x"}
check("tampering with from is caught", not verify(t, s))

# Canonical form must survive a JSON round trip and key reordering, or a
# signature made on one machine will not verify on another.
rt = json.loads(json.dumps(e))
check("canonical stable over json round trip", canonical(e) == canonical(rt))
shuffled = dict(reversed(list(e.items())))
check("canonical stable over key order", canonical(e) == canonical(shuffled))

# Non-ASCII is where ensure_ascii would silently break interop.
u = build(from_org="a", from_handle="x", to_org="b", to_handle="y", text="héllo — ✅", secret=s)
check("non-ascii text verifies", verify(u, json.loads(json.dumps(s))))
check("non-ascii survives round trip", canonical(u) == canonical(json.loads(json.dumps(u))))

check("addr parses", parse_address("@docs@zamua") == ("docs", "zamua"))
try:
    parse_address("docs")
    check("bare handle rejected", False)
except EnvelopeError:
    check("bare handle rejected", True)

for bad_env, why in [
    ({**e, "kind": "shout"}, "unknown kind"),
    ({**e, "depth": -1}, "negative depth"),
    ({**e, "v": 99}, "wrong version"),
    ({**e, "surprise": 1}, "unexpected field"),
    ({**e, "to": {"org": "B", "handle": "y"}}, "uppercase org"),
    ({**e, "text": "x" * 20000}, "oversized text"),
]:
    try:
        validate(bad_env); check(f"{why} rejected", False)
    except EnvelopeError:
        check(f"{why} rejected", True)

sys.exit(1 if fails else 0)
PY
if [ $? -eq 0 ]; then ok "envelope unit checks"; else bad "envelope unit checks"; fi

# ---------------------------------------------------------------- relay
head_ "2. relay + two gateways"
env PYTHONPATH="$ROOT" python3 -m wcfed.cli relay --host 127.0.0.1 --port "$RELAY_PORT" \
  --tokens "orga:$TOK_A,orgb:$TOK_B" >"$TMP/relay.log" 2>&1 &
PIDS+=($!)
if wait_http "http://127.0.0.1:$RELAY_PORT/v1/health"; then ok "relay is up"; else
  bad "relay is up" "$(tail -5 "$TMP/relay.log")"; echo; echo "FAILED"; exit 1
fi

mk_env() {  # org, port, sink_target
  cat >"$TMP/$1.env" <<EOF
WCFED_ORG=$1
WCFED_RELAY_URL=http://127.0.0.1:$RELAY_PORT
WCFED_RELAY_TOKEN=$2
WCFED_PEERS=$3:$SECRET
WCFED_SINK=echo
WCFED_SINK_TARGET=$TMP/inbox-$1.jsonl
WCFED_LISTEN=127.0.0.1:$4
WCFED_STATE_DIR=$TMP/state-$1
WCFED_POLL_WAIT=2
WCFED_DEPTH_MAX=8
WCFED_RATE_PER_MIN=30
EOF
}
mk_env orga "$TOK_A" orgb "$GW_A_PORT"
mk_env orgb "$TOK_B" orga "$GW_B_PORT"

for org in orga orgb; do
  port=$([ "$org" = orga ] && echo "$GW_A_PORT" || echo "$GW_B_PORT")
  env PYTHONPATH="$ROOT" WCFED_ENV_FILE="$TMP/$org.env" \
    python3 -m wcfed.cli gateway >"$TMP/gw-$org.log" 2>&1 &
  PIDS+=($!)
  if wait_http "http://127.0.0.1:$port/health"; then ok "gateway $org is up"; else
    bad "gateway $org is up" "$(tail -5 "$TMP/gw-$org.log")"
  fi
done

# ---------------------------------------------------------------- doctor
head_ "3. doctor"
if env PYTHONPATH="$ROOT" WCFED_ENV_FILE="$TMP/orga.env" \
    python3 -m wcfed.cli doctor >"$TMP/doctor.log" 2>&1; then
  ok "doctor reports all green"
else
  bad "doctor reports all green" "$(grep FAIL "$TMP/doctor.log" | head -5)"
fi

# ---------------------------------------------------------------- traffic
post() {  # gateway_port, json
  curl -fsS --max-time 20 -X POST "http://127.0.0.1:$1/outbound" \
    -H 'Content-Type: application/json' --data "$2" 2>/dev/null
}
inbox_has() {  # org, needle, tries
  for _ in $(seq "${3:-40}"); do
    grep -qF "$2" "$TMP/inbox-$1.jsonl" 2>/dev/null && return 0
    sleep 0.25
  done
  return 1
}

head_ "4. round trip in both directions"
CONV=$(post "$GW_A_PORT" '{"to":"y@orgb","text":"ping from A","from":"frontend"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["conv"])' 2>/dev/null)
if [ -n "$CONV" ]; then ok "A accepted outbound (conv $CONV)"; else bad "A accepted outbound"; fi
if inbox_has orgb "ping from A"; then ok "B received A's message"; else
  bad "B received A's message" "$(tail -5 "$TMP/gw-orgb.log")"; fi

post "$GW_B_PORT" "{\"to\":\"frontend@orga\",\"text\":\"pong from B\",\"conv\":\"$CONV\",\"depth\":1,\"from\":\"y\"}" >/dev/null
if inbox_has orga "pong from B"; then ok "A received B's reply on the same conv"; else
  bad "A received B's reply" "$(tail -5 "$TMP/gw-orga.log")"; fi
if grep -q "\"conv\": \"$CONV\"" "$TMP/inbox-orga.jsonl" 2>/dev/null \
   || grep -qF "$CONV" "$TMP/inbox-orga.jsonl" 2>/dev/null; then
  ok "conversation id survives the round trip"
else bad "conversation id survives the round trip"; fi

head_ "5. quarantine framing reaches the sink"
if grep -qF "EXTERNAL MESSAGE" "$TMP/inbox-orgb.jsonl" 2>/dev/null \
   && grep -qF "BEGIN EXTERNAL MESSAGE" "$TMP/inbox-orgb.jsonl" 2>/dev/null; then
  ok "delivered body carries the quarantine wrapper"
else bad "delivered body carries the quarantine wrapper"; fi
if grep -qF "DIFFERENT operator" "$TMP/inbox-orgb.jsonl" 2>/dev/null; then
  ok "wrapper names the trust boundary"
else bad "wrapper names the trust boundary"; fi

head_ "6. kinds"
post "$GW_A_PORT" '{"to":"y@orgb","text":"ambient post","kind":"post","from":"frontend"}' >/dev/null
if inbox_has orgb "ambient post"; then ok "kind=post is carried"; else bad "kind=post is carried"; fi

# ---------------------------------------------------------------- security
head_ "7. screening: forgery, depth, replay, address"
BEFORE=$(wc -l <"$TMP/inbox-orgb.jsonl" 2>/dev/null || echo 0)

# A forged envelope, signed with the wrong secret, injected straight at the
# relay with valid relay credentials. The relay is not the thing that stops
# this; the receiving gateway's signature check is.
env PYTHONPATH="$ROOT" python3 - "$RELAY_PORT" "$TOK_A" <<'PY' >"$TMP/forge.log" 2>&1
import json, sys, urllib.request
from wcfed.envelope import build
port, tok = sys.argv[1], sys.argv[2]
env = build(from_org="orga", from_handle="attacker", to_org="orgb", to_handle="y",
            text="FORGED-should-never-arrive", secret="wrong-secret")
req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/send",
    data=json.dumps(env).encode(), method="POST",
    headers={"Content-Type": "application/json", "X-Wcfed-Org": "orga", "X-Wcfed-Auth": tok})
print(urllib.request.urlopen(req, timeout=10).read().decode())
PY
sleep 2
if ! grep -qF "FORGED-should-never-arrive" "$TMP/inbox-orgb.jsonl" 2>/dev/null; then
  ok "forged signature is never delivered"
else bad "forged signature is never delivered" "IT ARRIVED — signature check is broken"; fi
if grep -q "REJECTED" "$TMP/gw-orgb.log" && grep -q "bad signature" "$TMP/gw-orgb.log"; then
  ok "forgery is logged as a rejection"
else bad "forgery is logged as a rejection" "$(tail -5 "$TMP/gw-orgb.log")"; fi

# Spoofed return address: relay must refuse an envelope whose `from` does not
# match the authenticated org, so one peer cannot impersonate another.
SPOOF=$(env PYTHONPATH="$ROOT" python3 - "$RELAY_PORT" "$TOK_A" <<'PY' 2>&1
import json, sys, urllib.request, urllib.error
from wcfed.envelope import build
port, tok = sys.argv[1], sys.argv[2]
env = build(from_org="orgb", from_handle="impostor", to_org="orgb", to_handle="y",
            text="SPOOFED", secret="whatever")
req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/send",
    data=json.dumps(env).encode(), method="POST",
    headers={"Content-Type": "application/json", "X-Wcfed-Org": "orga", "X-Wcfed-Auth": tok})
try:
    urllib.request.urlopen(req, timeout=10); print("ACCEPTED")
except urllib.error.HTTPError as e: print(f"REFUSED {e.code}")
PY
)
if [[ "$SPOOF" == REFUSED* ]]; then ok "relay refuses a spoofed return address ($SPOOF)"; else
  bad "relay refuses a spoofed return address" "got: $SPOOF"; fi

# Unauthenticated poll must not drain another org's queue.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
  -H "X-Wcfed-Org: orgb" -H "X-Wcfed-Auth: not-the-token" \
  "http://127.0.0.1:$RELAY_PORT/v1/poll?wait=0")
if [ "$CODE" = "401" ]; then ok "relay rejects a bad token (401)"; else
  bad "relay rejects a bad token" "got HTTP $CODE"; fi

# Depth cap: outbound refuses at the cap, so a runaway thread cannot spend the
# other operator's tokens.
RESP=$(curl -s --max-time 20 -X POST "http://127.0.0.1:$GW_A_PORT/outbound" \
  -H 'Content-Type: application/json' \
  --data '{"to":"y@orgb","text":"too deep","depth":9,"from":"frontend"}')
if printf '%s' "$RESP" | grep -q "depth"; then ok "outbound depth cap refuses depth 9"; else
  bad "outbound depth cap refuses depth 9" "got: $RESP"; fi

# Replay: the same envelope id delivered twice must reach the sink once.
env PYTHONPATH="$ROOT" python3 - "$RELAY_PORT" "$TOK_A" "$SECRET" <<'PY' >>"$TMP/forge.log" 2>&1
import json, sys, time, urllib.request
from wcfed.envelope import build
port, tok, secret = sys.argv[1], sys.argv[2], sys.argv[3]
env = build(from_org="orga", from_handle="frontend", to_org="orgb", to_handle="y",
            text="REPLAY-ME-ONCE", secret=secret)
for _ in range(3):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/send",
        data=json.dumps(env).encode(), method="POST",
        headers={"Content-Type": "application/json", "X-Wcfed-Org": "orga", "X-Wcfed-Auth": tok})
    urllib.request.urlopen(req, timeout=10).read()
    time.sleep(0.3)
PY
sleep 4
COUNT=$(grep -cF "REPLAY-ME-ONCE" "$TMP/inbox-orgb.jsonl" 2>/dev/null || echo 0)
if [ "$COUNT" = "1" ]; then ok "replayed envelope is delivered exactly once"; else
  bad "replayed envelope is delivered exactly once" "delivered $COUNT times"; fi

# Wrong destination org must not be delivered even with a good signature.
env PYTHONPATH="$ROOT" python3 - "$RELAY_PORT" "$TOK_A" "$SECRET" <<'PY' >>"$TMP/forge.log" 2>&1
import json, sys, urllib.request
from wcfed.envelope import build
port, tok, secret = sys.argv[1], sys.argv[2], sys.argv[3]
env = build(from_org="orga", from_handle="frontend", to_org="orgb", to_handle="y",
            text="MISROUTED", secret=secret)
env["to"]["org"] = "orgc"          # after signing: signature no longer matches
req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/send",
    data=json.dumps(env).encode(), method="POST",
    headers={"Content-Type": "application/json", "X-Wcfed-Org": "orga", "X-Wcfed-Auth": tok})
try:
    print(urllib.request.urlopen(req, timeout=10).read().decode())
except Exception as e:
    print("refused:", e)
PY
sleep 2
if ! grep -qF "MISROUTED" "$TMP/inbox-orgb.jsonl" 2>/dev/null; then
  ok "misrouted envelope is never delivered"
else bad "misrouted envelope is never delivered"; fi

# ---------------------------------------------------------------- report
head_ "8. gateway health"
for org in orga orgb; do
  port=$([ "$org" = orga ] && echo "$GW_A_PORT" || echo "$GW_B_PORT")
  H=$(curl -fsS --max-time 10 "http://127.0.0.1:$port/health" 2>/dev/null)
  if printf '%s' "$H" | grep -q '"ok": true'; then
    ok "$org health: $(printf '%s' "$H" | python3 -c 'import json,sys; s=json.load(sys.stdin)["stats"]; print(s)' 2>/dev/null)"
  else bad "$org health"; fi
done

printf '\n\033[1m%s\033[0m\n' "selftest: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  echo "logs in $TMP (re-run with KEEP_TMP=1 to keep them)"
  exit 1
fi
echo "Federation works on this machine. Next: INTEROP.md"
