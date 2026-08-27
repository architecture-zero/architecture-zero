# Security Policy

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting:**
[Report a vulnerability](https://github.com/architecture-zero/architecture-zero/security/advisories/new)

That channel is enabled and monitored. Please use it rather than a public issue,
so a fix can land before the details are public.

If you would rather not use GitHub, open a public issue containing only "I have
a security report, please open a channel" and nothing else, and one will be
opened for you.

## What to expect

This is maintained by one person, so here is the honest version rather than an
SLA that would not survive contact with a busy week:

- **Acknowledgement:** within a few days.
- **Assessment:** you will get a straight answer about whether it is real, what
  the actual blast radius is, and whether it is being fixed. If a report turns
  out not to be a vulnerability, you will get the reasoning, not a brush-off.
- **Fix and disclosure:** fixes land with a commit message that says plainly
  what was wrong. Credit is given unless you ask otherwise.

## Scope

In scope: anything in this repository - the FastAPI backend, the retrieval and
ingestion path, the agent tool surface, auth/authorization, and the deployment
manifests.

Particularly interesting, because these are where the interesting failures live:

- **Authorization that holds on one surface but not another.** Access is enforced
  at retrieval, at the agent's file tools, and at the answering endpoint. A path
  that reaches content through one of those while bypassing another is exactly
  the class worth reporting.
- **Anything that makes a control report success without acting.** Several fixes
  in this repo's history are precisely that: a write path that answered 200 and
  discarded the write, an editor that saved a row the server never read. A
  control that lies is worse than a missing one, because it stops the operator
  looking.
- **Ingest-time content that changes model behavior at answer time**
  (indirect prompt injection through the corpus).
- **Anything reachable before an instance is claimed.** `POST /api/auth/setup` is
  open until the first owner exists; that window is deliberately narrow and
  deliberately documented.

Out of scope: findings against a deployment's own configuration choices
(an operator opting into `CORS_ORIGIN=*`, disabling auth for local development,
or exposing a database directly) rather than against this code.

## Deploying this safely

Two settings matter more than the rest, and both default to the safe option:

- `ENABLE_AUTH=true` for anything reachable from a network.
- Leave `CORS_ORIGIN` at a specific origin. Setting it to `*` also disables the
  server-side origin check, so it is more permissive than it looks.

`ENABLE_AGENT_TOOLS` is off by default and should stay off unless you have read
what the tool surface does. The shell tool is a real shell.

## A note on what "secure" means here

This project's claim is that trust should be measured rather than asserted, and
that applies to its own security posture. The repository history contains fixes
for defects the maintainer found, defects outside reviewers found, and at least
one case where an outside review's assessment was wrong in the reassuring
direction. Those are all written down on purpose.

So: treat the controls here as implemented and tested, not as audited. If you
find something, the response will be to fix it and say what it was.
