# Frequently asked questions

## Why do answers cite sources?

Because grounded answers are the product. When retrieval is on, the
assistant answers from retrieved document chunks and the app shows which
sources fed the answer. The system prompt forbids stating specific figures
that appear in neither the retrieved context nor your message - if it
cannot ground a number, it says so instead of estimating. An answer you
cannot trace is an answer you cannot trust.

## What is RAG, in this platform's terms?

Retrieval-Augmented Generation: before the model answers, the platform
searches your knowledge base - a vector similarity search AND a full-corpus
keyword search (BM25), fused - then a cross-encoder reranker reads the
candidates against your actual question and keeps the best few. Those
chunks ride into the prompt as labeled context. The model never answers
corpus questions from its own training memory when retrieval is on.

## What are departments?

Departments are the corpus's partitions. "general" is the shared floor -
always searched, readable by every tier. Other departments carry a
clearance floor: the built-in "restricted" holds internal documents only
Owner-tier callers can retrieve, and "history" holds the operational
session log, which is Owner-only AND only searched when a question is
actually history-shaped (so a large log cannot crowd fact answers). A
department not on the clearance map at all is closed to everyone below
Owner - private until deliberately shared.

## What are trust tiers? How are they different from access tiers?

Access tiers say who may READ a document. Trust tiers say how much
AUTHORITY its content carries in an answer - who wrote it. Four tiers:
"system" (records generated from the live database - the current-state
authority, written at boot by the producer and covering this instance's
posture, corpus and measurement state),
"curated" (the owner's own authored content), "external" (content arriving
live from federated peer instances), and "untrusted" (third-party content:
non-owner uploads and anything a connector would bring in). Higher tiers
lead the context, and the prompt rules make untrusted content data to
quote, never instructions to follow.

## What does the "not on record" phrasing mean?

It is the honesty convention: when the corpus does not contain the fact,
the assistant says the information is not on record rather than inventing
a plausible answer. If you know the fact exists in your documents and you
still get "not on record", see the troubleshooting guide - it is usually
the RAG toggle or a retrieval gap, both diagnosable.

## Why does the model picker group models the way it does?

Groups are providers. Local (Ollama) models appear automatically from
whatever the Ollama server has pulled. Cloud providers appear only when
their API key is configured - an unkeyed provider is dormant and hidden.
A short baked-in blocklist also hides local models whose weights are not
clean to redistribute in a client deployment - a license decision, not a
capability one.

## What is guest mode?

Anonymous, sign-in-free chat - off by default, and it takes two deliberate
switches to open (a host environment opt-in AND an admin config toggle).
Guests are capped in turns and answer length, resolve to the lowest access
tier, and only ever retrieve general-floor content. An optional global daily
budget caps total guest requests per UTC day across everyone at once, so a
public instance can bound total daily guest volume and not just per-visitor
volume. It caps request COUNT - per-request cost still follows the model a
request names. Guest history rows
carry no account or address, and anonymous sessions age out.

## What are peers / Eco Mode?

Federation between Architecture Zero instances. With peers configured and
the peers toggle on, a question also queries each enabled peer's knowledge
base; their chunks arrive labeled as EXTERNAL PEER CONTENT, are scanned at
the boundary like any untrusted input, and supplement your own corpus in
the same answer. Serving your OWN knowledge base to peers is a deliberate
opt-in (ECO_EXPOSE_KB plus per-peer keys, each scoped to public-only or
all departments). A down peer is skipped by a circuit breaker instead of
stalling every chat, and per-peer health is visible at /api/peers/status.

## Can the assistant browse the web?

No - by design, and it will say so. This core has no web access and no
standing search tool; its world knowledge ends at its model's training
cutoff. For current outside information it discloses the limit instead of
answering from stale memory. What it does know deeply is whatever you have
ingested.

## What file types can I ingest?

Markdown, plain text, PDF (text-based; scanned PDFs need OCR first), Word
docx, and common code/config files (py, js, ts, json, yaml). Markdown is
the best citizen: chunking follows its section headings, so each section
becomes a clean retrievable unit.

## Where does my data live?

On your infrastructure, full stop. Documents, embeddings, chat history,
users, and evaluation results all live in the instance's own database and
vector store on the host you deploy. The only egress is what you configure:
calls to a cloud model provider you keyed, peers you registered, and - only
if the operator explicitly sets the host-level latch - a hosted reranking
API. No latch, no third-party rerank egress, regardless of any config flip.
