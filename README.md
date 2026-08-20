# Architecture Zero

A self-hosted RAG assistant chassis with a measured trust layer: hybrid
retrieval with a cross-encoder reranker behind a provider seam, an
ingest-time injection gate for untrusted content, a judged evaluation harness
whose own instruments are calibrated, and a single-VM deployment shape.

Built by deriving the shared core that several production instances already
run - the pieces here shipped somewhere real first, carried a measurement, and
were cleaned for release afterward. Nothing in this repository is
aspirational.

Status: pre-release derivation in progress. See DERIVATION.md for what is in
scope, where each piece comes from, and the rules the derivation follows.

License: Apache-2.0.
