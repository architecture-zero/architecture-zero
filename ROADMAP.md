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
- **Silent session refresh in the reference client** - the API mints and
  rotates refresh tokens correctly and `POST /api/auth/refresh` works, but the
  shipped client stores the refresh token without ever spending it, so a
  session ends when its 30-minute access token expires. Keying a silent
  refresh off a 401 changes what every request does on failure - including
  mid-stream - so it wants its own tests rather than a release-eve patch. See
  Known limitations in the README.
- **One numeric-env parser** - a bad number in `.env` should refuse to boot
  (falling back to a default is the silent-discard shape this codebase has
  spent its review history removing), and it should say which variable, what
  value and what the default is. `runtime_config._env_num` does that for the
  three it owns. Roughly two dozen more are parsed with a bare `int(os.getenv(
  ...))` across alerting, jobs, jwt_auth, peers, providers, rag_config and
  main, and they still fail as an unattributed `ValueError` from inside an
  import chain. Mechanical, but a ten-module edit wants its own change rather
  than riding a release.
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
- **Refresh-token reuse detection** - rotation is correct (opaque token,
  hash-at-rest, revoke-then-mint, Redis with a DB fallback) but the read,
  the revoke and the mint are three steps rather than one, so two concurrent
  refreshes can both observe a live token. The race is the smaller half. The
  larger one is that a replayed token produces no signal at all: whoever
  presents it second simply gets a 401 and signs in again, which is exactly
  what a legitimate user does after a thief wins the race. Rotation without
  reuse detection cannot tell those apart. Wants a compare-and-revoke with a
  single-use guarantee, plus family revocation on a detected replay.
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
  defects were. Not yet covered: the admin panel, the setup wizard, model
  selection, and the identity transitions (sign-in, sign-out, continue as
  guest) whose state resets are currently verified by hand. Each wants the
  same treatment - find the rule, find where the rule becomes an observable,
  test that rather than the symptoms.
