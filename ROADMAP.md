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
- **Index maintenance at boot** - automated vector-index parameter
  adoption, orphan cleanup, and a one-shot force-rebuild lever for
  write-corrupted collections (the flush hook and rebuild rationale are
  already in; this automates the recovery).
- **Async ingest workers** - queue-dispatched ingestion for very large
  uploads (the job model and status endpoint shapes are in place).
- **On-demand injection probe** - a live-fire probe script sharing the
  standing injection cohort's SPECS, for red-teaming outside eval runs.
