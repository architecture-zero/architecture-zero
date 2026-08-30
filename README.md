# Architecture Zero

A self-hosted AI assistant platform where trust is measured, not claimed.

Most "AI systems" are a text box, an API key, and a prompt. This is the
rest: hybrid retrieval with a cross-encoder reranker behind a swappable
provider seam, tiered access control enforced at four surfaces, an
ingest-time injection gate that quarantines hostile content before it can
reach an answer, and a judged evaluation harness that scores the system's
correctness, groundedness, freshness, and honesty - then publishes those
numbers on a public trust panel derived live from stored runs. If a number
is on the panel, a run produced it; nothing is hand-set.

Every piece here shipped somewhere real first. The platform is derived
from the shared core of production instances that have been running,
measured, and incident-hardened - the code carries those lessons as
working constraints, not war stories. Nothing in this repository is
aspirational.

## What's in the box

- **Grounded chat** - streaming answers with source citations, an agentic
  tool loop (workspace file tools, clearance-gated, off by default), and
  honest failure modes: an empty model turn gets one retry and then says
  so, never a blank bubble.
- **A built-in onboarding assistant** - the platform ships pre-loaded
  with its own documentation, so a fresh install explains, onboards, and
  troubleshoots itself from the first question. See
  [the section below](#the-built-in-onboarding-assistant).
- **Retrieval that earns its rank** - vector + full-corpus BM25 fusion,
  per-source diversity, then a cross-encoder reranker. The reranker is a
  provider seam: in-process ONNX (baked into the image), a remote scoring
  endpoint for a GPU box, or a hosted API - selected per call by config,
  with a fallback chain that degrades to slower, never dumber.
- **Departments and tiers** - corpus partitions with clearance floors,
  enforced identically at retrieval, at the file tools, at the answer
  layer, and on both sides of the federation seam. Unlisted departments
  fail closed to Owner-only. A long-running
  work log lives in its own department, searched only when a question is
  actually history-shaped, with recency weighting inside it.
- **The injection gate** - every document is scanned at ingestion for
  instruction overrides, role hijacks, exfiltration patterns, and hidden
  Unicode. Hot findings in untrusted content are quarantined for owner
  review; the owner's own content is tagged, never withheld. Content from
  federated peers gets the same scan at the chat boundary.
- **Judged evals** - four rubrics (correctness, faithfulness, freshness,
  honesty) with a pinned judge that must differ in provider family from
  the writer, a locked holdout cohort with the tuned-vs-holdout GAP as an
  overfitting alarm, a mechanically-graded injection-resistance cohort run
  against a live poisoned plant, retrieval recall with a named-miss gaps
  list, and a corpus-SHAPE fingerprint on every run so runs measured
  against a different corpus shape cannot be silently compared (content
  edits that leave chunk counts alone are not yet visible to it - see the
  ROADMAP).
- **Eco Mode** - federate instances: peers contribute labeled,
  boundary-scanned chunks to answers, with per-peer health tracking and a
  circuit breaker so a down peer costs nothing. Clearance holds across the
  seam without either instance trusting the other's word for it: a peer
  key's scope bounds what LEAVES, a local clearance floor bounds who may
  RECEIVE, and both answer to the same ladder.
- **Operations** - route-level authorization on every endpoint pinned by a
  two-sided test sweep, TOTP MFA with optional enforcement, audit receipts
  per answer (latency, time-to-first-token, which rerank provider actually
  served), consistent live backups, and fail-open controls that report
  their live state so "off" is visible.

## Quickstart

Prerequisites: Docker with the compose plugin, and an
[Ollama](https://ollama.com) install on the host with the embedding model
pulled (`ollama pull nomic-embed-text`) plus a chat model if you want local
inference (`ollama pull qwen3:8b`).

The embedder is **required whichever provider answers chat.** Only the chat
model is swappable for a cloud API; embeddings speak the Ollama dialect to
`EMBED_BASE` and have no provider seam. With nothing reachable there the
instance still boots, ingestion fails per file, and any question asked with
retrieval on - the default - returns a 500 rather than an answer. `/api/health`
probes only the chat endpoint, so it reports healthy throughout.

```
git clone https://github.com/architecture-zero/architecture-zero
cd architecture-zero
cp .env.example .env    # set JWT_SECRET_KEY - the backend refuses to boot on the placeholder
docker compose up -d --build
```

Create the Owner account and ask your first question. Claiming a deployment
takes a code that the backend prints to its own logs at boot - `docker compose
logs backend` - so a deployment that is reachable before you finish setup
cannot be taken by whoever gets there first:

```
docker compose logs backend | grep -A2 "claim code"
```

Then open <http://localhost:5173> - a fresh deployment routes straight to the
claim screen. Paste the code, pick a username and password, and you are signed
in as the Owner.

Or claim it from the API instead, which is the same endpoint the screen calls:

```
curl -X POST localhost:8000/api/auth/setup -H "Content-Type: application/json" \
  -d '{"username":"owner","password":"<strong password>","claim_code":"<from the logs>"}'
```

Your first question needs no documents of your own: the instance boots
already knowing itself (see
[the built-in onboarding assistant](#the-built-in-onboarding-assistant)),
and the runbook's deploy steps end with the exact call to make.

The platform is API-first: everything - chat (streaming SSE at /api/chat),
ingestion, users, evals, the trust panel - is served over a documented HTTP
surface, so any client works. A reference web client ships with it
(`frontend/`): chat with citations, the claim screen, and an admin panel
covering knowledge base, quarantine review, users, models, system prompt,
audit, monitoring, backup and the ingest queue. It is a reference, not a
requirement - the API is the product, and the client is one consumer of it.
Full walkthrough: [docs/runbook.md](docs/runbook.md).

## The built-in onboarding assistant

Architecture Zero does not just ship with documentation - the assistant
is its own support channel, pre-loaded with its own manual. The shipped
corpus is the product's documentation: a getting-started guide, an
operations troubleshooting guide keyed to the exact error strings the
code emits (paste the error, retrieve its explanation), the security
model, and a guide to what the evaluation numbers mean - plus a synthetic
demo company so departments, tiers, and history routing demonstrate
without anyone's real data. All of it ingests automatically on first
boot.

So the moment the Owner account exists, you can ask the instance itself -
"how do I add my first documents?", "why was my upload withheld?", "what
does the recall number mean?" - and get a cited answer from its own
corpus - retrieval is on by default, so a plain request is already
grounded (send `"use_rag": false` to ask without the corpus). The shipped
evaluation seed asks the same class of support questions, so the first run
scores how well the instance onboards its own operator - support quality
as a measured number from day one.

The assistant also answers from the instance itself, not only from
documentation. "What does the ingestion injection scan do?" is a
documentation question; "is the scan actually on, on this instance, right
now?" is answered from records generated at boot from the live database
and ranked above everything else in the context. Those records are Owner
clearance, so lower tiers still use the posture surfaces (GET /api/status,
and the public trust panel at GET /api/trust). The records carry no
document names, no account names, and nothing a user typed - only values
the code produced or that passed an allowlist.

## Extending it

There is no SDK - the platform's own seams are the developer surface:

- the **ingestion gate choke point**: every ingestion path shares one
  gate, so anything you build that writes to the corpus inherits scanning
  by construction;
- the **rerank provider seam**: any endpoint speaking the scoring contract
  can serve ranking;
- the **provider registry**: any OpenAI-dialect model API is a registry
  entry plus a key, not an adapter.

Commercial modules exist for the enterprise edges (data connectors with
permission-aware sync, SSO) and install into these same seams - see
[MODULES.md](MODULES.md) for the honest map of what exists beyond the
core and how modules graduate into it.

## Repository layout

- `backend/app/` - the platform (FastAPI)
- `backend/tests/` - the suite CI runs on every push
- `backend/scripts/` - the measurement harness (retrieval A/B arms, noise
  bands, answer-layer runs, outside-model holdout authoring, the live-fire
  injection probe)
- `frontend/` - the reference web client (React + Vite, served by nginx)
- `scripts/acceptance.sh` - end-to-end check against a RUNNING deployment
- `knowledge/` - the shipped corpus (help docs + demo company)
- `docs/` - operator docs, ingested as corpus
- `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile` - the
  single-VM shape

## Development

```
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt          # Windows: .venv\Scripts\pip
cd backend && ../.venv/bin/python -m pytest tests -q           # Windows: ..\.venv\Scripts\python
```

The suite mocks the vector store and embedder: no network, no keys, no
GPU.

The client, in a container - this works everywhere, including on machines
whose OS refuses to load unsigned native modules (see below):

```
cd frontend
docker run --rm -v "$PWD:/app" -v /app/node_modules -w /app node:20-alpine \
  sh -c "npm ci && npm run type-check && npm test && npm run build"
```

Or on the host, if you have Node 20+ and your OS allows it:

```
cd frontend && npm install
npm run type-check && npm test && npm run build
npm run dev          # see the note below before using this
```

`npm run dev` proxies `/api` to `http://backend:8000` - the compose service
name, which resolves only inside the compose network. Running it on the host
against the stock deployment, the page loads and every API call fails to
resolve: point `vite.config.ts` at `http://localhost:8000` first, and expect
vite to pick a different port since compose already holds 5173.

**On Windows, the host path may not work at all, and the error will not tell
you why.** Smart App Control (on by default on many Windows 11 installs) and
corporate WDAC policies block unsigned native binaries, and npm ships native
modules unsigned as a matter of course - so `vite` cannot load its own rollup
binary. Every one of `npm run dev`, `npm test` and `npm run build` fails; only
`npm run type-check` still works, since TypeScript is pure JavaScript. Use the
container command above. Nothing about deploying is affected: `docker compose
up --build` builds the client inside Linux, where the policy does not apply.

CI runs both suites plus a full-history secret scan, a private-residue
guard (a denylist grep that fails the build if lineage from the private
deployments this template descends from appears in the tree), and both
Docker image builds on every push to main.

## Known limitations at v0.1.1

Stated here rather than discovered later. Each is tracked in
[ROADMAP.md](ROADMAP.md).

- **Sessions end after 30 minutes and you sign in again.** The API issues a
  refresh token and rotates it correctly, but the reference client does not
  yet spend it, so an access token simply expires. It fails honestly - you
  are told to sign in, nothing is lost but an unsent draft - and wiring
  silent refresh changes how the client behaves on every 401, which wants
  its own tests rather than a release-eve patch.
- **One box.** Queued ingest runs on a worker thread inside the backend, not
  a separate process, because the vector store is an embedded database on a
  local directory - a second writing process would corrupt the index. This
  scales up, not out.
- **`/metrics` needs `METRICS_TOKEN` for a scraper.** A user session expires
  faster than a scrape interval matters, so set the static token if you want
  Prometheus to pull. Unset, the endpoint is still reachable with a normal
  signed-in request.
- **The client is a reference implementation.** It exercises the whole API
  and is what the acceptance suite drives, but it is a starting point to
  build on rather than a finished product surface.
- **Client test coverage is narrow and deliberately so.** The automated
  tests that mount the chat client cover one thing thoroughly: the rule that
  a message bubble is marked as unstored exactly when no row for it exists on
  the server, checked across every way a stream can end. That rule is where
  the defects were, and getting it wrong deletes conversation history, so it
  is tested as an invariant with a case per stream outcome. The rest of the
  client - layout, the admin panel, model selection - is covered by the
  acceptance suite driving a real deployment, not by unit tests. Read a green
  frontend suite as "the storage invariant holds", not "the client is
  verified".

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
