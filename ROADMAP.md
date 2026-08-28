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
- **Async ingest workers** - queue-dispatched ingestion for very large
  uploads (the job model and status endpoint shapes are in place).
