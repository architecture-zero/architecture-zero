# Troubleshooting Architecture Zero

## The assistant says something is "not on record" but the document exists

Three causes, in order of likelihood. First: is RAG actually on for this
conversation? With retrieval off the assistant is told to say so rather
than fake a lookup - check the RAG toggle. Second: retrieval found the
document but below the similarity threshold - lower
rag_similarity_threshold in Settings slightly, or ask with wording closer
to the document's own vocabulary. Third: a genuine retrieval gap - run a
retrieval-only evaluation and check the Knowledge Gaps list, which names
exactly which expected documents retrieval failed to surface and what came
back instead of them.

## "Login required - this instance is private"

The instance is working as designed: it is private by default. Sign in, or
if you intend anonymous access, enable guest mode BOTH ways - the
ALLOW_GUEST_MODE=true host environment variable AND the guest toggle in
admin config. Either one alone keeps the instance closed.

## "Session expired - sign in again"

Your access token expired and the presented token was invalid - this is the
normal refresh signal, and the app usually refreshes silently. Seeing it
repeatedly means the refresh token also expired (sign in again) or the
server's JWT secret changed (every session invalidates when it rotates).

## My upload was withheld: "Injection-shaped content withheld"

The ingestion gate scanned the document and found content shaped like an
attack on the assistant - instruction overrides ("ignore all previous
instructions"), hidden text, exfiltration directives, or similar - in a
document from an untrusted source. Nothing was indexed. Review it under
Admin > KB Quarantine: the findings list says exactly what fired. If the
document is legitimate, Release re-ingests it with the block waived - it
stays labeled untrusted at retrieval and the assistant still treats its
content as data, never instructions. Only the Owner can release.

## "MFA is required on this instance and this account has no TOTP enrolled"

The operator set REQUIRE_MFA=true but this account never enrolled an
authenticator. From another signed-in session (or after an admin resets
the account's MFA), enroll via MFA setup and retry. Operators: always have
every password account enroll BEFORE flipping REQUIRE_MFA.

## "Account locked. Try again in N minutes"

Too many failed password attempts. Wait out the lockout window, or an admin
can unlock immediately (Admin > Users > Unlock). If this fires without
failed attempts by the real user, treat it as someone guessing at the
account's password and review the audit log.

## Answers are slow

Almost always the reranker: scoring the candidate pool with a
cross-encoder on a small CPU takes seconds per answer. Check Admin > KB >
Rerank Status - it shows which provider actually served and a live
self-test. Options, fastest first: point rerank_provider=remote-http at a
GPU box running a scoring endpoint; use hosted-api (Cohere/Voyage - only
if the operator set the RERANK_HOSTED_ALLOWED host latch, since it sends
chunk text to the vendor); or disable reranking (rerank_enabled=false) and
accept retriever-order results. All of these are live config flips, no
restart needed.

## Rerank status says "reranker load failed" or provider "none"

The scoring model could not load (first-run download blocked, missing
model cache) or the configured remote endpoint is unreachable. The system
degrades gracefully - answers still flow using the retriever's own order -
but ranking quality drops silently, which is why the status surface
exists. Fix the named error, or flip rerank_provider to a working leg. A
non-local provider that fails falls back to the local encoder
automatically before giving up.

## The evaluation refused to run: "Startup ingest is still re-embedding"

The boot-time corpus sync is still running, and an evaluation started now
would measure a half-ingested index and produce a plausible-looking wrong
number. Wait for the startup_sync_done lines in the backend logs, then
retry. This guard exists because half-corpus numbers look real.

## The evaluation refused to run: "same provider family - self-graded"

The answer model and the judge model resolve to the same provider family,
and a judge grading its own lab's writer biases every score. Change
eval_answer_model or eval_judge_model in admin config so they differ, or
pass allow_same_family=true only if you deliberately accept a self-graded
run.

## "I could not produce an answer this turn"

The model returned empty output twice in a row (the system retries once
automatically before saying this). Usually a provider-side hiccup - resend
the message. If it persists on a local model, the model may be failing to
load or out of memory on the Ollama host; check Ollama's logs and
/api/health.

## Chat answers "I can only answer questions based on the documents..."

The instance runs in RAG-only mode (RAG_ONLY_MODE=true) and retrieval
found nothing above the similarity threshold for this question. That
refusal is the designed behavior for corpus-bound deployments: no
retrieval, no answer. Ask about corpus content, add the missing documents,
or lower the threshold.

## Health says "degraded" / Ollama unreachable

/api/health pings the Ollama base URL. Degraded means that ping failed:
the Ollama server is down, the OLLAMA_BASE address is wrong for your
network layout (from inside a container, localhost is the container - use
host.docker.internal or the host's address), or a firewall blocks it.
Cloud-only deployments can ignore Ollama health if no local models are
used.

## Vectors disappeared after a crash or power loss

The vector index persists on a write threshold, not on close - a hard kill
can lose vectors written since the last flush while the document metadata
survives. A graceful stop flushes automatically. After a hard kill, the
startup sync detects sources whose indexed chunk count no longer matches
the file and re-ingests exactly the missing chunks. If a collection
refuses searches entirely after a crash, delete and re-ingest its sources
- the content-addressed ingest rebuilds only what is missing.

## Uploads rejected: "File too large" or "Unsupported file type"

The upload cap defaults to 50 MB (MAX_UPLOAD_MB). Supported types: md,
txt, pdf, docx, py, js, ts, json, yaml. "No text could be extracted" on a
PDF usually means a scanned/image-only PDF - run OCR first, the platform
ingests text, not images.

## I cannot delete or demote a user

Two protections fire here by design: you cannot deactivate yourself, and
the LAST active Owner can never be deactivated or demoted - removing it
would re-open the public first-run setup endpoint to anyone. Create a
second Owner first if you are rotating the account.
