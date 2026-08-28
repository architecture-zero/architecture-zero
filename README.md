# Architecture Zero

A self-hosted AI assistant platform where trust is measured, not claimed.

Most "AI systems" are a text box, an API key, and a prompt. This is the
rest: hybrid retrieval with a cross-encoder reranker behind a swappable
provider seam, tiered access control enforced at three surfaces, an
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
  enforced identically at retrieval, at the file tools, and at the answer
  layer. Unlisted departments fail closed to Owner-only. A long-running
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
  list, and a corpus fingerprint on every run so incomparable runs can
  never be silently compared.
- **Eco Mode** - federate instances: peers contribute labeled,
  boundary-scanned chunks to answers, with per-peer health tracking and a
  circuit breaker so a down peer costs nothing.
- **Operations** - route-level authorization on every endpoint pinned by a
  two-sided test sweep, TOTP MFA with optional enforcement, audit receipts
  per answer (latency, time-to-first-token, which rerank provider actually
  served), consistent live backups, and fail-open controls that report
  their live state so "off" is visible.

## Quickstart

Prerequisites: Docker with the compose plugin; for local models, an
[Ollama](https://ollama.com) install on the host with a chat model and the
embedding model pulled (`ollama pull qwen3:8b`, `ollama pull nomic-embed-text`).

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

curl -X POST localhost:8000/api/auth/setup -H "Content-Type: application/json" \
  -d '{"username":"owner","password":"<strong password>","claim_code":"<from the logs>"}'
```

Your first question needs no documents of your own: the instance boots
already knowing itself (see
[the built-in onboarding assistant](#the-built-in-onboarding-assistant)),
and the runbook's deploy steps end with the exact call to make.

The platform is API-first: everything - chat (streaming SSE at /api/chat),
ingestion, users, evals, the trust panel - is served over a documented HTTP
surface, so any client works; a reference web frontend is on the roadmap.
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
corpus. Send chat requests with `"use_rag": true` so answers ground in
the documents (the runbook shows the full call). The shipped evaluation
seed asks the same class of support questions, so the very first eval run
scores how well the instance onboards its own operator - support quality
as a measured number from day one.

One honest boundary: the assistant answers from documentation and from
whatever you ingest, not from the instance's live configuration. "What
does the ingestion injection scan do?" it answers today; "is the scan
actually on, on this instance, right now?" belongs to the posture surface
(GET /api/status, which reports the live state of the fail-open controls)
until the live-system record generator on the [roadmap](ROADMAP.md) fills
the dormant `system` trust tier and current-state questions get the same
grounded treatment.

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
  bands, answer-layer runs)
- `knowledge/` - the shipped corpus (help docs + demo company)
- `docs/` - operator docs, ingested as corpus
- `docker-compose.yml`, `backend/Dockerfile` - the single-VM shape

## Development

```
python -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
cd backend && ../.venv/bin/python -m pytest tests -q
```

The suite mocks the vector store and embedder: no network, no keys, no
GPU. CI runs the same suite plus a full-history secret scan, a
private-residue guard (a denylist grep that fails the build if lineage
from the private deployments this template descends from appears in the
tree), and a Docker image build on every push to main.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
