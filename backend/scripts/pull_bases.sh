#!/usr/bin/env bash
# Pull every Dockerfile base by name before this host builds (2026-11-21).
#
# A build host keeps the bases it pulled first and builds on them from then
# on: a build without --pull takes the local tag, and a build's own --pull
# does not move that tag, so the next plain build is back on the old base. A
# host deployed that way still ran nginx 1.30.6 days after 1.30.7 fixed a TLS
# denial of service, and a zlib with a fixed heap overflow in every Python
# image. docker pull moves the tag, so every build after this one, test or
# deploy, starts from the base the registry holds now.
#
# The list is list_scanned_images.py --bases: every Dockerfile's FROM, read
# without a compose file or PyYAML. A pull that fails warns and the build goes
# on from the cached copy: a registry outage must not stop a deploy whose code
# is fine, and the weekly image scan builds fresh either way. A listing that
# fails stops here: that is a broken reading, not an outage.
#
# A compose service on the same host must not run one of these tags as its own
# image: moving the tag recreates that container at its next `compose up -d`,
# dropping its open connections. Pin such a service to an exact version
# (test_list_scanned_images.py guards this repository's default compose file).
#
#   bash backend/scripts/pull_bases.sh    # from anywhere: it finds its repository
#
# PULL_BASES_PYTHON names the interpreter (default python3); the tests set it.
set -euo pipefail
cd "$(dirname "$0")/../.."
python="${PULL_BASES_PYTHON:-python3}"
bases="$("$python" backend/scripts/list_scanned_images.py --bases)"
bases="${bases//$'\r'/}"   # a Python on Windows ends each line CRLF
for img in $bases; do
    if docker pull -q "$img" >/dev/null; then
        echo "base pulled: $img"
    else
        echo "::warning::could not pull base $img; this build uses the cached copy"
    fi
done
