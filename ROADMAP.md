# Roadmap

What's ahead for the open core, in rough order of readiness. Commercial
modules and their graduation policy live in [MODULES.md](MODULES.md).

- **Live-system records for lower tiers** - the boot-time producer now
  fills the `system` trust tier with this instance's own posture, corpus
  and measurement state, at Owner clearance. Still ahead: a variant safe
  for the general floor (which needs a conflict resolved first - the
  non-owner rules forbid recounting internal metrics, while the grounding
  rules say a live record wins), refresh on change rather than only at
  boot, and a content-aware corpus fingerprint so a record's content edit
  is visible to evaluation banding rather than invisible to it.
- **Index parameter adoption** - boot maintenance now cleans orphaned
  segments, heals records whose metadata outlived their vector, and ships
  the one-shot force-rebuild lever. Parameter drift is REPORTED rather than
  adopted automatically: adopting it means dropping and re-adding a healthy
  collection with its only copy in memory, which is not a thing to do
  unattended. Still ahead: a rebuild that stages to disk first, which would
  make automatic adoption safe enough to enable.
- **Distributed ingest** - large uploads can now be queued instead of
  embedded inside the request: the upload returns a job id, an in-process
  worker indexes the document, and `GET /api/admin/jobs` reports progress.
  In-process is as far as this safely goes while the vector store is
  embedded - a worker in a second process is a second writer against one
  index with no cross-process lock, and the lexical index it invalidates
  is its own copy, so the API would serve a stale BM25 half. Still ahead:
  the vector store as a server, which is what would make a broker and a
  worker container a gain rather than distribution bought with index
  integrity.
- ~~**Silent session refresh in the reference client**~~ - SHIPPED
  2026-09-21 (`frontend/src/sessionRefresh.ts`): a 401 on an expired access
  token becomes one single-flight refresh (a Web Lock across tabs) and one
  replay, with its own tests; a refused refresh returns the original 401 so
  the "session expired" banner still means a session was really lost.
- **One numeric-env parser** - a bad number in `.env` should refuse to boot
  (falling back to a default is the silent-discard shape this codebase has
  spent its review history removing), and it should say which variable, what
  value and what the default is. `runtime_config._env_num` does that for the
  seven it owns (the four request bounds joined it 2026-09-21). Dozens more -
  about fifty, counted 2026-09-30 - are parsed with a bare `int(os.getenv(...))`
  or `float(os.getenv(...))` across most backend modules and the routers, and
  they still fail as an unattributed `ValueError` from inside an import chain.
  Mechanical, but an edit that wide wants its own change rather than riding a
  release.
- **Question-set fingerprinting for evaluation banding** - the trust panel
  bands runs sharing a writer, a corpus fingerprint, a judge-instrument era
  and an exam SHAPE, where shape is `n_rest`: a count of non-honesty rows.
  A count is not an identity. Two exams with the same number of questions
  band together whatever those questions are, and moving one question
  between the tuned and holdout cohorts leaves the count untouched while
  changing what correctness, holdout and gap each mean. The fix is a hash
  over question id, content, category, expected source, as_level, holdout
  flag and setup turns, stamped on the run and added to the band key - the
  same move the corpus fingerprint already makes, one axis over. Until then
  a band can silently span two different exams.
- **Refresh-token reuse detection - the larger half shipped 2026-09-10.** A
  replayed rotated token now revokes the whole session family, logged and
  counted, with the same 401 a garbage token gets (`SECURITY.md` states the
  control). What remains is the smaller half: the read, the revoke and the
  mint are still three steps rather than one, so two concurrent refreshes
  from the same client can both observe a live token for an instant. Wants a
  compare-and-revoke with a single-use guarantee.
- **Readiness that covers the retrieval dependencies** - `/api/health/ready`
  treats only the database as critical; the embedding provider, the vector
  store and the configured answering provider are unchecked or advisory. An
  instance therefore reports ready while every retrieval-backed question
  returns a 500. Documented in the README rather than hidden, but the honest
  probe is the better answer: per-dependency status, and a 503 when the
  pieces retrieval actually needs are down.
- **Non-root containers** - both images run as root, which trivy flags as
  DS-0002 and which is worth fixing rather than arguing with. The reason it
  is not a one-line change is the data directory: it arrives as a bind mount
  of a host path, so it keeps that host directory's ownership, and a user
  inside the container has to line up with a uid the template cannot know in
  advance. Getting it right means creating the user, taking ownership of the
  paths the image itself owns, and giving existing v0.1.x deployments an
  upgrade note for the directory their new container would otherwise be
  unable to write. Deferred with that reasoning in `.trivyignore` rather than
  dropped out of the scanner's range, because a suppression nobody can read
  is how a finding stops existing.
- **FastAPI and starlette onto a fixed line** - seven starlette findings sit
  behind the pin at fastapi 0.115.5, and clearing them needs starlette 1.3.1
  or later, which only arrives with a FastAPI major bump. Two of the seven
  are reachable here rather than theoretical: a parsing slowdown on large
  multipart bodies, which the admin upload route accepts, and a Host header
  that goes unvalidated into `request.url.path`, which matters to anyone
  running path-based rules in a proxy in front of this. They are deferred
  rather than accepted, and the distinction is the whole point - an
  acceptance says a finding cannot reach you, while this one says it can and
  the fix has a cost worth naming. That cost is changing the web framework
  underneath everyone who deploys this template, which wants the acceptance
  suite run against it rather than a version number edited to turn a scanner
  green. The ignore list in `.github/workflows/ci.yml` names each finding and
  why it is still there.
- **Widen client test coverage** - the tests that mount the chat client cover
  the stored-row invariant across every stream outcome, which is where the
  defects were, and since 2026-10-02 the paths that touch it or the user's
  identity: the refusals made before storage (409, 413), leaving a
  conversation mid-answer, a render that lands late, and the identity
  transitions' resets (sign-out's credentials, a guest's draft into an
  account, a history read that lands after a switch). Not yet covered: the
  setup wizard, model selection, and the admin panel beyond its save rules
  (those have their cases since 2026-10-03, adminWriteSafety.test.tsx). Each wants the same
  treatment - find the rule, find where the rule becomes an observable, test
  that rather than the symptoms.
- ~~**Alerts that fire on an unwatched box**~~ - SHIPPED 2026-10-02
  (`backend/app/self_check.py`): the disk and Ollama alerts were raised
  inside `GET /api/health/detailed` alone, so they fired only while an Owner
  had the Monitoring tab open - rehearsed 2026-09-29, five minutes with Ollama
  stopped and nobody signed in, nothing sent. The same probes, and the backup
  and drill heartbeats, now run on a timer from boot through the same alerts;
  the runbook's Monitoring section says what the timer cannot see.
- ~~**An update that takes its own copy first**~~ - SHIPPED 2026-10-02
  (`backend/app/db.py`, "A copy before the first one-way change"): the first
  boot after an update encrypts plaintext second-factor seeds and saved
  provider keys in place, and an older version cannot read them afterwards.
  A boot with anything to convert now copies the database first, through
  SQLite's backup API, and holds the conversion when it cannot write the
  copy; the runbook's "Going back" says how to use it. The copy is the
  database alone - copying the whole data directory before an update stays
  the operator's step.
- ~~**Stop that stops the model**~~ - SHIPPED 2026-10-01
  (`backend/app/closing_stream.py`): a client disconnect cancelled the
  streaming response without closing its synchronous generator, so the
  provider stream kept running until garbage collection finalised it. The
  chat route's `ClosingStreamingResponse` now closes the generator once the
  response is over and the provider stream sits in `closing()`, so the close
  reaches the connection; the tests drop a real connection mid-answer and
  fail on the old route.
