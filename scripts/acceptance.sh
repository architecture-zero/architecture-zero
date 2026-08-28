#!/usr/bin/env bash
#
# End-to-end acceptance for a RUNNING deployment.
#
#   docker compose up -d --build
#   ./scripts/acceptance.sh
#
# The unit suites prove the parts. This proves the assembly: that a fresh
# deployment can be claimed, signed into, and answered by - through the same
# origin a browser uses.
#
# EVERYTHING GOES THROUGH THE FRONTEND ORIGIN ON PURPOSE. Hitting the backend
# directly would skip the nginx /api proxy, which is what makes the stock
# compose file work with no configuration at all - and it would skip the
# response headers, which are served by the frontend and not by the API.
#
# It expects a deployment with NO Owner yet (a fresh backend/data), because the
# claim is one of the things under test and it can only happen once. Against an
# already-claimed instance the claim and login checks will fail, correctly.
#
# Reset to a fresh one with:
#   docker compose down && rm -rf backend/data && mkdir backend/data
#
# Chat needs a reachable model (OLLAMA_BASE) and an embedder (EMBED_BASE); with
# neither, the last two checks fail and the rest still tell you something.

set -uo pipefail

BASE="${1:-http://localhost:5173}"
USER_NAME="${ACCEPT_USER:-acceptance-owner}"
PASS_WORD="${ACCEPT_PASS:-AcceptPass1!}"

pass=0
fail=0
ok()  { echo "  PASS  $1"; pass=$((pass + 1)); }
bad() { echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "        ${2}"; fail=$((fail + 1)); }

echo "acceptance against $BASE"
echo

echo "== 1. the client is served, and configured =="
body=$(curl -s -m 10 "$BASE/")
if grep -q '<div id="root">' <<<"$body"; then
  ok "index.html served"
else
  bad "index.html served" "$(head -c 200 <<<"$body")"
fi
# The image bakes __RT_VITE_* placeholders and the entrypoint replaces them from
# the container's env. A surviving placeholder means that script did not run -
# the classic cause is CRLF line endings on the .sh (see .gitattributes).
if grep -q '__RT_VITE_' <<<"$body"; then
  bad "runtime placeholders replaced" "a literal __RT_VITE_* survived: the entrypoint did not run"
else
  ok "runtime placeholders replaced"
fi

echo "== 2. security headers, on the document a browser loads =="
# Checked on '/' specifically. nginx drops every INHERITED add_header the moment
# a location declares one of its own, and '/' resolves through try_files into
# the cache-control block - so headers declared only at server level are present
# on the API and absent here, which is exactly backwards. This check exists
# because that shipped once.
hdr=$(curl -s -m 10 -D - -o /dev/null "$BASE/")
for h in x-content-type-options x-frame-options referrer-policy permissions-policy; do
  if grep -qi "^$h:" <<<"$hdr"; then ok "$h"; else bad "$h" "absent on /"; fi
done

echo "== 3. the /api proxy reaches the backend =="
cfg=$(curl -s -m 10 "$BASE/api/auth/config")
if grep -q 'needs_setup' <<<"$cfg"; then
  ok "GET /api/auth/config: $cfg"
else
  bad "GET /api/auth/config through the proxy" "$cfg"
fi

echo "== 4. an unclaimed deployment says so =="
if grep -q '"needs_setup":true' <<<"${cfg// /}"; then
  ok "needs_setup true - the client routes to #setup"
else
  bad "needs_setup true" "already claimed, or the key is missing: $cfg"
fi

echo "== 5. claim, with the code from the container logs =="
code=$(docker compose logs backend 2>/dev/null \
  | grep -oE 'claim code[^A-Za-z0-9_-]*[A-Za-z0-9_-]{20,}' \
  | grep -oE '[A-Za-z0-9_-]{20,}$' | tail -1)
if [ -z "$code" ]; then
  bad "claim code" "not found in the backend logs - is the deployment already claimed?"
else
  ok "claim code read from the logs"
  claim=$(curl -s -m 30 -X POST "$BASE/api/auth/setup" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USER_NAME\",\"password\":\"$PASS_WORD\",\"claim_code\":\"$code\"}")
  if grep -q 'owner created' <<<"$claim"; then ok "owner created"; else bad "claim" "$claim"; fi
fi

echo "== 6. sign in =="
login=$(curl -s -m 30 -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$USER_NAME\",\"password\":\"$PASS_WORD\"}")
TOK=$(sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p' <<<"$login")
if [ -n "$TOK" ]; then ok "access token"; else bad "access token" "$login"; fi
# The claim screen stores this too; without it the first session on a new
# deployment is the one session that cannot renew.
if grep -q '"refresh_token"' <<<"$login"; then ok "refresh token"; else bad "refresh token" "$login"; fi
if grep -q '"needs_setup":false' <<<"$(curl -s -m 10 "$BASE/api/auth/config" | tr -d ' ')"; then
  ok "needs_setup flipped to false"
else
  bad "needs_setup false after the claim" "the claim did not take"
fi

echo "== 7. retrieval is on by default =="
conf=$(curl -s -m 10 "$BASE/api/config" -H "Authorization: Bearer $TOK")
if grep -q '"default_rag_enabled":true' <<<"${conf// /}"; then
  ok "default_rag_enabled is true"
else
  bad "default_rag_enabled" "a first question would answer from training memory: $conf"
fi

echo "== 8. a first question is answered, and grounded =="
ans=$(curl -s -m 300 -X POST "$BASE/api/chat" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOK" \
  -d '{"prompt":"What is Architecture Zero?","use_rag":true,"history":[],"session_id":"acceptance"}')
if grep -q '\[DONE\]' <<<"$ans"; then ok "the answer streamed to [DONE]"; else bad "chat stream" "$(head -c 400 <<<"$ans")"; fi
if grep -qi 'sources' <<<"$ans"; then
  ok "the answer carried sources - retrieval ran"
else
  bad "sources" "answered without citing the corpus: $(head -c 400 <<<"$ans")"
fi

echo
echo "==== $pass passed, $fail failed ===="
[ "$fail" -eq 0 ]
