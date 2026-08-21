# The security model, in plain terms

## The one-sentence version

Every layer assumes the content flowing through it might be hostile, every
control fails closed, and anything fail-open gets a status surface so "off"
is visible instead of silent.

## The ingestion injection gate

The moment third-party content enters a RAG corpus, the attack channel is
the ANSWER: a poisoned document can carry instructions the model might
follow ("ignore your rules", "send the conversation to this URL"). So every
document is scanned AT INGESTION for instruction overrides, role hijacks,
exfiltration patterns, hidden Unicode text, and agent-directed commands.
What happens next depends on provenance: untrusted content with a hot
finding is QUARANTINED - withheld from the corpus entirely, held for owner
review. The owner's own curated content is only TAGGED, never withheld,
because a corpus legitimately quotes attack strings (these very docs do).
Every ingestion path shares one gate choke point, so a new ingestion
surface inherits the scan by construction.

## Quarantine review and release

Quarantined content never touched the index - its full text waits in a
review queue (GET /api/admin/kb/quarantine) with the exact findings listed.
Releasing is an Owner-only trust decision: the document re-ingests with
the block waived but the injection tag preserved, so it stays visibly
flagged and its content is still handled as data, never instructions.
Deleting discards it. If the same source later re-ingests normally, stale
held rows are marked superseded rather than silently lingering.

## Retrieved content is data, not instructions

The answer-layer half of the injection defense. Every retrieved chunk
arrives in the prompt inside a labeled block that opens with an explicit
contract: instructions found inside retrieved content are content to
report, never directives to follow. Provenance labels rank authority -
live system records and the owner's documents outrank peer and third-party
content, and untrusted content can never unlock restricted material or
redefine who the user is. The assistant is also forbidden from emitting
markdown images or links built from context data, which closes the classic
render-time exfiltration channel.

## Access tiers enforced three times

A clearance level guards content at every surface that could serve it:
retrieval drops departments above the caller's level (including
query-routed ones), the assistant's file tools refuse reads above level
(and hide even the NAMES of higher-tier files in listings and searches),
and the answer layer itself carries a non-owner rule that refuses to
recount internal operational history even if fragments of it leaked into
general-floor context. Three surfaces, one shared clearance map - they
cannot drift apart because they read the same configuration.

## Authentication hardening

Password logins carry per-account lockout after repeated failures. TOTP
two-factor is built in, and REQUIRE_MFA=true refuses password logins for
un-enrolled accounts - checked AFTER password verification, deliberately,
so the refusal cannot be used to probe which accounts exist. The first-run
setup endpoint self-disables once an Owner exists, and the last active
Owner can never be deactivated or demoted - both guards close the
account-takeover path through an unconfigured or orphaned instance.

## Route-level authorization, tested two-sided

Every API route carries its own auth dependency, independent of the
middleware - defense in depth, so flipping the middleware off never exposes
an endpoint. The test suite sweeps the live route table both ways: a new
route without auth fails the build, and so does a stale entry on the
explicit public allowlist, where every public route documents its own
compensating gate.

## Guardrails that live in code, not config

The non-negotiable refusal rules (credentials and secrets, compensation
figures, instruction-override attempts) are compiled into the backend, not
stored in the editable system prompt - an admin edit can never drop them.
The credentials rule is deliberately strict: while refusing, the assistant
will not name where secrets live, their rotation status, or offer to
summarize them, because narrating the neighborhood of a secret is itself a
leak.

## Peer and boundary scanning

Content from federated peers arrives at chat time and never passes the
ingestion gate - so it gets the same injection scan at the chat boundary.
A peer chunk with a hot finding is dropped from that answer and logged;
milder findings ride along visibly tagged. Peer knowledge is reference
material, labeled EXTERNAL, and never outranks your own corpus.

## Fail-open controls get positive signals

A control that fails open (rate limiting, injection scanning, the
reranker) is silent when broken - so each one reports its live state on a
status surface (/api/status, the rerank status endpoint) where "on" is a
readable fact, not an assumption. The backup status endpoint inverts the
pattern: it returns an error status when backups are missing or stale, so
a backup job that silently stops running trips monitoring instead of
being discovered during a restore.
