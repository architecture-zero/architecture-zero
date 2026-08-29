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
- **Widen client test coverage** - the tests that mount the chat client cover
  the stored-row invariant across every stream outcome, which is where the
  defects were. Not yet covered: the admin panel, the setup wizard, model
  selection, and the identity transitions (sign-in, sign-out, continue as
  guest) whose state resets are currently verified by hand. Each wants the
  same treatment - find the rule, find where the rule becomes an observable,
  test that rather than the symptoms.
