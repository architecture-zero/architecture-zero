# Contributing

This is maintained by one person, so here is the honest version rather than a
process that would not survive a busy week.

**Small fixes and clear bugs are welcome.** Open an issue or a PR. If it is
obvious and tested, it will probably land.

**Before building something large, open a discussion first.** Not because the
idea needs approval, but because the project has opinions about seams (see
[MODULES.md](MODULES.md)) and finding out afterwards that a feature belongs in a
different layer wastes your evening, not mine.

**Questions belong in
[Q&A](https://github.com/orgs/architecture-zero/discussions), not issues.** A
running instance can also answer questions about itself, which is often faster.

**Security vulnerabilities do not go in issues, PRs, or discussions.** Use
[private reporting](https://github.com/architecture-zero/architecture-zero/security/advisories/new).
[SECURITY.md](SECURITY.md) says what to expect and which failure classes are
most worth reporting.

## Running it

Prerequisites: Docker with the compose plugin, and [Ollama](https://ollama.com)
on the host with the embedding model pulled.

```bash
ollama pull nomic-embed-text          # required whichever provider answers chat
ollama pull qwen3:8b                  # optional, for local inference

git clone https://github.com/architecture-zero/architecture-zero
cd architecture-zero
cp .env.example .env                  # set JWT_SECRET_KEY - it refuses to boot on the placeholder
docker compose up -d --build
```

The backend is on `:8000`, the reference client on `:5173`. The claim code for
first-owner setup is printed to the backend logs at boot.

## Running the tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest tests -q
```

```bash
cd frontend
npm ci && npm run test && npx tsc --noEmit && npm run build
```

One thing that will confuse you if nobody says it: chromadb's native stack can
crash the **interpreter** at teardown on Linux - SIGSEGV on 3.12, SIGABRT on
3.11 - *after* the session completed and every test passed. A fully green
summary, then exit 139 or 134. CI treats the junit report as the truth and
accepts a teardown crash only when that report exists and shows zero failures
and zero errors. If you see it locally, check the report before you start
debugging your change.

## What CI runs

Five jobs, all of which must pass:

| Job | What it does |
|---|---|
| private-residue guard | Greps the tree against `.github/residue-denylist.txt`. This template is derived from private deployments, and no term from that lineage may appear in it. |
| gitleaks | Full-history secret scan. |
| test suite | pytest, with the junit rule above. |
| frontend | Type-check, tests, build. |
| docker image builds | Both images. |

## The bar for a change

These are the standards the codebase actually holds itself to. They are not
bureaucracy; each one is here because something got past its absence.

**A test that cannot fail is not a test.** If you add one, verify it by breaking
the thing it guards and confirming *that* test goes red. Several tests in this
repo were written, passed, and were later found incapable of failing. One of
them was a guard written to prevent exactly the class it failed to catch.

**Run it before you call it done.** The last three defects found before and
after `v0.1.0` came from executing the product, not from reading it - a default
that only worked on localhost, a proxy stripping a port from a forwarded header,
and a log field silently overwriting log severity that 596 passing tests were
blind to. Reviews read code. Running tests deployment. They find different bugs.

**Say why in the code, not just what.** This codebase carries long comments
explaining the reasoning behind non-obvious choices, including the defects that
motivated them. That is deliberate. If you change something subtle, leave the
next reader the reason.

**Do not silently narrow a guarantee.** If a change makes a stated guarantee
weaker - a control that fails open, a check that stops covering a path - say so
in the PR. A quiet narrowing is the failure mode this project spends most of its
review effort on.

**Route-level authorization is pinned by a two-sided test.** Adding a route
means the sweep in `backend/tests/test_route_authz_wiring.py` will fail until it
knows about it, including at what privilege level. That is intentional: guard
*removal* was already caught loudly, and guard *downgrade* was caught by nothing
until the sweep became level-aware.

## Style

Match the surrounding code. There is no separate style guide, and a PR will not
be rejected over formatting.

Plain ASCII punctuation in code, comments, docs, and commit messages - regular
hyphens, straight quotes.
