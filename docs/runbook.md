# Runbook - deploying and operating Architecture Zero

This runbook is itself part of the shipped corpus: once the instance is
running, you can ask the assistant these questions directly.

## Deploy on a single machine

Prerequisites: Docker with the compose plugin, and an Ollama install on the
host with the embedding model pulled (`ollama pull nomic-embed-text`), plus a
chat model if you want local inference (`ollama pull qwen3:8b`). The embedder
is required whichever provider answers chat - only the chat model is swappable
for a cloud API. On a Linux host Ollama binds `127.0.0.1` by default and the
containers cannot reach it there: `sudo systemctl edit ollama`, add
`[Service]` / `Environment="OLLAMA_HOST=0.0.0.0"`, restart it (the
2026-09-16 clean-box rehearsal on Ubuntu 24.04 found the installer's unit
sets no bind address, so the service answered only on loopback until this
override was added; the README carries the same note).

1. Clone the repository.
2. `cp .env.example .env` and set JWT_SECRET_KEY to a real secret
   (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
   Auth fails closed on the placeholder - the backend will not boot on the
   default value, whether or not ENABLE_AUTH is on. The guard runs at import
   and never consults that flag.
3. `docker compose up -d --build` (the first build downloads and bakes the
   reranker models into the image).
   The backend's port is published on loopback only, so the `localhost:8000`
   commands below run on the box itself; from another machine, the same paths
   answer through the frontend's port (`http://<host>:5173/api/health`).
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

    docker compose stop
    sudo cp -a backend/data ../data-before-update-$(date +%Y%m%d-%H%M%S)
    git pull
    docker compose up -d --build

(`sudo` on a Linux host: the container wrote those files as root. The time in
the name is not decoration: `cp -a` into a directory that already exists puts
the copy INSIDE it, so a name that can repeat - a fixed one, or a date alone
on a day you update twice - is right once and wrong the next time.)

Stop first, and copy before you pull. The copy is the only way back - see
"Going back" below - and it is only whole while nothing is writing: the
application database runs in WAL mode (its newest commits sit in the -wal file
until a checkpoint), the vector store's database does not, and the vector
index flushes its tail on a graceful stop. Keep `JWT_SECRET_KEY` as it is in `.env`; it is also the key the secrets
in the database are encrypted under.

On its first boot the new version brings the database up to its own shape,
the startup sync re-ingests the shipped files the update changed and leaves
your own documents alone (content-addressed deltas), and the eval question
set reconciles from the seed file. Nothing else is needed.

`git pull && docker compose up -d --build` on a RUNNING instance also works -
it is the line this section used to give, and the one the 2026-09-29 upgrade
rehearsal ran - but it takes no copy, and while the new image builds the
running instance's file watcher ingests the files the pull just changed with
the OLD code. The rehearsal watched that happen: the new version's first sync
skipped every file, and the index already held the update's text. Between
v0.1.0 and that day the two versions ingest a file the same way, so nothing
was lost by it. A release that changes how a file is chunked or stamped would
leave those files in the old shape until they next change.

### What the first boot after an update changes

Rehearsed 2026-09-29, from v0.1.0 to the main of that day, on a deployment
with a first week in it: four accounts in two departments, one of them with a
second factor, a provider key saved through the settings page, uploads, a
held upload, conversations, and a session left signed in. All of it came
through - the same accounts, roles and departments; the Owner signed in with
the same authenticator and both members with their passwords (the admin's
account was compared, not signed in); the session still refreshing; and the
same answers from the same documents to the same people, including the two
people a document must never reach.

Four things change on disk:

- **Secrets are encrypted at rest.** v0.1.x stored second-factor seeds and
  saved provider keys as written. The first boot encrypts both, under a key
  derived from `JWT_SECRET_KEY`, and says so in the log:
  `mfa seed sweep: encrypted N plaintext seed(s) at rest` and
  `provider-key sweep: encrypted N plaintext secret(s) at rest`.
- **The schema grows.** A `security_state` table (the throttles and the
  second-factor challenge counters, which used to live in memory and now
  survive a restart) and three columns on `audit_log`.
- **Rows whose account is gone are removed**, and the log names what went
  (`fk orphan sweep: ...`). Silent when there are none, which is the usual
  case: v0.1.x deactivates an account and never deletes one.
- **The index gains the help pages.** A `kb_help` collection holding the
  product's own help pages is written on the first boot. v0.1.x ignores it
  if you go back.

A later boot finds nothing left to do and prints none of those lines.

### What changes for anything that calls the API

Since v0.1.x, for a script or a client of your own:

- The routes that create or change durable authority ask for the caller's
  own password in the body, as `current_password`, and answer 400 without it:
  creating an account, changing a role, a permission change that grants
  `manage_users` or `manage_system`, provider settings, registering or
  changing a peer, enrolling a second factor, changing a username, and an
  administrator resetting someone's second factor.
- Sign-in attempts are bounded per address (429), a request over the input
  bound is refused before it is scanned, stored or billed (413), and a second
  message on a conversation that is still answering is refused (409).
- Changing a role: setting the role an account already has answers
  `{"status": "unchanged"}` and writes nothing (it used to reset the account's
  explicit permission list); changing your own role is refused (403); an id
  that names no active account is a 404, with nothing written.

### Going back

An older version cannot run on data a newer version has booted on. The
rehearsal tried it, because it is what anyone would try first: check out
v0.1.0, rebuild, same data directory. It boots and reports healthy, and the
password-only accounts it tried sign in - which is what makes it look like
it worked. The Owner's sign-in fails with a 500 at the authenticator step, and
so would any other account with a second factor: the old code hands the
encrypted seed to the authenticator library as if it were the seed. The saved
provider key is read as its ciphertext and would be sent to the provider as
the key.

The way back is the copy:

    docker compose stop
    sudo rm -rf backend/data
    sudo cp -a ../data-before-update-<the time you took it> backend/data
    git checkout v0.1.0            # the version the copy was taken on
    docker compose up -d --build

The rehearsal ran that too, and every reading matched the one taken before
the update. Whatever was written after the update goes with it. The checkout
is left on the tag, not on a branch: `git checkout main` before the next
update, or `git pull` has nothing to pull into.

If you did not take the copy: since 2026-10-02 the first boot that would
convert anything an older version cannot read takes a copy of the database
first - `backend/data/pre-update/history.db.<UTC time>`, the newest three
kept (`PRE_UPDATE_COPY_DIR`, `PRE_UPDATE_COPIES_KEPT`) - and its log names it.
If it cannot write that copy, it does not convert: the log says
`one-way change HELD`, the rows stay as they were, and both versions can
still read them. That copy is the database alone, and it holds the
second-factor seeds and provider keys as the older version stored them - in
plaintext, the only form that version reads. So it does not stay and does
not travel: no backup carries `pre-update/`, every boot names each copy it
still holds, and a boot deletes a copy older than `PRE_UPDATE_COPY_KEEP_DAYS`
(14). Delete it yourself once the new version is confirmed. To go back with it, stop,
put it in place of `backend/data/history.db` (and remove `history.db-wal` and
`history.db-shm` beside it), check out the older version, and start; the
rest of the data directory is whatever the newer version left.

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
from a failure. The work runs on a worker thread, so a long ingest holds only
its own connection - the rest of the API keeps answering - and uploads write
to the index one at a time.

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

The archive is written beside the data it protects, so a disk that takes one
takes both. Copy archives off the machine.

### Restoring a backup

    docker compose stop
    aside=../data-set-aside-$(date +%Y%m%d-%H%M%S)
    sudo mv backend/data "$aside"
    mkdir backend/data
    sudo tar -xzf "$aside"/backups/az_backup_<timestamp>.tar.gz -C backend/data
    docker compose up -d

An archive unpacks straight into an empty `backend/data/`. The old directory
is set aside rather than deleted: it holds the other archives, and it is the
way back if this archive turns out not to be the one you wanted. Its name
carries the time for the reason the update's copy does: `mv` into a
directory that already exists nests, as `cp -a` does. Boot with
the `JWT_SECRET_KEY` the archive was taken under, or read the next section
first.

An archive taken on an older version restores into a newer one. The first
boot treats it as an update and brings it up the same way - rehearsed
2026-09-29 with a v0.1.0 archive: both encryption sweeps ran, the index
came back whole, the Owner signed in with the same authenticator and both
members with their passwords, and every document answered.

One thing will look wrong and is not: the Backup tab names the archive
BEFORE the one you restored as the last backup. The snapshot is taken before
the backup records itself, so an archive never contains its own receipt.

## Rotating JWT_SECRET_KEY, and restoring under a different one

The MFA TOTP seeds and the provider keys stored through the admin settings
page are Fernet-encrypted at rest under a key derived from JWT_SECRET_KEY
(except in a pre-update copy, for its keep window - Going back, above).
Rotating the secret, or restoring a backup into an instance that boots with a
different secret, makes every one of those rows unreadable at once. The app
fails CLOSED: an account with two-factor enrolled is refused at sign-in with a
403 that names the fix, rather than crashing or being let in on the password
alone. Access tokens die with the rotation (they are signed with the secret);
refresh tokens are random values hashed in the database and SURVIVE it - an
MFA account's refresh is refused by the stranding check, a password-only
account keeps refreshing. To end every session at a rotation, revoke per user
(sign out everywhere) or set `revoked=1` on the `refresh_tokens` table and
clear the `az:rt:*` keys if Redis is on.

Re-key the rows OFFLINE, with the backend stopped, before booting under the
new secret:

    docker compose stop backend
    docker compose run --rm --no-deps backend python scripts/rekey_at_rest.py --db /app/data/history.db --dry-run
    docker compose run --rm --no-deps backend python scripts/rekey_at_rest.py --db /app/data/history.db
    # set the new JWT_SECRET_KEY in .env, then
    docker compose up -d backend

The script prompts for the previous and the new secret (or reads
REKEY_OLD_SECRET / REKEY_NEW_SECRET from the environment when there is no
terminal) - never pass them as arguments. Exit code 2 means at least one row
could not be read under either secret; it is listed and left untouched. For
an account whose seed is truly lost, an Owner can reset its MFA
(POST /api/admin/users/{id}/mfa-reset) and the person enrolls again. On a
deployment with `REQUIRE_MFA=true` that reset account cannot sign in to
re-enroll (login refuses accounts with no TOTP), so use the re-key, or set
`REQUIRE_MFA=false` for the re-enrollment. The same reset is the path for a
person who lost a working authenticator: since 2026-10-02 the self-service
re-key (POST /api/auth/mfa/setup with `rekey: true`) also takes a current
code from the authenticator being replaced, so without the device it cannot
be done from the account itself.
GET /api/auth/me reports `mfa_secret_unreadable` for the signed-in account,
and the admin roster carries the same field per user.

## Monitoring

- GET /api/health - liveness; its Ollama word is the self-check timer's
  last pass - the route calls nothing itself.
- GET /api/health/ready - readiness: DB (critical), the retrieval lane
  (critical when this instance serves retrieval), Redis and Ollama (the
  timer's last pass, reported, non-fatal).
- GET /api/status (authed) - the posture surface: which fail-open controls
  are actually on (rate limiting, injection scan mode, PII mode at ingest
  and the output-side PII mode on answers with the types it masks), provider
  config, agent-tool gates.
- GET /metrics - Prometheus counters. Authenticated: a signed-in request
holding `view_analytics` (the Owner, or the Admin preset) works, and for a
scraper set METRICS_TOKEN in the backend environment
and send it as a bearer. A user session cannot serve a scraper - access
tokens expire in 30 minutes and Prometheus cannot refresh one. The
Monitoring tab's downloadable scrape config already carries the right
target port and auth block. The block reads the token from a file
(`credentials_file: /etc/prometheus/metrics_token`): write the same
METRICS_TOKEN value there, alone on one line, readable only by the
Prometheus user. The token itself is never part of the download.
- GET /api/health/detailed (Owner) - disk, DB latency, provider health,
  and the retrieval lane's last check with its reason; fires configured
  alerts on disk pressure and Ollama outages.

**The instance checks itself (since 2026-10-02).** Every
`SELF_CHECK_INTERVAL_SECONDS` (default 300; `0` turns it off) a timer inside
the backend runs the same disk and Ollama probes that route runs, and reads
the backup job's and the restore drill's heartbeats the way
`/api/backup-status` does, raising through the same channels and the same
per-check cooldown (`ALERT_COOLDOWN_SECONDS`, an hour by default). So
`ALERT_WEBHOOK_URL` or the SMTP settings deliver a disk, Ollama or backup
alert with nobody signed in. Before the timer the alerts fired only when an
Owner read the detailed route - the Monitoring tab's load and its 30-second
refresh - and a 2026-09-29 rehearsal sent nothing for five minutes with
Ollama stopped. `SELF_CHECK_BACKUP=false` leaves the heartbeats out, for a
deployment that backs up some other way. The timer lives inside the process
it watches, so it cannot report that process being down: point an outside
monitor at the routes that need no session, and each says a different
amount:

- `/api/backup-status` answers 503 when the backup job's heartbeat or the
  restore drill's is missing, stale or failed (Backups, above) - so it
  answers 503 from the first day until both are being written.
- `/api/health/ready` answers 503 when the database is down, or until a
  recent check of the retrieval lane found it working - or found it not
  needed here: it failed, has not finished its first check (that runs at
  boot), has not been checked lately, or the timer is off (below).
- `/api/health` answers 200 whatever it finds and puts `"status":
  "degraded"` in the body when Ollama is unreachable - a monitor has to read
  the body to catch that one.
- Disk pressure reaches no route that needs no session: the detailed route
  shows it, and the self-check's alert raises it. If alerting is off, watch
  the disk from the host.

**The retrieval lane (since 2026-10-07).** Retrieval gets its embeddings from
`EMBED_BASE`, a separate service from the chat provider, so a healthy
database and a healthy cloud model used to read ready while every question
that needs the knowledge base failed. The same timer now checks the lane:
it embeds one short sentence through the same call a question uses, then
searches the vector store with that vector - read-only, never a write. A
missing `EMBED_MODEL` fails there (the service refuses it), and so does a
different model, whose vector is the wrong width for the index. A failed
check raises an alert like the others, and `/api/health/ready` answers 503
until a check passes; its body shows `rag` as `ok`, `error`, `stale`,
`pending`, `not_required`, `off` or `skipped`, and the Owner's detailed route gives the
reason (`embed_unreachable`, `embed_model_missing`, `embed_refused`,
`embed_malformed`, `vector_store_unreadable`, `vector_search_failed`,
`probe_crashed`, and `vector_store_empty` on a deployment that declares
its corpus never empty - below).

- The lane is required when the instance has documents of its own to serve
  (the product help pages do not count) or `RAG_ONLY_MODE` is on. Otherwise
  it reads `not_required` and asks nothing of the embed service. A
  deployment whose corpus is never legitimately empty can pass
  `corpus_expected=True` to the probe in its startup hook (`app/main.py`),
  and an empty store - a volume not mounted, an index wiped - then reads
  `error` instead of `not_required`.
- The readiness route never runs the check itself - it reads the last one,
  and only a check that found the lane `ok` or `not_required` is ready.
  Everything else fails it (since 2026-10-07; `pending` and `off` used to
  pass, so a broken lane read ready from boot): `error`; `stale`, a check
  older than two intervals plus a minute - 660 seconds at the default
  interval, so a failure shows at the next check and a check that never
  finishes shows once the last one is that old; `pending`, no check finished
  since boot; `skipped`, no check wired; `off`, the timer disabled.
- The first lane check runs at boot, so readiness is green within seconds of
  a start whose lane works. If it fails it is retried every 15 seconds until
  it passes or the first regular check is due - without an alert, because a
  cold start whose embed service is still loading is not an incident; the
  regular checks alert as before, and readiness shows the failure throughout.
- `SELF_CHECK_INTERVAL_SECONDS=0` turns the lane check off with the rest of
  the timer; readiness then shows `off` - for the lane, Redis and Ollama
  alike - and answers 503, because nothing proves the lane. Keep the timer
  on wherever an outside monitor reads readiness - and wherever a load
  balancer or orchestrator gates traffic on this route, which would take a
  timer-off instance out of rotation.

## Running the test suite

```
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt          # Windows: .venv\Scripts\pip
cd backend && ../.venv/bin/python -m pytest tests -q           # Windows: ..\.venv\Scripts\python
```
The suite mocks the vector store and embedder - it needs no Ollama, no
network, and no API keys. CI runs the same suite plus a secret scan, a
private-residue guard (see `.github/residue-denylist.txt`), and dependency
and static-analysis scanning (pip-audit, bandit, trivy) on every push. The
scanners and the suite also run on a daily schedule; the secret scan and the
residue guard do not, because they only have something to say about a diff.

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
