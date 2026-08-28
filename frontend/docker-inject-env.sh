#!/bin/sh
# Replace the build-time placeholders in the static bundle with this container's
# real runtime env. Runs once at container start, before nginx.
#
# These shell defaults are the ones that apply in a built image - the TSX
# fallbacks are folded away at build time (see the Dockerfile comment). Keep
# the two in step by hand; nothing enforces it.
set -e
ROOT=/usr/share/nginx/html

# Escape chars that are special in a sed replacement (& | \).
esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }

# API_URL defaults to EMPTY, which makes the client call its own origin and go
# through the /api proxy in nginx.conf. Set VITE_API_URL only when the backend
# is served from somewhere else.
API=$(esc "${VITE_API_URL:-}")
NAME=$(esc "${VITE_INSTANCE_NAME:-Architecture Zero}")
COLOR=$(esc "${VITE_PRIMARY_COLOR:-#2563eb}")

find "$ROOT" -type f \( -name '*.js' -o -name '*.html' \) -exec sed -i \
  "s|__RT_VITE_API_URL__|$API|g; s|__RT_VITE_INSTANCE_NAME__|$NAME|g; s|__RT_VITE_PRIMARY_COLOR__|$COLOR|g" {} +

echo "[inject-env] applied runtime config (instance='${VITE_INSTANCE_NAME:-Architecture Zero}')"
