# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to adhere to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-06-08

First public release. Programmatic Google Colab control plus a multi-backend job API.

### Added

- **Transports** behind a single `TransportAdapter` contract:
  - `cli` — wraps the official `google-colab-cli` (sanctioned default), with a
    golden-tested stdout parser pinned to v0.5.7.
  - `native` — from-scratch `/tun/m/*` client + Jupyter-websocket kernel (co-primary,
    opt-in). Both **live-validated** against real Colab Pro.
- **Auth** — ADC-led providers (the Phase 0-verified path) + scope constants.
- **Secrets** — one `SecretStore` contract over keyring (chunked), an encrypted file
  (headless/CI), and an in-memory store.
- **SDK** — `ColabClient` / `ColabSession` (async, context-managed) and the `@remote`
  decorator (ship a local function to a GPU via cloudpickle).
- **CLI** — `colabctl` (Typer): `run`, `exec`, `new`, `sessions`, `status`, `stop`,
  `upload`, `download`, `keepalive`, and `job run` / `job backends`.
- **MCP server** — `colabctl-mcp` (FastMCP) exposing 9 tools (interactive + batch-job)
  so AI agents can drive Colab, Modal, and Vertex.
- **Provider abstraction** — `Backend` (submit/status/logs/result/cancel) + a
  capability-routing `BackendRouter` with infra failover, plus the **Colab**,
  **Modal** (live-validated), **Vertex AI**, **Hugging Face Jobs**, and **Kaggle**
  backends, wired into the CLI (`colabctl job …`) and MCP.
- **Browser-bridge transport** (colab-mcp model) — JSON-RPC relay over a local
  WebSocket; human-in-the-loop, needs live validation.
- **Runtime-lifecycle manager** — best-effort keep-alive ticks + proactive checkpoint
  + automatic re-assign/restore on idle reclamation.
- **Drive sync** — `DriveSync` (durable My-Drive file sync via user-OAuth) + lifecycle
  checkpoint/restore hooks.
- **Observability** — namespaced logging + a reusable `retry_async` (exponential
  backoff that never retries terminal quota/entitlement errors).
- **Spend guard** — `cap_timeout` enforces a hard billable-time ceiling on paid
  backends (wired into Modal).

### Known limitations

- The Colab RuntimeService keep-alive RPC is unusable under token auth (live-confirmed);
  long jobs rely on kernel activity + checkpoint/re-assign instead.
- Vertex stdout is in Cloud Logging (not captured); `result` returns state + a log link.
- Vertex / Hugging Face / Kaggle backends and the browser-bridge are not yet
  live-validated (no accounts in CI); their logic is unit-tested against fakes.
- Still planned: RunPod/vast.ai + hyperscaler backends, a papermill notebook adapter,
  a `jupyter_http_over_ws` integration test rig, a billing watchdog, and a docs site.

[Unreleased]: https://github.com/mandipadk/colabctl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mandipadk/colabctl/releases/tag/v0.1.0
