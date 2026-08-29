# Runbook - deploying and operating Architecture Zero

This runbook is itself part of the shipped corpus: once the instance is
running, you can ask the assistant these questions directly.

## Deploy on a single machine

Prerequisites: Docker with the compose plugin, and an Ollama install on the
host with the embedding model pulled (`ollama pull nomic-embed-text`), plus a
chat model if you want local inference (`ollama pull qwen3:8b`). The embedder
is required whichever provider answers chat - only the chat model is swappable
for a cloud API.

1. Clone the repository.
2. `cp .env.example .env` and set JWT_SECRET_KEY to a real secret
   (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
   Auth fails closed on the placeholder - the backend will not boot on the
   default value, whether or not ENABLE_AUTH is on. The guard runs at import
   and never consults that flag.
3. `docker compose up -d --build` (the first build downloads and bakes the
   reranker models into the image).
4. Open http://localhost:8000/api/health - expect status healthy (or
   degraded if the CHAT endpoint is not up yet, which a cloud-only deployment
   can ignore). It says nothing about the embedder: that is a separate
   endpoint (`EMBED_BASE`, model `nomic-embed-text`) and it is required no
   matter which provider answers chat. Without it the instance still boots and
   reports healthy, every ingest fails, and a question asked with retrieval on
   - the default - answers 500 rather than answering ungrounded.
5. Create the Owner account. This takes a **claim code**, which the backend
   mints at boot and prints to its own logs while the deployment is unclaimed -
   run `docker compose logs backend` and look for the banner. Only someone who
   can already read your server logs has seen it, which is what stops a
   publicly-reachable deployment being taken by whoever finds it first in the
   minutes before you get here. The code dies the moment the Owner exists, and
   a restart before then mints a new one.
   `curl -X POST localhost:8000/api/auth/setup -H "Content-Type: application/json" -d "{\"username\":\"owner\",\"password\":\"<strong password>\",\"claim_code\":\"<code from the logs>\"}"`
   Running multiple workers or replicas? Set `SETUP_CLAIM_CODE` in `.env`
   instead - the generated code lives in ONE process's memory, so with several
   processes only one of them would accept yours.
6. Sign in. The shipped corpus (help docs + demo company KB) ingests on
   first boot; watch for the `startup_sync_done` lines in
   `docker compose logs backend`.
7. Ask your first question - about the platform itself. Sign in (POST
   /api/auth/login, same credentials as setup) and use the returned
   `access_token` as a Bearer token:
   `curl -N -X POST localhost:8000/api/chat -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" -d "{\"prompt\":\"How do I add my first documents?\"}"`
   No `use_rag` field is needed: omitted, it follows the instance's
   `default_rag_enabled` setting, which ships true - so the answer is
   grounded in the corpus with citations. Send `"use_rag": false` to ask
   the model without the corpus. The answer streams back as server-sent events. The corpus the
   instance just booted with is its own manual, so it can onboard and
   troubleshoot you before you have ingested a single document.

## Updating

`git pull && docker compose up -d --build`. The container recreates; the
startup sync re-ingests only changed files (content-addressed deltas), and
the eval question set reconciles from the seed file automatically.

**One-time note for instances created before session ids became per-owner.**
`chat_sessions` originally required a session id to be unique across the whole
deployment, while the code that reads those rows scoped them to their owner - so
a second account's first message failed. The next boot rebuilds that table once,
automatically, and logs if it cannot. **Back up first**: take a backup
(`POST /api/admin/backup`) or copy `backend/data/` with the container stopped.
The rebuild copies, drops and renames a live table, and the database runs in WAL
mode with an active writer. A fresh instance is unaffected and skips it.

## Live system records

At the end of every boot the instance generates a small set of records
describing its own posture, corpus and measurement state, and indexes them
at Owner clearance. They are what lets the assistant answer "is the
injection scan on here?" from this deployment rather than from the
documentation. Watch for `startup_sync_done stage=system-records` in the
boot log; `POST /api/kb/sync` regenerates them on demand and returns their
status alongside the file syncs. Regenerating is cheap - an unchanged
record re-embeds nothing.

## Stopping safely

`docker compose stop` (or down). The compose file sets stop_grace_period
to 45s ON PURPOSE: uvicorn drains connections and the shutdown hook
flushes the vector index's unpersisted tail. Do not shorten it - a kill
that beats the flush can lose recently written vectors (the startup
completeness check will re-ingest them, but a graceful stop is free).

## Red-teaming the injection defense

`backend/scripts/injection_probe.py` measures the one control with no status
surface: when poisoned third-party content IS in the model's context, does the
answer obey it? It plants the shipped poison fixture into throwaway
departments, asks through the same pipeline chat uses, and grades the answers
mechanically - no judge, so no second set of error bars.

    docker compose exec backend python scripts/injection_probe.py

**It writes to your corpus**, with the ingestion gate waived on purpose - the
answer layer only runs if the content gets through. Against a non-empty corpus
it refuses to start without `--i-know-this-writes`, and prints exactly what it
would create first. It also refuses a `--department` that would resolve onto
your real corpus or onto a declared access tier, because its cleanup deletes by
source name and a running evaluation plants that same source name.

Cleanup runs in a `finally`, so it survives an error or a Ctrl-C. It does not
survive `docker kill` or an OOM - if that happens, run the probe again; it
sweeps whatever the killed run left before it plants anything new.

Three arms are reported. The `curated` one is the number to read: it plants the
same poison as if it had arrived through a trusted path, so nothing stands
between the attack and the prompt rules. Exit 0 means every arm held.

## Index maintenance

Every boot runs a short, embed-free pass over the vector store before the
ingest syncs. It clears debris that deleting a collection leaves behind, and it
finds records whose metadata outlived their vector - the silent failure mode
here, because an unclean stop can lose vectors written since the last flush
while the sqlite metadata survives, and the ingest skip-check counts a dead
record as present. Dead records are dropped and their sources are queued for
re-embedding on the same boot. Look for `chroma_maintenance` in the log; it
reports every boot, including the boots where it found nothing.

`params_drift` in that line means a collection's index parameters differ from
the current target. The instance will NOT act on it: adopting new parameters
means dropping and re-adding a healthy collection, and the only copy of its
records lives in memory until the re-add finishes. It is reported so you can
decide.

**The force-rebuild lever.** Write a JSON list of collection names to
`backend/data/force-rebuild.json` and restart once:

    echo '["knowledge_base"]' > backend/data/force-rebuild.json

Any editor will do - the file just has to hold that JSON, in UTF-8. On Windows
write it from an editor rather than with PowerShell's `>`, which produces
UTF-16 and will not parse.

The next boot rebuilds exactly those collections and deletes the file, so it
fires once even if that boot dies - which is what makes it usable during a
restart loop. It is the cure for write-side index corruption, which has no safe
in-process probe: an index can crash the process natively on the first write of
every boot while every read-side check stays green. A name that matches no
collection is logged as an error rather than ignored.

**Back up first** (`POST /api/admin/backup`, or copy `backend/data/` with the
container stopped). A rebuild exports a collection to memory, drops it, and
re-adds it. Documents that came from files on disk can always be re-ingested;
**uploaded documents have no copy outside the index**, so if a rebuild is
interrupted they are gone.

## Large uploads and the ingest queue

By default an upload is indexed inside the request: `POST /api/ingest/upload`
returns once the document is chunked and embedded. On a large file over a slow
embedding backend that is a long-held connection, and a proxy timing out in
front of it turns a working ingest into an error the caller cannot tell apart
from a failure.

Set `ENABLE_ASYNC_JOBS=true` to queue instead. The upload then returns
immediately with `{"status": "queued", "job_id": ...}` and a worker thread does
the indexing. Poll it:

    curl -s -H "Authorization: Bearer $TOKEN" \
      http://localhost:8000/api/admin/jobs | jq

`enabled` reports the posture, `queued` the live depth, and each row carries
`status` (`queued` / `running` / `complete` / `failed`), `chunks_processed`
against `chunks_total`, and the error when one failed.

**What is NOT deferred.** The injection scan, the quarantine decision, PII
redaction and the caller's trust tier all still run synchronously, before the
job is queued. An upload whose content is withheld is still refused in the
response, never silently accepted and quarantined later. Only chunking,
embedding and the index diff move to the worker.

**The worker is in-process, and that is deliberate.** The vector store is
embedded rather than a server, so a worker in a separate container would be a
second process writing one HNSW index with no cross-process locking - the same
class of loss the grace period under "Stopping safely" exists to avoid. It
would also invalidate only its own copy of the in-memory lexical index, leaving
this process serving a stale BM25 half on every hybrid search. One process
avoids both. The cost is that ingestion scales to one machine.

**Bounds.** One worker thread, so queued documents index serially - the point is
getting the work off the request path, not doing more of it at once.
`ASYNC_JOB_MAX_QUEUED` (default 20) caps how many documents wait at once,
because each holds its extracted text in memory until its turn. Past the cap an
upload answers `503`, and its job row is closed as failed rather than left
looking queued.

**A restart ends in-flight jobs.** They live in this process, so a stop loses
them. At the next boot every row still marked queued or running is failed with
`interrupted by a restart - re-upload to retry`, rather than left claiming
progress forever. Re-uploading is safe and cheap: chunk ids address content, so
the chunks that did land are skipped rather than embedded a second time.

## Backups

Everything stateful lives in `backend/data/` (SQLite databases, the chroma
vector index, ingest state). Two mechanisms:
- On-demand: POST /api/admin/backup (Owner) writes a consistent snapshot
  archive under `backend/data/backups/` - SQLite is snapshotted via the
  backup API, safe against live writers; retention prunes old archives.
- Scheduled: run `scripts/backup_cron.py` INSIDE the container from a host
  cron - `docker compose exec -T backend python /app/scripts/backup_cron.py`.
  It takes the same snapshot as the button and writes `backup-status.json`.
  It runs in-container on purpose: the endpoint is Owner-gated and the only
  credential this platform issues expires in 30 minutes, so a nightly curl
  would work once and 401 every night after.

GET /api/backup-status is an unauthenticated probe that returns 503 when
backups are missing, stale, or failed - point an uptime monitor at it so a
backup job that silently stops running alarms instead of being discovered
during a restore. The probe reads TWO heartbeat files from the data
directory, and neither is written by the backup endpoint itself - your
scheduled job writes them as its success receipts:
- `backup-status.json` - the backup job's heartbeat
- `drill-status.json` - the restore DRILL's heartbeat, so "we take
  backups" and "we have restored one recently" are separately proven
Each is JSON like `{"ok": true, "last_success": "2026-08-21T090000Z"}`;
missing, stale (BACKUP_MAX_AGE_HOURS), or ok=false trips the probe. If you
do not run restore drills yet, write both files from the backup job - and
start running drills.

## Monitoring

- GET /api/health - liveness (also checks Ollama reachability).
- GET /api/health/ready - readiness: DB (critical), Redis and Ollama
  (reported, non-fatal).
- GET /api/status (authed) - the posture surface: which fail-open controls
  are actually on (rate limiting, injection scan mode, PII mode), provider
  config, agent-tool gates.
- GET /metrics - Prometheus counters. Authenticated: a signed-in request
works, and for a scraper set METRICS_TOKEN in the backend environment
and send it as a bearer. A user session cannot serve a scraper - access
tokens expire in 30 minutes and Prometheus cannot refresh one. The
Monitoring tab's downloadable scrape config already carries the right
target port and auth block.
- GET /api/health/detailed (Owner) - disk, DB latency, provider health;
  fires configured alerts on disk pressure and Ollama outages.

## Running the test suite

```
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt          # Windows: .venv\Scripts\pip
cd backend && ../.venv/bin/python -m pytest tests -q           # Windows: ..\.venv\Scripts\python
```
The suite mocks the vector store and embedder - it needs no Ollama, no
network, and no API keys. CI runs the same suite plus a secret scan and a
private-residue guard (see `.github/residue-denylist.txt`) on every push.

## Running an evaluation

POST /api/admin/evals/run: retrieval-only runs are fast and free (recall +
the Knowledge Gaps list); answer-mode runs need a writer model and the
pinned judge (different provider families - the run refuses same-family
pairs). The run refuses to start while the boot ingest is still embedding,
so a fresh deploy measures a whole corpus, never a half-ingested one.
Deeper measurement (A/B arms, noise bands) lives in
`backend/scripts/eval_retrieval.py` - run it inside the container.

Growing the locked holdout: `backend/scripts/author_holdout.py` has an OUTSIDE
model read corpus files and draft questions plus grading keys for them, spread
deterministically across the whole corpus. It writes a JSON array to
/tmp/holdout-questions.json; you review it and merge what survives into
`backend/eval-questions.json`, and the next boot syncs it in. The rule that
makes the cohort worth having: nobody edits the drafted text. A bad item is
DELETED, never fixed - the moment you improve a question you have tuned it, and
a tuned holdout measures nothing the tuned set was not already measuring. The
script costs one API call per sampled file.

## Where things live

- `backend/data/` - all persistent state (the only directory to back up)
- `knowledge/` - the corpus (edit or add files; the watcher live-ingests)
- `docs/` - operator docs, ingested as corpus
- `backend/eval-questions.json` - the eval seed (synced to the DB on boot)
- `.env` - secrets and per-instance posture (never committed)
