# Modules beyond the core

This file exists to be honest about the boundary: what this repository
contains, what exists beyond it, and how things cross that line.

## The model: seams, not an SDK

The open core is complete on its own - a full trust-layer RAG platform you
can deploy, measure, and extend. There is no license key, no crippled
feature, no "community edition" ceiling. Extension happens through the
platform's own seams, which are ordinary code paths, not a plugin API:

- the ingestion gate choke point (`app/database.py`): every write path
  runs the same scan-and-tier gate, so new ingestion surfaces inherit the
  security model by construction;
- the rerank provider seam (`app/rerank.py`): any endpoint speaking the
  scoring contract - POST {query, texts, model} -> {scores} - can serve
  ranking;
- the provider registry (`app/providers.py`): any OpenAI-dialect model API
  is a registry entry plus a key.

Commercial modules are private packages that install into these seams. No
fork, no patched core: a module is code the core already has a socket for.

## What exists beyond the core today

These run in production deployments and are available commercially:

- **Data connectors** - Google Drive, Calendar, and Gmail ingestion and
  answer-time search, each arriving through the ingestion gate with
  provenance tiers and per-item injection scanning; plus a web
  search/fetch leg with the same boundary discipline.
- **Permission-aware sync** - connector content that mirrors the source
  system's own access control into the platform's tiers, so a document
  restricted at the source stays restricted in answers.
- **Single sign-on** - Google Sign-In (OIDC) with per-instance auth modes,
  server-side allow policies, and a break-glass local admin; the same seam
  extends to other identity providers.
- **The MCP server** - the platform's knowledge served to MCP clients as
  read-only tools behind an OAuth 2.1 authorization server.

## Graduation

Modules move from the commercial lane into the open core over time - and
because a module is a package installing into a published seam, graduation
is a package changing repositories, not a rewrite. The MCP server is the
first candidate: it is read-only by design, which makes it the safest
piece to open next.

What will NOT appear in this repository, ever: any production instance's
corpus, configuration, or operational data. The demo corpus is synthetic
and the help corpus documents the platform itself.

## Contact

Commercial modules, support, or a deployment conversation: open an issue,
or reach the maintainer through the profile on this repository's
organization page.
