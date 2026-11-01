# The file reader's locked box (server.py says what it is for). Only Python
# and the two parsers: no brotli and no jbig2dec, so pypdf refuses those
# streams (dependency_missing) instead of handing them to native decoders.
# The locks themselves - no network, read-only, no capabilities, capped - are
# set where the container runs (docker-compose.yml's extractor service) and
# proven from inside it by check_locks.py.
FROM python:3.11-slim
WORKDIR /extractor
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py worker.py check_locks.py ./
# A user with no shell and no home, and the socket folder it may write. 1777
# so the folder works whichever container first mounts the shared volume.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin extractor \
    && mkdir -p /run/extractor && chown 10001:10001 /run/extractor && chmod 1777 /run/extractor
USER 10001:10001
CMD ["python", "-I", "server.py"]
