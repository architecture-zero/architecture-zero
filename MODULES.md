# Modules beyond the core

This file exists to be honest about the boundary: what this repository
contains, what exists beyond it, and how things cross that line.

## The model: seams, not an SDK

The open core is complete on its own - a full trust-layer RAG platform you
can deploy, measure, and extend. There is no license key, no crippled
feature, no "community edition" ceiling. Extension happens through the
platform's own seams, which are ordinary code paths, not a plugin API:

- the ingestion gate choke point (`backend/app/database.py`): every write
  path runs the same scan-and-tier gate, so new ingestion surfaces inherit
  the security model by construction;
- the rerank provider seam (`backend/app/rerank.py`): any endpoint speaking
  the scoring contract - POST {query, texts, model} -> {scores} - can serve
  ranking;
- the provider registry (`backend/app/providers.py`): any OpenAI-dialect
  model API is a registry entry plus a key.

Commercial modules are built on this same core, in a separate product.
Today that means code carried in that product, not drop-in packages: a
connector's content does arrive through the ingestion gate above, but
per-document access control needed changes to the core itself - a
principals filter on retrieval, new account columns, changes to the chat
route - that this repository does not carry.

## What exists beyond the core today

These are in the commercial product today:

- **Data connectors** - Google Drive ingestion, arriving through the
  ingestion gate with provenance tiers and per-item injection scanning.
- **Permission-aware sync** - connector content that mirrors the source
  system's own access control onto each document as the list of who may
  read it, applied inside retrieval, so a document restricted at the
  source stays restricted in answers.
- **Single sign-on** - Google and Microsoft Entra sign-in (OIDC) beside
  the password login, chosen per instance, with server-side allow
  policies, each landing a person on the account that carries their
  address. There is no SSO-only mode yet: it needs a break-glass local
  admin this product does not have, so an instance configured for one
  refuses to boot rather than run looser than it claims.
- **Microsoft 365** - a SharePoint / OneDrive connector on the same
  permission-aware engine as Drive: application permissions consented once,
  per-item grants mapped onto the platform's access control.
- **Chat where people already are** - a Microsoft Teams bot and a Slack app
  that answer as the asker, through the same chat route and with the same
  document permissions as the web app; there is no second answer pipeline.

These run in a private deployment on the same core, and are not part of
the commercial product today:

- **More connectors** - Google Calendar and Gmail ingestion through the
  same ingestion gate, answer-time mail search, and a web search/fetch
  leg with the same boundary discipline.
- **The MCP server** - the platform's knowledge served to MCP clients as
  read-only tools behind an OAuth 2.1 authorization server.

## Graduation

Modules move into the open core over time. Because they are carried as
code rather than packages, graduating one is a port into this repository,
not a package changing hands. The MCP server is the first candidate: it
is read-only by design, which makes it the safest piece to open next.

What will NOT appear in this repository, ever: any production instance's
corpus, configuration, or operational data. The demo corpus is synthetic
and the help corpus documents the platform itself.

## Contact

Commercial modules, support, or a deployment conversation: open an issue,
or reach the maintainer through the profile on this repository's
organization page.
