# The file reader's locked box (server.py says what it is for). Only Python,
# the two parsers and cryptography - pypdf's AES provider, so a PDF encrypted
# with an empty user password (bank statements and invoices often are) reads
# as it did in the backend, which had it through python-jose. No brotli and
# no jbig2dec, so pypdf refuses those streams (dependency_missing) instead of
# handing them to native decoders.
# The locks themselves - no network, read-only, no capabilities, capped - are
# set where the container runs (docker-compose.yml's extractor service) and
# proven from inside it by check_locks.py.
FROM python:3.11-slim
WORKDIR /extractor
# The base image's own packages, patched: the same upgrade, for the same
# reason, as backend/Dockerfile.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# The base image's setuptools and wheel, removed: nothing in this box builds or
# installs a package after the line above, and the base's setuptools 79.0.1
# vendors jaraco.context and wheel releases with fixed HIGH CVEs (the weekly
# image scan, 2026-11-21). The box keeps what it runs.
RUN pip uninstall -y -q setuptools wheel
COPY server.py worker.py check_locks.py ./
# A user with no shell and no home, and the socket folder it may write. 1777
# so the folder works whichever container first mounts the shared volume.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin extractor \
    && mkdir -p /run/extractor && chown 10001:10001 /run/extractor && chmod 1777 /run/extractor
USER 10001:10001
CMD ["python", "-I", "server.py"]
