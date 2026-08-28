# Roadmap

What's ahead for the open core, in rough order of readiness. Commercial
modules and their graduation policy live in [MODULES.md](MODULES.md).

- **Reference web frontend** - the platform is deliberately API-first
  today; a reference chat + admin client is the most-wanted addition.
- **Live-system record generator** - the producer for the `system` trust
  tier: chunks generated from the live database that carry current-state
  authority on status questions. The tier's ranking, labeling, and prompt
  machinery already ship; this fills them.
- **Index maintenance at boot** - automated vector-index parameter
  adoption, orphan cleanup, and a one-shot force-rebuild lever for
  write-corrupted collections (the flush hook and rebuild rationale are
  already in; this automates the recovery).
- **Async ingest workers** - queue-dispatched ingestion for very large
  uploads (the job model and status endpoint shapes are in place).
- **Holdout authoring workflow** - tooling that has an outside model
  author new locked-holdout eval questions, keeping the tuned/holdout
  separation easy to maintain as a corpus grows.
- **On-demand injection probe** - a live-fire probe script sharing the
  standing injection cohort's SPECS, for red-teaming outside eval runs.
