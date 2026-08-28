# Roadmap

What's ahead for the open core, in rough order of readiness. Commercial
modules and their graduation policy live in [MODULES.md](MODULES.md).

- **Reference web frontend** - the platform is deliberately API-first
  today; a reference chat + admin client is the most-wanted addition.
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
