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

Create the Owner account and ask your first question:

```
curl -X POST localhost:8000/api/auth/setup -H "Content-Type: application/json" \
  -d '{"username":"owner","password":"<strong password>"}'
```

The platform is API-first: everything - chat (streaming SSE at /api/chat),
ingestion, users, evals, the trust panel - is served over a documented HTTP
surface, so any client works; a reference web frontend is on the roadmap.
Full walkthrough: [docs/runbook.md](docs/runbook.md).

## Ask it about itself

The shipped corpus is the product's own documentation: onboarding, an
operations-grade troubleshooting guide keyed to the exact errors the code
emits, the security model, and a guide to what the evaluation numbers
mean - plus a synthetic demo company so departments, tiers, and history
routing demonstrate without anyone's real data. A fresh install can
onboard and support its own users, and the shipped evaluation seed scores
how well it does that from the very first run.

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
