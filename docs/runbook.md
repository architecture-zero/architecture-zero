# Runbook - deploying and operating Architecture Zero

This runbook is itself part of the shipped corpus: once the instance is
running, you can ask the assistant these questions directly.

## Deploy on a single machine

Prerequisites: Docker with the compose plugin, and (for local models) an
Ollama install on the host with at least one chat model and the embedding
model pulled (`ollama pull qwen3:8b` and `ollama pull nomic-embed-text`).

1. Clone the repository.
2. `cp .env.example .env` and set JWT_SECRET_KEY to a real secret
   (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
   Auth fails closed on the placeholder - the backend will not boot with
   ENABLE_AUTH=true and the default value.
3. `docker compose up -d --build` (the first build downloads and bakes the
   reranker models into the image).
4. Open http://localhost:8000/api/health - expect status healthy (or
   degraded if Ollama is not up yet; cloud-only deployments can ignore it).
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
   `curl -N -X POST localhost:8000/api/chat -H "Authorization: Bearer <access_token>" -H "Content-Type: application/json" -d "{\"prompt\":\"How do I add my first documents?\",\"use_rag\":true}"`
   Set `use_rag` to true - that is what grounds the answer in the shipped
   corpus with citations; without it the model answers from training
   memory. The answer streams back as server-sent events. The corpus the
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

## Stopping safely

`docker compose stop` (or down). The compose file sets stop_grace_period
to 45s ON PURPOSE: uvicorn drains connections and the shutdown hook
flushes the vector index's unpersisted tail. Do not shorten it - a kill
that beats the flush can lose recently written vectors (the startup
completeness check will re-ingest them, but a graceful stop is free).

## Backups

Everything stateful lives in `backend/data/` (SQLite databases, the chroma
vector index, ingest state). Two mechanisms:
- On-demand: POST /api/admin/backup (Owner) writes a consistent snapshot
  archive under `backend/data/backups/` - SQLite is snapshotted via the
  backup API, safe against live writers; retention prunes old archives.
- Scheduled: run a host cron that calls the endpoint (or archives the data
  directory while the container is stopped).

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
- GET /metrics (authed) - Prometheus counters.
- GET /api/health/detailed (Owner) - disk, DB latency, provider health;
  fires configured alerts on disk pressure and Ollama outages.

## Running the test suite

```
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest tests -q
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

## Where things live

- `backend/data/` - all persistent state (the only directory to back up)
- `knowledge/` - the corpus (edit or add files; the watcher live-ingests)
- `docs/` - operator docs, ingested as corpus
- `backend/eval-questions.json` - the eval seed (synced to the DB on boot)
- `.env` - secrets and per-instance posture (never committed)
