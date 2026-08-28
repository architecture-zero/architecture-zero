# Getting started with Architecture Zero

## What is Architecture Zero?

Architecture Zero is a self-hosted AI assistant platform built around a
measured trust layer: hybrid retrieval (vector + BM25) with cross-encoder
reranking, tiered access control, an ingest-time injection gate, and a
judged evaluation system that publishes its own numbers. You run it on your
own infrastructure, point it at your own documents, and every answer is
grounded in your corpus with citations. This assistant you are talking to
right now is an Architecture Zero instance, and these help documents are
part of its knowledge base.

## What can I ask this assistant?

This instance booted with the platform's own documentation already
ingested, so it can onboard and support you directly - before you have
added any documents of your own. On record from the start: this
getting-started guide, a troubleshooting guide keyed to the exact error
messages the platform shows (paste the error text and ask), the security
model, a guide to reading the evaluation numbers, a FAQ, the operator
runbook, and a synthetic demo company for exploring departments and tiers
safely. Ask things like "how do I set up MFA?", "why was my upload
withheld?", or "what does the recall number mean?" - answers cite the
document they came from. Keep retrieval (RAG) on so answers ground in
these documents. The assistant can also answer from this
instance's own live state: records generated at boot describe its posture,
corpus and measurement state, and rank above ordinary documents on
current-state questions. They are Owner clearance, so at lower tiers the
operator surfaces remain the route (GET /api/status behind auth, and the
public trust panel at GET /api/trust).

## First run: create the Owner account

On a fresh install no accounts exist. The very first step is creating the
Owner: POST to /api/auth/setup with a username, a password, and a claim
code - that first account automatically becomes the Owner, the highest
access tier.

The claim code is what makes the first run safe, and it is worth
understanding rather than pasting. The setup endpoint disables itself
permanently the moment an Owner exists - but the window BEFORE that is
real: a deployment reachable from a network with no Owner yet belongs to
whoever posts to it first. (This guide used to say there was no such
window. That was wrong, and it was corrected on 2026-08-27 along with the
fix.) So the backend now mints a random code at every boot while the
deployment is unclaimed and prints it to its own logs - `docker compose
logs backend`. Only someone who can already read your server logs has
seen it. The code dies the instant the Owner is created, and a restart
before then mints a new one.

Running multiple workers or replicas? Set SETUP_CLAIM_CODE in the
environment instead. The generated code lives in one process's memory, so
with several processes only one of them would accept yours.

If you see "Owner already exists", setup already ran; sign in instead. If
you see "Invalid or missing claim code", re-read the boot banner in the
logs - and if the container has restarted since you copied it, take the
newer one. The platform is API-first: every surface in this guide is an HTTP endpoint,
usable from any client. A reference web client ships with the platform and
serves the claim screen at http://localhost:5173 on a stock deployment, so
the steps in this guide can be followed from a browser instead of curl.

## Signing in and staying signed in

Sign in with username and password (POST /api/auth/login). You receive a
short-lived access token and a refresh token; clients refresh silently via
POST /api/auth/refresh. If your session expires you get "Session expired -
sign in again" - just sign in again. After too many wrong passwords the
account locks temporarily ("Too many failed attempts") and unlocks itself
after the lockout window, or an admin can unlock it immediately (POST
/api/admin/users/{id}/unlock).

## Setting up two-factor authentication (MFA)

Order matters: enroll first, enforce second. From a signed-in session, call
MFA setup (POST /api/auth/mfa/setup) to get a QR code, scan it with any
TOTP authenticator app, then confirm one code (POST /api/auth/mfa/enable)
to activate. Only AFTER every account that needs password login has
enrolled should the operator set REQUIRE_MFA=true in the host environment
and restart. With enforcement on, a password login for an account with no
enrolled authenticator is refused outright - so flipping the flag before
enrolling locks that account out until an admin resets its MFA.

On an UNCLAIMED deployment there is no admin to do that resetting, so
claiming is refused outright while REQUIRE_MFA is set. Claiming does not
enroll a factor, enrolling needs a signed-in session, and signing in is what
enforcement refuses - a deployment claimed under the flag would have an Owner
who can never sign in, a spent claim code, and no way back short of editing
the database. Claim with the flag off, enroll, then turn it on and restart.

## Connecting a model provider

Out of the box the backend expects a local Ollama server (OLLAMA_BASE,
default http://host.docker.internal:11434) - install Ollama, pull a model,
and it appears in the model list automatically. Cloud providers activate
the moment their API key is configured: the Owner sets keys via PUT
/api/settings (Anthropic, OpenAI, Gemini, Mistral, Groq, xAI, DeepSeek), or
they come from the host environment (ANTHROPIC_API_KEY and friends). A
provider with no key stays dormant and out of the model list entirely.

## Adding your first documents

Three ways in, all passing the same ingestion gate:
1. Upload - POST /api/ingest/upload accepts markdown, text, PDF, docx, and
   common code files; uploads are chunked in fixed windows.
2. The knowledge directory - drop files into the mounted knowledge folder;
   a file watcher ingests changes live, and a startup sync catches anything
   added while the server was down. These files are chunked on markdown
   section headings (the best-retrieving shape), and re-saving a file
   re-ingests only the sections that actually changed.
3. The API - POST /api/ingest indexes the posted text as one document, for
   programmatic writes that manage their own chunking.
Everything becomes retrievable immediately after ingest.

## Departments and user tiers

Every user has a role (Owner, Admin, Member - plus Guest for anonymous
access if you enable it) and a department. Documents live in departments
too: "general" is the shared floor everyone can read; departments listed
with a higher clearance floor (like the built-in "restricted" and
"history") are only retrievable by tiers cleared for them. Retrieval, the
assistant's file tools, and the answer layer all enforce the same tiers, so
a lower tier cannot pull higher-tier content into an answer by any path.
Add users via POST /api/users, and set role, department, and permissions
through the same users API.

## Running your first evaluation

The instance ships with a seeded evaluation set (synced from the repo's
eval-questions.json on every boot). Kick off a retrieval-only pass first
(POST /api/admin/evals/run) - it is fast, costs no model calls, and tells
you whether retrieval finds the right documents (recall, with a ranked
miss list at /api/admin/evals/recall). Then run a full answer-mode pass
(retrieval_only=false) to have the judges score answer quality,
groundedness, freshness, and honesty. The writer and judge models are
pinned in admin config and must come from different provider families -
the system refuses to let a model family grade its own answers.

## Reading the trust panel

GET /api/trust is the public trust panel: the instance's own measured
numbers, derived live from stored evaluation runs - never hand-entered.
It reports bands across repeated runs rather than a single lucky score, and
honesty is its own metric, never blended into correctness. If the panel
shows no numbers yet, no complete evaluation run exists - run one. The
admin variant (/api/admin/trust) adds provenance detail behind auth.

## Guest mode (off by default)

The instance is private by default: every request needs a signed-in user.
Guest access requires BOTH the host environment opt-in
(ALLOW_GUEST_MODE=true) and the admin config toggle - so a stray config
row alone can never open the site. Guests get a capped number of turns and
a reduced token budget, and only ever see general-floor content. An instance
left open to the public can also set DEMO_DAILY_GUEST_LIMIT, a global daily
budget capping total guest requests per UTC day across all callers - the
total-volume bound per-IP limits cannot give you. It counts requests rather
than tokens, so per-request cost still follows whichever model a request names.
It is 0, meaning off, by default.
