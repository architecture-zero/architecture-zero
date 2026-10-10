# Troubleshooting Architecture Zero

## The assistant says something is "not on record" but the document exists

Three causes, in order of likelihood. First: is RAG actually on for this
conversation? With retrieval off the assistant is told to say so rather
than fake a lookup - check the RAG toggle. Second: retrieval found the
document but below the similarity threshold - lower
rag_similarity_threshold in Settings slightly, or ask with wording closer
to the document's own vocabulary. Third: a genuine retrieval gap - run a
retrieval-only evaluation and check the Knowledge Gaps list, which names
exactly which expected documents retrieval failed to surface and what came
back instead of them.

## "Login required - this instance is private"

The instance is working as designed: it is private by default. Sign in, or
if you intend anonymous access, enable guest mode BOTH ways - the
ALLOW_GUEST_MODE=true host environment variable AND the switch on the admin
panel's Guest Access tab. Either one alone keeps the instance closed.

## "Guest limit reached (N messages). Sign in to continue chatting."

A per-conversation cap, not an outage. Guest sessions are limited to
GUEST_MAX_TURNS user messages. Sign in to keep going, or raise the limit in
the host environment; 0 removes the cap. This one counts messages in the
current conversation, so a fresh chat starts over.

## "The demo is seeing high demand right now - please try again a little later"

The instance's global daily guest budget is spent. It is a volume backstop
rather than a per-IP rate limit: DEMO_DAILY_GUEST_LIMIT caps total guest
requests per UTC day across ALL callers, because per-IP limits do not stop
distributed or IP-rotating traffic. It counts requests, not tokens, so it
bounds how many guest turns land in a day and not how large any one of them is.
Signed-in users are never counted against it, so signing in is the immediate
way through. The counter resets at UTC midnight; raise the limit or set it to 0
to switch the cap off. It counts in Redis when Redis is reachable and in the
instance's own database otherwise (the `security_state` table, since
2026-09-11), so on the default single-container setup a restart no longer
clears it - the day's count survives a redeploy.

## "Message too long: this request carries N characters and the limit is M"

A per-request input bound, not an outage. The count is your message PLUS the
conversation the client sends back with it, because that is what reaches the
model provider. Guests are bounded by GUEST_MAX_INPUT_CHARS (24,000 by
default), signed-in users by CHAT_MAX_INPUT_CHARS (200,000). Shorten the
message or start a new chat; an operator raises the bound in the host
environment, and 0 removes it. Nothing was stored or sent to the model, so
the web client shows this line under the conversation and puts your message
back in the box.

## "This conversation is too long to send (N messages; the limit is M)"

The same bound counted in messages: CHAT_MAX_HISTORY_MESSAGES (200 by default).
Start a new chat. The stored conversation is untouched - the bound is on what
one request may carry, not on what History keeps.

## "Request refused: session_id carries N characters and the limit is M"

A chat request names its session, and may name a model. Neither is part of
the conversation, so the bounds above do not count them; each is held to the
width of the column it is stored in (255 characters for the session id, 100
for the model name). No client that ships with this project sends a value
that long - this is a hand-written or scripted request.

## "Request body too large: N bytes, and this route accepts up to M"

The byte ceiling on a request's body, checked before anything parses it:
MAX_JSON_BODY_BYTES (2 MB by default) on every route, and the upload size
plus room for its envelope on the two routes that take a document. It is
refused on the headers when the request declares its length, and as it
crosses the ceiling when it does not. An operator raises the figure in the
host environment; 0 switches the default ceiling off.

## "Session expired - sign in again"

Your access token expired and the presented token was invalid - this is the
normal refresh signal, and the app usually refreshes silently. Seeing it
repeatedly means the refresh token also expired (sign in again) or the
server's JWT secret changed (every session invalidates when it rotates).

## My upload was withheld: "Injection-shaped content withheld"

The ingestion gate scanned the document and found content shaped like an
attack on the assistant - instruction overrides ("ignore all previous
instructions"), hidden text, exfiltration directives, or similar - in a
document from an untrusted source. Nothing was indexed. Review it via the
quarantine queue (GET /api/admin/kb/quarantine): the findings list says
exactly what fired. If the document is legitimate, release it (POST
/api/admin/kb/quarantine/{id}/release) - it re-ingests with the block
waived, stays labeled untrusted at retrieval, and the assistant still
treats its content as data, never instructions. Only the Owner can
release.

## "MFA is required on this instance and this account has no TOTP enrolled"

The operator set REQUIRE_MFA=true but this account never enrolled an
authenticator. From another signed-in session (or after an admin resets
the account's MFA), enroll via MFA setup and retry. Operators: always have
every password account enroll BEFORE flipping REQUIRE_MFA.

## "Invalid username or password" when the password is definitely right

The likeliest cause is the account lockout, and it does not announce itself.
After too many failed password attempts the account locks for the lockout
window, and during that window every sign-in attempt answers "Invalid
username or password" - the same answer a wrong password and an unregistered
username get. That is deliberate: an answer that said "locked" would confirm
to an anonymous caller that the username is real, which is how account lists
get harvested. The cost is this confusing moment for a real user.

How to tell it apart, as an operator: the server records the refusal even
though the caller is not told. Look for `auth_login_refused_locked` in the
application log (it carries the username and the minutes remaining), and for
a `login_locked` row in the security events if your deployment stores them.

The remedy is the same as before: wait out the window, or an admin can unlock
immediately (POST /api/admin/users/{id}/unlock). If the real user was not
failing sign-ins, treat the lock as someone guessing at that account's
password and review the audit log.

## Answers are slow

Almost always the reranker: scoring the candidate pool with a
cross-encoder on a small CPU takes seconds per answer. Check the rerank
status surface (GET /api/admin/kb/rerank-status) - it shows which provider
actually served and a live self-test. Options, fastest first: point
rerank_provider=remote-http at a GPU box running a scoring endpoint; use
hosted-api (Cohere/Voyage - only if the operator set the
RERANK_HOSTED_ALLOWED host latch, since it sends chunk text to the
vendor); or disable reranking (rerank_enabled=false) and accept
retriever-order results. All of these are live config flips via PATCH
/api/admin/config - no restart needed; only the hosted latch itself is
host-env-only by design.

## Rerank status reports an error, or answers serve without reranking

The scoring model could not load (first-run download blocked, missing
model cache) or the configured remote endpoint is unreachable. The system
degrades gracefully - answers still flow using the retriever's own order -
but ranking quality drops silently, which is why the status surface
exists (and why per-answer audit receipts record which provider actually
served, including "none"). Fix the named error, or flip rerank_provider to
a working leg. A non-local provider that fails falls back to the local
encoder automatically before giving up.

## The evaluation refused to run: "Startup ingest is still re-embedding"

The boot-time corpus sync is still running, and an evaluation started now
would measure a half-ingested index and produce a plausible-looking wrong
number. Wait for the startup_sync_done lines in the backend logs, then
retry. This guard exists because half-corpus numbers look real.

## The evaluation refused to run: "same provider family - self-graded"

The answer model and the judge model resolve to the same provider family,
and a judge grading its own lab's writer biases every score. Change
eval_answer_model or eval_judge_model in admin config so they differ, or
pass allow_same_family=true only if you deliberately accept a self-graded
run.

## "I could not produce an answer this turn"

The model returned empty output twice in a row (the system retries once
automatically before saying this). Usually a provider-side hiccup - resend
the message. If it persists on a local model, the model may be failing to
load or out of memory on the Ollama host; check Ollama's logs and
/api/health.

## Chat answers "I can only answer questions based on the documents..."

The instance runs in RAG-only mode (RAG_ONLY_MODE=true) and retrieval
found nothing above the similarity threshold for this question. That
refusal is the designed behavior for corpus-bound deployments: no
retrieval, no answer. Ask about corpus content, add the missing documents,
or lower the threshold.

## Health says "degraded" / Ollama unreachable

/api/health reports the self-check timer's last call to the Ollama base
URL (the first one interval after boot, then every
SELF_CHECK_INTERVAL_SECONDS) - it calls nothing itself. Degraded means that
call failed, and it keeps reading degraded after a fix until the next pass
(an Owner opening the Monitoring tab checks again at once). The usual
causes: the Ollama server is down, the OLLAMA_BASE address is wrong for your
network layout (from inside a container, localhost is the container - use
host.docker.internal or the host's address), a firewall blocks it, or - the
usual cause on a Linux host - Ollama is listening on 127.0.0.1 only, which
the containers cannot reach even with the right address. Set
OLLAMA_HOST=0.0.0.0 on the Ollama service (`sudo systemctl edit ollama`)
and restart it; `ss -ltn | grep 11434` should then show `*:11434`, not
`127.0.0.1:11434`.
Cloud-only deployments can ignore Ollama health if no local models are
used.

## Readiness answers 503 with "rag": "error", "stale", "pending" or "off"

The retrieval lane failed its last check, or has not been checked lately.
Retrieval gets its embeddings from EMBED_BASE - a different service from the
chat model - so the chat can be healthy while every question that needs your
documents fails. The Owner's Monitoring view (/api/health/detailed) names the
reason:

- `embed_unreachable` - nothing answered at EMBED_BASE. The same address
  rules as Ollama above apply: from inside a container, use
  host.docker.internal or the host's address, and the service must listen
  beyond 127.0.0.1.
- `embed_model_missing` - the service answered but does not have
  EMBED_MODEL (default nomic-embed-text). Pull it there: `ollama pull
  nomic-embed-text`.
- `embed_refused` / `embed_malformed` - the service answered with an error
  or without a vector; its own log says why.
- `vector_search_failed` - the vector came back but an index would not
  search with it. Every collection is searched, at the k a question asks,
  and the backend log's `probe_retrieval_lane: the search failed on` line
  names each one that failed and why. Every collection failing usually
  means EMBED_MODEL changed after the documents were indexed: the new
  model's vectors are a different width. Set it back, or re-ingest under the
  new one. One collection failing while the others search is that index:
  rebuild it with the runbook's force-rebuild lever.
- `vector_store_unreadable` - the vector store itself could not be read;
  see the next section.
- `vector_store_empty` - the store holds no documents while KNOWLEDGE_DIR
  holds files to serve (or the deployment declared its corpus is never
  empty): check that the data volume is mounted where CHROMA_PATH points,
  and read the backend log's `startup_sync_errors` lines - a failing embed
  service leaves the store empty too. Then re-ingest. On a first boot it
  shows until the startup sync has indexed the first document.
- `stale` - no check has finished for more than two intervals of
  SELF_CHECK_INTERVAL_SECONDS plus a minute (660 seconds at the default).
  The backend log's `self_check` lines show whether the timer is running.
- `pending` - the first check since boot has not finished. It runs at boot
  and is retried every 15 seconds while it fails; pending for minutes means
  the check itself is hanging, usually on the embed call (see
  `embed_unreachable`).
- `off` - SELF_CHECK_INTERVAL_SECONDS is 0, so nothing checks the lane and
  readiness cannot vouch for it. Set the interval back (default 300).
- `skipped` - no lane check was wired: the backend's startup did not finish.

The check runs every SELF_CHECK_INTERVAL_SECONDS (default 300), so after a
fix readiness turns green within one interval; a restart starts it over at
`pending`, and the boot check turns it green within seconds if the lane
works.

## Readiness answers 503 with "crash_loop": "looping (...)"

The backend has booted more than BOOT_LOOP_THRESHOLD times (default 4) on
the same build within BOOT_LOOP_WINDOW_SECONDS (default an hour). Usually it
is crashing and Docker keeps restarting it: a climbing
`docker inspect -f '{{.RestartCount}}' az_backend` confirms it, and
`docker compose logs backend` shows what ended each run. Fix that, and the
check clears once the boots age out of the window. If you restarted or
rebuilt it that many times yourself, wait out the window - and build with
GIT_SHA so each deploy counts as a new build (docs/runbook.md, Monitoring).

## Readiness shows "crash_loop": "unrecorded"

This boot's stamp could not be written to `boot-history.json`: the data
directory (BOOT_HISTORY_DIR, default BACKUP_STATUS_DIR, default /app/data)
is missing, read-only or full. Readiness still passes, but this process
cannot count its own restarts, so a crash loop would go unseen. The boot's
`boot_unrecorded` log line names the directory. Fix the volume's
permissions or free space, then restart once; the next boot reads `ok`.

## Vectors disappeared after a crash or power loss

The vector index persists on a write threshold, not on close - a hard kill
can lose vectors written since the last flush while the document metadata
survives. A graceful stop flushes automatically. After a hard kill, the
startup sync detects sources whose indexed chunk count no longer matches
the file and re-ingests exactly the missing chunks. If a collection
refuses searches entirely after a crash, delete and re-ingest its sources
- the content-addressed ingest rebuilds only what is missing.

## Building the frontend fails: "Cannot find module @rollup/rollup-win32-x64-msvc"

The printed message says npm has an optional-dependency bug and tells you to
delete `node_modules` and `package-lock.json` and reinstall. On Windows that
advice is usually wrong, and following it changes nothing - the module is
already there and already correct.

Check what is really happening:

    node -e "require('@rollup/rollup-win32-x64-msvc')"

If that answers `An Application Control policy has blocked this file`, the
operating system is refusing to load the binary. Smart App Control - on by
default on many Windows 11 installs - and corporate WDAC policies block
unsigned native code, and npm modules ship unsigned as a matter of course.
`npm run dev`, `npm test` and `npm run build` all fail this way; `npm run
type-check` still works, because TypeScript is pure JavaScript.

Do not turn Smart App Control off to fix this. It cannot be turned back on
without reinstalling Windows, and it is doing its job - it has found no fault
with the file, it simply has no basis to trust it. Build in a container
instead, which is both unaffected and better isolated than running the same
code directly on your machine:

    cd frontend
    docker run --rm -v "$PWD:/app" -v /app/node_modules -w /app node:24-alpine \
      sh -c "npm ci && npm run build"

Deployment is not affected at all: `docker compose up --build` already builds
the client inside Linux.

## Uploads rejected: "File too large" or "Unsupported file type"

The upload cap defaults to 50 MB (MAX_UPLOAD_MB). A file far over the cap is
refused before it is read, with "Request body too large" (above). The two
routes that take a document ask who is calling, and whether their account
holds manage_kb, before they read anything, whatever ENABLE_AUTH is set to:
an upload answered "Not authenticated" carried no session, and one answered
"Permission required: manage_kb" came from an account without that
permission (the Owner and the Admin preset hold it). Supported types: md,
txt, pdf, docx, py, js, ts, json, yaml. "No text could be extracted" on a
PDF usually means a scanned/image-only PDF - run OCR first, the platform
ingests text, not images.

## I cannot delete or demote a user

These protections fire here by design: you cannot deactivate yourself or
change your own role (another Owner does it), and the LAST active Owner can
never be deactivated or demoted - nothing could administer the deployment
after it, and the first-run setup would not take a new Owner either: since
2026-10-07 a claimed deployment stays claimed (a durable claim marker is
written with the first Owner). Create a second Owner first if you are
rotating the account. Setting a user to the role they already have changes
nothing: it answers "unchanged" and leaves their explicit permissions alone.
