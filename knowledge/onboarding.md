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

## First run: create the Owner account

On a fresh install no accounts exist. The very first step is creating the
Owner: open the app (or POST to /api/auth/setup) and register the first
account - it automatically becomes the Owner, the highest access tier. The
setup endpoint disables itself permanently the moment an Owner exists, so
there is no window for someone else to claim an unconfigured instance. If
you see "Owner already exists", setup already ran; sign in instead.

## Signing in and staying signed in

Sign in with username and password at the login screen (POST
/api/auth/login). You receive a short-lived access token and a refresh
token; the app refreshes silently in the background. If your session
expires you get "Session expired - sign in again" - just sign in again.
After too many wrong passwords the account locks temporarily ("Too many
failed attempts") and unlocks itself after the lockout window, or an admin
can unlock it immediately from the user admin panel.

## Setting up two-factor authentication (MFA)

Order matters: enroll first, enforce second. From a signed-in session, call
MFA setup (POST /api/auth/mfa/setup) to get a QR code, scan it with any
TOTP authenticator app, then confirm one code (POST /api/auth/mfa/enable)
to activate. Only AFTER every account that needs password login has
enrolled should the operator set REQUIRE_MFA=true in the host environment
and restart. With enforcement on, a password login for an account with no
enrolled authenticator is refused outright - so flipping the flag before
enrolling locks that account out until an admin resets its MFA.

## Connecting a model provider

Out of the box the backend expects a local Ollama server (OLLAMA_BASE,
default http://host.docker.internal:11434) - install Ollama, pull a model,
and it appears in the model picker automatically. Cloud providers activate
the moment their API key is configured: open Settings (Owner only) and add
keys for Anthropic, OpenAI, Gemini, Mistral, Groq, xAI, or DeepSeek. A
provider with no key stays dormant and out of the picker entirely. Keys can
also come from the host environment (ANTHROPIC_API_KEY and friends).

## Adding your first documents

Three ways in, all passing the same ingestion gate:
1. Upload - the admin KB screen or POST /api/ingest/upload accepts
   markdown, text, PDF, docx, and common code files.
2. The knowledge directory - drop files into the mounted knowledge folder;
   a file watcher ingests changes live, and a startup sync catches anything
   added while the server was down.
3. The API - POST /api/ingest for programmatic single-document writes.
Documents are chunked on markdown section headings, embedded, and become
retrievable immediately. Re-saving a file re-ingests only the sections
that actually changed.

## Departments and user tiers

Every user has a role (Owner, Admin, Member - plus Guest for anonymous
access if you enable it) and a department. Documents live in departments
too: "general" is the shared floor everyone can read; departments listed
with a higher clearance floor (like the built-in "restricted" and
"history") are only retrievable by tiers cleared for them. Retrieval, the
assistant's file tools, and the answer layer all enforce the same tiers, so
a lower tier cannot pull higher-tier content into an answer by any path.
Add users from the admin panel and set their role and department there.

## Running your first evaluation

The instance ships with a seeded evaluation set (synced from the repo's
eval-questions.json on every boot). From the admin evals screen, run a
retrieval-only pass first - it is fast, costs no model calls, and tells you
whether retrieval finds the right documents (recall, with a ranked miss
list). Then run a full answer-mode pass to have the judges score answer
quality, groundedness, freshness, and honesty. The writer and judge models
are pinned in admin config and must come from different provider families -
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
a reduced token budget, and only ever see general-floor content.
