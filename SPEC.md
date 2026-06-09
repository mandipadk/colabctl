# colabctl — Programmatic Google Colab Control

> **Complete engineering specification** for a production-grade package that gives developers *and* AI agents (Claude / Codex) full programmatic control over Google Colab — allocate GPU/TPU runtimes, run code and notebooks, stream outputs, and sync files — **without ever touching the Colab website manually.**

- **Status:** Specification complete — pre-implementation
- **Date:** 2026-05-31
- **Working name:** `colabctl` (final name TBD)
- **Basis:** Synthesized from a 50-agent research → adversarial-verification → design pipeline (7 research tracks, 29 candidate approaches stress-tested, 12 subsystems designed).
- **Companion docs:** [`DECISIONS.md`](./DECISIONS.md) (approach-by-approach viability matrix) · [`RESEARCH.md`](./RESEARCH.md) (sourced research dossiers).

---

## 0. Read this first — the strategic finding that shapes everything

The instinctive plan — *reverse-engineer Colab and drive it from a hidden client* — is **the wrong default in 2026.** The adversarial review surfaced one dominant fact:

> **Google now ships its own official, sanctioned, agent-targeted Colab control tooling** — [`google-colab-cli`](https://pypi.org/project/google-colab-cli/) (v0.5.x, Apache-2.0) and [`colab-mcp`](https://github.com/googlecolab/colab-mcp) (v1.0.x, Apache-2.0) — that does exactly the headline goal: **headless T4 / L4 / A100 / H100 sessions with code, notebook, and file operations.**

This inverts the architecture. Hand-rolling the raw `/tun/m/*` backend means *owning every silent breakage Google introduces while gaining nothing the official CLI doesn't already give.* The reverse-engineered transports (direct backend, cookie + `SAPISIDHASH`, browser/UI automation, in-page iframe) scored **1–3.5 / 10** and are not just fragile — they are **strategically obsolete** and, for cookie/browser paths, **structurally dead within this project's ship window** (Device-Bound Session Credentials reached GA in Chrome 146, April 2026).

Two more facts that the design is built around:

1. **ToS risk on *paid* Colab Pro is MEDIUM/LOW, not high.** The FAQ bans on UI-bypass and remote control apply to *free* runtimes and are explicitly lifted on paid plans with a positive compute-unit balance. The real residual risk is **opaque, no-recourse abuse-detection bans** on sustained headless GPU usage — which we treat as a first-class, *user-disclosed* product fact (see [Risk Register](#risk-register)).
2. **Durable survivability comes from the provider abstraction** (the single highest-rated strategic decision, 7/10). Colab is the first-class backend, but its two irreducible risks (Google interface churn + abuse-detection bans) are contained behind a capability-detecting interface so the product *routes around* failure to Modal (8/10) or Colab Enterprise/Vertex (6.5/10) instead of dying.

**The decision:** wrap Google's official tooling as the sanctioned-primary Colab transport, keep a thin reverse-engineered `/tun/m/*` client as a *contained, opt-in, version-gated escape hatch* (never the default), and invest the durable engineering in the provider abstraction. This is a complete system — real auth lifecycle, runtime lifecycle, structured execution, durable Drive sync, an MCP server, a Typer CLI, and pluggable sanctioned alt-backends — **not an MVP.**

---

## 1. Executive Summary

Build a production-grade package whose first-class backend is Google's OWN official, sanctioned Colab tooling (google-colab-cli v0.5.x + colab-mcp v1.0.x, both Apache-2.0, agent-targeted, shipped 2026), wrapped behind a stable provider-abstraction layer, with a thin reverse-engineered /tun/m/* client retained as an internal, version-gated, OPT-IN escape hatch (not the default), and with Colab Enterprise/Vertex + Modal as fully-sanctioned production/overflow backends. The single most important fact across the adversarial verdicts is that the discouraged reverse-engineered transports (direct backend, cookie+SAPISIDHASH, browser/UI automation, in-page iframe) are not just fragile — they are strategically obsolete because Google now ships an official agent-control path that does exactly the headline goal (headless T4/L4/A100/H100 sessions, code/notebook/file ops). Owning the raw /tun/m/* contract means owning every silent breakage Google introduces while gaining nothing the official CLI doesn't already give. The second most important fact: for a paid Colab Pro target, ToS risk is MEDIUM/LOW, not high — the FAQ's bans on UI-bypass and remote control apply to free runtimes and are explicitly lifted on paid plans with a positive compute-unit balance; the real residual risk is opaque, no-recourse abuse-detection bans on sustained headless GPU usage. The package therefore treats Colab as the first-class backend but invests its durable engineering in a clean capability-detecting abstraction so the product survives Colab churn, abuse-detection bans, and Google interface changes by routing around them. This is a complete system: real auth lifecycle, runtime lifecycle, structured execution, durable file sync via Drive (user-OAuth, not service account), an MCP server, a Typer CLI, and pluggable sanctioned alt-backends — not an MVP.

---

## Table of Contents

- [0. Read this first — the strategic finding](#0-read-this-first--the-strategic-finding-that-shapes-everything)
- [1. Executive Summary](#1-executive-summary)
- [2. Architecture Decision Record](#2-architecture-decision-record)
- [3. System Architecture & Domain Model](#3-system-architecture--domain-model)
- [4. Authentication & Session Management](#4-authentication--session-management)
- [5. Transport Layer & Colab Connection](#5-transport-layer--colab-connection)
- [6. Execution Engine & Runtime Lifecycle](#6-execution-engine--runtime-lifecycle)
- [7. Notebook & File Synchronization](#7-notebook--file-synchronization)
- [8. Provider Abstraction & Fallback](#8-provider-abstraction--fallback)
- [9. Python SDK & CLI Surface](#9-python-sdk--cli-surface)
- [10. MCP Server for AI Agents](#10-mcp-server-for-ai-agents)
- [11. Reliability & Observability](#11-reliability--observability)
- [12. Security, Compliance & Account Safety](#12-security-compliance--account-safety)
- [13. Testing & Quality Strategy](#13-testing--quality-strategy)
- [14. Repository Structure, Packaging & Config](#14-repository-structure-packaging--config)
- [15. Consolidated Risk Register](#15-consolidated-risk-register)
- [16. Delivery Roadmap](#16-delivery-roadmap)
- [17. Open Decisions for the Owner](#17-open-decisions-for-the-owner)
- [Appendix A — Approach Viability Matrix](./DECISIONS.md)
- [Appendix B — Research Dossiers](./RESEARCH.md)

---

## 2. Architecture Decision Record

### 2.1 Decision

DECISION: Layer a sanctioned-primary transport over a capability-detecting provider abstraction, with Colab as the first-class backend accessed through Google's official tooling, NOT through a hand-rolled reverse-engineered client by default.

CHOSEN LAYERS (bottom to top):
1. Secret storage (CORE, verdict score 7): keyring (25.7.x) with per-account-email keying. Mitigations the verdict demands are baked in: chunk any blob >4KB across multiple Keychain items; treat the OS keychain as defense-in-depth, NOT a security boundary (any same-user Python process can read it after 'always allow'); ship a second backend abstraction (SecretService / Windows Credential Manager / age-encrypted file) so the macOS Keychain win generalizes to headless Linux servers and CI.

2. Auth (CORE for Colab path): mirror the SANCTIONED model — OAuth2 user credentials via the official google-colab-cli's loopback flow (the only confirmed-working path), NOT self-registered clients. Self-registered Desktop OAuth client + colaboratory scope is FALLBACK only (verdict: scope grantability unconfirmed, 7-day refresh-token death in Testing status). GCP ADC/service-account auth is the auth layer ONLY for the Enterprise/Vertex backend (verdict: rock-solid mechanism, wrong target for consumer Pro).

3. Transport / runtime allocation (CORE): PRIMARY = shell out to / vendor google-colab-cli behind a hard adapter interface with version pinning and a capability probe; treat it as a fast-moving dependency (v0.5.x, yanked releases, Python 3.13-only, no stable JSON mode confirmed, Google rejects external PRs). SECONDARY = colab-mcp browser bridge for human-in-the-loop interactive sessions (sanctioned, low ToS, but requires an open logged-in tab — explicitly NOT headless). OPT-IN ESCAPE HATCH (not default, disclosed-risk): a thin, version-gated direct /tun/m/* client + jupyter-kernel-client websocket exec + runtime-proxy-token lifecycle manager, used only when the user explicitly enables it and accepts the fragility; this is the genuinely-works-today path (verdicts confirm /tun/m/* endpoints, X-Colab-Runtime-Proxy-Token header-only auth, TooManyAssignmentsError, tokenExpiresInSeconds are all real in colab-vscode) but it is undocumented, drift-prone, and abuse-detection-exposed.

4. Code execution (CORE within whichever transport is active): standard Jupyter kernel exec over WebSocket via jupyter-kernel-client, with the CORRECTED auth recipe the verdict flagged — the runtime-proxy token is a HEADER-only credential (X-Colab-Runtime-Proxy-Token) distinct from the OAuth Bearer identity token, plus X-Goog-Colab-Tunnel and the X-Goog-Colab-Token XSRF header; do NOT send the proxy token three ways.

5. Notebook/file sync (CORE, but redesigned per verdict): durable artifacts go to Google Drive via USER-OAuth doing plain-blob .ipynb uploads to the human's My Drive (ownership stays with the human, counts against their quota) — NOT a service account (SA cannot own Google-native files → 403 storageQuotaExceeded) and NOT the GET-only /tun/m/ Contents API (uploads almost certainly fail). In-VM transient I/O uses the kernel-comms mechanism Google itself ships. All durable state externalized to Drive/GCS because runtimes are ephemeral.

6. Provider abstraction (CORE, verdict score 7 — 'single best strategic decision'): submit/status/logs/fetch/cancel with capability feature-detection (live-logs vs poll-then-fetch, interactive vs batch). Colab is the first-class node; Colab Enterprise/Vertex (sanctioned headless production, score 6.5) and Modal Sandboxes (score 8, gVisor-isolated, ideal for agent-generated code) are first-tier sanctioned backends. HF Jobs / Kaggle / RunPod-vast / hyperscaler jobs are registered but lower-priority fallbacks. Optional: papermill/nbclient adapter (score 6) for batch .ipynb-over-any-kernel.

7. AI-agent surface (CORE): an MCP server exposing the abstraction's verbs to agents, plus a Typer CLI for developers.

WHY EACH MAJOR LOSER WAS REJECTED:
- cookie-sapisidhash-auth / cookie-extraction-browser (scores 1.5/1.5, AVOID): DBSC reached GA in Chrome 146 (April 2026) and lands on macOS via Secure Enclave in Chrome 148 — it binds sessions to non-exportable hardware keys and structurally defeats cookie replay within the product's own ship window; ToS-prohibited; full-account-credential blast radius; documented March-2026 ban wave on autonomous agents using broad credentials.
- headless-browser-ui-automation / in-page-kernel-iframe-driving (scores 2/1, AVOID): CDP detection operates at the protocol layer independent of profile (the headline 'evades detection' pro is false); Playwright persistent-context + storageState is internally contradictory in current versions; the kernel API lives only in cross-origin sandboxed per-cell iframes you cannot script into; ToS-prohibited; obsoleted by the official CLI.
- tun-tunnel-header-proxy / jupyter-contents-api-filesync as a DEFAULT (scores 2.5/2, AVOID as foundation): Google deliberately routed its OWN colab-mcp through an authenticated browser bridge, NOT the x-colab-tunnel header — the strongest signal that path is the unsupported back door; the reverse-engineering source is ~6 years stale; the Contents API proxy is GET-only so uploads fail.
- oauth-borrowed-vscode-client / direct-backend-tun-assign as the DEFAULT (scores 3.5/3.5, FALLBACK): borrowed client_id is a single point of catastrophic failure you don't own; the colaboratory scope is confirmed NOT publicly grantable; Google removed this exact runtime code from colab-mcp 'for launch.' Retained ONLY as the opt-in escape hatch, never the default.
- hybrid-tiered-backends as the SPINE (score 4, FALLBACK): the tiering instinct is right but mis-cast as the product spine; only the Enterprise tier is genuinely headless+sanctioned, and it is a different product. Adopted as a routing strategy INSIDE the provider abstraction, not as the architecture's identity.

### 2.2 Why this wins

It is the only design that is simultaneously (1) faithful to the literal goal — programmatic control of Colab Pro with no website — by making Colab the first-class backend via Google's own sanctioned agent tooling; (2) honest about the dominant strategic fact in the verdicts — that hand-rolling reverse-engineered transports is obsolete now that an official agent CLI exists, so we wrap the official tool and keep the raw /tun/m/* client as a contained, opt-in escape hatch rather than the load-bearing default; and (3) survivable — the capability-detecting provider abstraction (the highest-rated strategic decision in the review, score 7, and 'exactly what this pattern is for') contains Colab's two irreducible risks (Google interface churn and opaque abuse-detection bans) behind a stable interface so the product keeps working by routing to Modal (8), Enterprise/Vertex (6.5), or other sanctioned backends when Colab degrades or an account is blocked. It corrects the specific technical errors the adversarial review caught: proxy-token is header-only (not Bearer, not query param); Drive sync must be user-OAuth plain-blob uploads to My Drive, never a service account writing native-MIME files; durable state must be externalized because runtimes are ephemeral; keyring is defense-in-depth not a boundary and needs a non-Mac backend. It also correctly de-risks ToS: by targeting paid Colab Pro and preferring the sanctioned CLI/MCP, it lands in the MEDIUM/LOW band the verdicts establish, with the residual abuse-detection risk explicitly surfaced to the user rather than hidden.

### 2.3 Primary stack

Python 3.11+ core (NOTE: the official google-colab-cli currently requires Python 3.13 — see risk register; the wrapper invokes it as a pinned external/vendored subprocess via an isolated interpreter/uv tool env so the core package's 3.11+ floor is preserved). Async via asyncio + httpx + websockets. pydantic v2 for all models (auth blobs, RuntimeProxyInfo, assignment/quota outcomes, provider capability descriptors, structured kernel outputs). Typer-based CLI for developers. An MCP server (FastMCP-style) exposing the provider-abstraction verbs (submit/status/logs/fetch/cancel + notebook/file ops) to AI agents. Packaged with pyproject + uv; secrets in the OS keychain via keyring with per-account-email keying AND a pluggable non-Mac backend (SecretService / Windows Credential Manager / age-encrypted file). Execution layer: jupyter-kernel-client for the Jupyter websocket protocol. File sync: google-api-python-client / google-auth for Drive (user-OAuth). Sanctioned backends: google-cloud-aiplatform (Vertex/Colab Enterprise), modal SDK; lower-priority huggingface_hub (HF Jobs), kaggle CLI, runpod/vastai SDKs registered behind the abstraction. nbclient/papermill optional for batch .ipynb adaptation. Seed stack accepted with no overrides except the documented Python-version interop note for the official CLI dependency.

---

## 3. System Architecture & Domain Model

This section defines the complete component topology of `colabctl`, the layered design, the canonical end-to-end data flow for *"run this code on a Colab GPU and stream results back"*, and the field-level domain model that every layer and every backend adapter speaks. It is the contract that the rest of the spec builds on: if a later section disagrees with the model here, this section wins.

### 1. Design Principles (the load-bearing decisions)

The architecture is the direct expression of the chosen-architecture verdicts. Six principles are non-negotiable and recur in every component:

1. **Sanctioned-primary, escape-hatch-secondary.** The default Colab transport is Google's own `google-colab-cli` (subprocess) and `colab-mcp` (browser bridge). The hand-rolled `/tun/m/*` direct client exists, but is *opt-in, version-gated, and disclosed-risk* — never the default. The product is *not* a reverse-engineering project that happens to wrap a CLI; it is a CLI/MCP wrapper that happens to carry a contained escape hatch.
2. **The provider abstraction is the spine, not Colab.** Every capability is expressed as `submit / status / logs / fetch / cancel + notebook/file ops` over a `Backend` interface with explicit capability feature-detection. Colab is the first-class node but it is *one node*. When Colab degrades or an account is banned, the same domain objects route to Modal, Vertex/Colab Enterprise, HF Jobs, etc.
3. **The proxy token is header-only.** `X-Colab-Runtime-Proxy-Token` is a distinct credential from the OAuth `Authorization: Bearer` identity token. We never send the proxy token as a Bearer or query param. This correction is baked into the `Kernel`/transport models.
4. **All durable state is externalized.** Runtimes are ephemeral (90-min idle, 12/24h hard cap, re-assignment loses VM state). Durable artifacts go to the human's **My Drive via user-OAuth plain-blob `.ipynb` upload** — never a service account writing native-MIME files.
5. **Keyring is defense-in-depth, not a security boundary.** Secret storage is pluggable (Keychain / SecretService / Windows Cred Manager / age-file), chunks blobs >4 KB, and is keyed per-account-email.
6. **Async-first, typed everywhere.** `asyncio` + `httpx` + `websockets`; **pydantic v2** for every wire/domain object so capability descriptors, quota outcomes, and structured kernel outputs are validated at the boundary.

### 2. Top-Level Component Diagram

```
                         ┌──────────────────────────────────────────────────────┐
                         │                  SURFACES (entry points)              │
                         │                                                        │
   developer ───────────►  colabctl.cli  (Typer)        colabctl.mcp (FastMCP)  ◄──── AI agent
                         │   `colab run/new/...`          submit/status/logs/...  │
                         └───────────────┬────────────────────────┬──────────────┘
                                         │  (both call the same library API)
                                         ▼                        ▼
                         ┌──────────────────────────────────────────────────────┐
                         │            colabctl.core  — ORCHESTRATION             │
                         │  Session lifecycle, Execution state machine, router,  │
                         │  capability negotiation, retry/keepalive, event bus   │
                         └───────────────┬───────────────────────┬──────────────┘
                                         │                        │
                  ┌──────────────────────┴───────┐        ┌───────┴──────────────────┐
                  ▼                              ▼        ▼                          ▼
        ┌───────────────────┐        ┌───────────────────────────┐        ┌──────────────────┐
        │ colabctl.providers│        │   colabctl.transport      │        │ colabctl.filesync│
        │  Backend registry │        │  (Colab-specific only)    │        │  Drive (user-    │
        │  + abstraction    │        │                           │        │  OAuth blob),    │
        │                   │        │  ┌─────────────────────┐  │        │  kernel-comms,   │
        │  ColabBackend ────┼───────►│  │ cli_adapter (PRIMARY)│ │        │  GCS adapter     │
        │  ModalBackend     │        │  │ mcp_bridge (SECONDARY)│ │        └────────┬─────────┘
        │  VertexBackend    │        │  │ tun_client (ESCAPE) ⚠ │ │                 │
        │  HFJobsBackend    │        │  └─────────┬───────────┘  │                 │
        │  KaggleBackend    │        │            │              │                 │
        │  RunpodBackend    │        │  ┌─────────▼───────────┐  │                 │
        │  …registered      │        │  │ kernel (jupyter-    │  │                 │
        └─────────┬─────────┘        │  │ kernel-client WS)   │  │                 │
                  │                  │  └─────────────────────┘  │                 │
                  │                  └─────────────┬─────────────┘                 │
                  │                                │                               │
                  ▼                                ▼                               ▼
        ┌───────────────────────────────────────────────────────────────────────────────┐
        │                         colabctl.auth  — CREDENTIAL LAYER                       │
        │  OAuth2 loopback (CLI-mirrored, PRIMARY) · ADC/SA (Vertex only) · provider keys │
        └───────────────────────────────────────┬───────────────────────────────────────┘
                                                 ▼
        ┌───────────────────────────────────────────────────────────────────────────────┐
        │                       colabctl.secrets  — SECRET STORAGE                        │
        │  keyring (Keychain) | SecretService | WinCredMan | age-file   (>4KB chunked)    │
        └───────────────────────────────────────────────────────────────────────────────┘

        cross-cutting: colabctl.models (pydantic v2)  ·  colabctl.config  ·  colabctl.errors  ·  colabctl.telemetry
        ⚠ = disclosed-risk, opt-in, version-gated. Never default.
```

### 3. Package / Module Layout

Real, importable paths. The repo is a single `uv`/`pyproject` package with the `core` floor at **Python 3.11+**; the official `google-colab-cli` (3.13-only) is invoked as an *isolated subprocess* (see §5.1), so it never constrains the core interpreter.

```
src/colabctl/
├── __init__.py
├── models/                      # pydantic v2 — the domain model (§9). Zero runtime deps on other layers.
│   ├── __init__.py
│   ├── core.py                  # Session, Runtime, Kernel, Notebook, Execution, Output, Artifact
│   ├── backend.py               # Backend descriptor, BackendCapabilities, GpuSpec, QuotaOutcome
│   ├── auth.py                  # AuthBlob, RuntimeProxyInfo, OAuthCredential
│   └── events.py                # ExecutionEvent, LogChunk, StatusChange (the streaming event union)
├── errors.py                    # exception hierarchy (§8)
├── config.py                    # ColabctlConfig (pydantic-settings), TOML + env + keyring resolution
├── secrets/                     # secret storage (defense-in-depth)
│   ├── base.py                  # SecretStore ABC
│   ├── keyring_store.py         # keyring backend + >4KB chunking
│   ├── secretservice_store.py
│   ├── wincred_store.py
│   └── age_file_store.py
├── auth/
│   ├── base.py                  # CredentialProvider ABC
│   ├── oauth_loopback.py        # CLI-mirrored OAuth2 loopback (PRIMARY for Colab + Drive)
│   ├── adc.py                   # google-auth ADC / service account (Vertex ONLY)
│   └── refresh.py               # refresh-token lifecycle + 7-day-death detection
├── transport/                   # COLAB-SPECIFIC transports (not the provider abstraction)
│   ├── base.py                  # ColabTransport ABC (assign/proxy-info/keepalive/unassign)
│   ├── cli_adapter.py           # PRIMARY: subprocess to google-colab-cli (isolated uv tool env)
│   ├── mcp_bridge.py            # SECONDARY: drives colab-mcp local websocket bridge
│   ├── tun_client.py            # ESCAPE HATCH (opt-in, version-gated): /tun/m/* httpx client
│   ├── proxy_token.py           # RuntimeProxyInfo lifecycle (refresh, expiry, re-assign)
│   └── kernel.py                # jupyter-kernel-client WS exec (header-only proxy token)
├── providers/                   # THE SPINE
│   ├── base.py                  # Backend ABC: submit/status/logs/fetch/cancel + nb/file ops
│   ├── registry.py              # BackendRegistry, capability-based routing
│   ├── colab.py                 # ColabBackend (wraps transport.*)
│   ├── modal.py                 # ModalBackend (Sandbox.create / Function(gpu=))
│   ├── vertex.py                # VertexBackend (notebookExecutionJobs)
│   ├── hf_jobs.py               # HFJobsBackend
│   ├── kaggle.py                # KaggleBackend (poll-then-fetch)
│   └── runpod.py                # RunpodBackend / vastai (registered, low priority)
├── filesync/
│   ├── base.py                  # FileSync ABC
│   ├── drive.py                 # user-OAuth plain-blob .ipynb to My Drive (CORE)
│   ├── kernel_comms.py          # in-VM transient I/O (google.colab.files mechanism)
│   └── gcs.py                   # GCS adapter (Vertex/Enterprise durable store)
├── notebook/
│   ├── papermill_adapter.py     # OPTIONAL batch .ipynb-over-any-kernel (nbclient)
│   └── ipynb.py                 # .ipynb (de)serialization, parameter injection
├── core/
│   ├── engine.py                # ColabctlEngine — the public library API facade
│   ├── session.py               # SessionManager — lifecycle FSM (§7)
│   ├── execution.py             # ExecutionEngine — submit→stream→collect FSM (§7)
│   ├── router.py                # picks Backend by capability + policy + health
│   ├── keepalive.py             # ~60s keepalive task; idle/lifetime re-assign watchdog
│   └── eventbus.py              # async pub/sub for ExecutionEvent streaming
├── cli/
│   └── app.py                   # Typer app
├── mcp/
│   └── server.py                # FastMCP server exposing the abstraction verbs
└── telemetry.py                 # structured logging, redaction, abuse-signal surfacing
```

### 4. Layered Design (bottom → top)

| Layer | Module(s) | Responsibility | Key external dep |
|-------|-----------|----------------|------------------|
| **L0 Secrets** | `secrets/*` | Encrypt-at-rest credential blobs; chunk >4 KB; per-account-email keying; pluggable non-Mac backends | `keyring` 25.7.x |
| **L1 Auth** | `auth/*` | Mint/refresh credentials. OAuth2 loopback (Colab+Drive, PRIMARY); ADC/SA (Vertex ONLY); detect 7-day refresh death | `google-auth`, stdlib loopback |
| **L2 Transport** | `transport/*` | *Colab-only* runtime allocation + proxy-token lifecycle + kernel WS. Three adapters behind `ColabTransport` | `google-colab-cli` (subprocess), `httpx`, `jupyter-kernel-client` |
| **L3 Runtime/Session** | `core/session.py`, `core/keepalive.py` | Allocate/hold/reclaim a `Runtime`; bind a `Session` to it; keepalive; re-assign on loss | — |
| **L4 Execution** | `core/execution.py`, `transport/kernel.py` | Drive cell/code execution over the active transport; produce `Output` stream; aggregate to `Execution` | `jupyter-kernel-client` |
| **L5 File-sync** | `filesync/*` | Durable artifacts → Drive (user-OAuth blob); transient I/O → kernel-comms; GCS for Vertex | `google-api-python-client` |
| **L6 Providers** | `providers/*` | Capability-detecting `Backend` abstraction; routing; *contains* L2–L5 behind a stable verb set | `modal`, `google-cloud-aiplatform`, `huggingface_hub`, `kaggle`, `runpod` |
| **L7 Surfaces** | `cli/`, `mcp/`, `core/engine.py` | Developer CLI (Typer) + agent MCP server (FastMCP) + library facade | `typer`, FastMCP |

The critical structural rule: **L2–L5 are private to `ColabBackend`.** The CLI/MCP/library surfaces talk *only* to L6 (`providers`). A non-Colab backend (Modal) provides its own L2–L5 internally. This is what makes the product survivable when Colab breaks.

### 5. Component Specifications

#### 5.1 Transport — `ColabTransport` ABC and the three adapters

```python
# transport/base.py
from abc import ABC, abstractmethod
from colabctl.models.core import Runtime
from colabctl.models.auth import RuntimeProxyInfo
from colabctl.models.backend import GpuSpec, QuotaOutcome

class ColabTransport(ABC):
    """Colab-specific runtime allocation. Implemented by cli_adapter / mcp_bridge / tun_client."""
    name: str
    is_headless: bool                # cli=True, mcp_bridge=False, tun=True

    @abstractmethod
    async def allocate(self, spec: GpuSpec, *, idempotency_key: str) -> Runtime: ...

    @abstractmethod
    async def proxy_info(self, runtime: Runtime) -> RuntimeProxyInfo:
        """Returns RuntimeProxyInfo incl. header-only proxy token + tokenExpiresInSeconds."""

    @abstractmethod
    async def keepalive(self, runtime: Runtime) -> None: ...

    @abstractmethod
    async def unassign(self, runtime: Runtime) -> None: ...

    @abstractmethod
    async def probe(self) -> "TransportProbe":
        """Capability + version probe run once at startup (pinned-version gate)."""
```

**5.1.1 `cli_adapter.py` (PRIMARY).** Shells out to `google-colab-cli` in an **isolated `uv tool` environment** (`uv tool run --from google-colab-cli==<pin> colab ...`) so its Python 3.13 requirement never touches the core 3.11+ runtime.

- A **hard version pin** + `probe()` capability check runs at import. If the installed CLI version is outside the supported range, the adapter raises `TransportUnsupportedError` and the router falls back (per policy).
- Because **no stable JSON mode is confirmed**, the adapter is built around a `CliOutputParser` with a *pinned grammar per CLI version*. Parsing is defensive: any unrecognized line is captured into `raw_stdout` on the resulting object and logged, never silently dropped. A parse miss on a *required* field raises `CliContractDriftError` (a hard failure that triggers fallback) rather than guessing.
- `allocate()` runs `colab new --gpu {T4|L4|A100|H100}`, parses the session/runtime identifiers, and constructs a `Runtime`.

**5.1.2 `mcp_bridge.py` (SECONDARY, human-in-the-loop).** Drives the official `colab-mcp` local websocket bridge. Sets `is_headless=False`, `BackendCapabilities.interactive=True`, `requires_browser_tab=True`. The bridge requires an open, logged-in Colab tab; the adapter surfaces `BridgeTabNotConnectedError` if the 60-second `fe_connected` window elapses, and `BridgeBusyError` (close code 1013) on a second concurrent client. Used for interactive sessions, never for unattended agents.

**5.1.3 `tun_client.py` (ESCAPE HATCH — opt-in, disclosed-risk).** A thin `httpx` client against `/tun/m/assign`, `/tun/m/runtime-proxy-token`, `/tun/m/assignments`, `/tun/m/unassign/{endpoint}`, `/tun/m/ccu-info`. Strips the `)]}'` XSSI prefix; performs the two-phase `X-Goog-Colab-Token` XSRF dance; sends `X-Goog-Colab-Tunnel: true`. **Gated by an explicit opt-in flag** (`config.colab.enable_escape_hatch = true` *and* a per-call `accept_unsanctioned_risk=True`) and a runtime version banner. Surfaces `TooManyAssignmentsError` (per-account assignment cap) and the `QuotaOutcome` enum. This is the genuinely-works-today path but it is undocumented and drift-prone; it is never selected by default routing.

#### 5.2 Proxy-token lifecycle — `transport/proxy_token.py`

A `RuntimeProxyManager` owns the `RuntimeProxyInfo` and refreshes it before `token_expires_in_seconds` elapses (refresh at 75% of TTL). Critical correctness rule encoded in `kernel.py`:

```python
# transport/kernel.py — the ONLY correct auth recipe
def _kernel_headers(proxy: RuntimeProxyInfo, identity_bearer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {identity_bearer}",       # OAuth identity — separate credential
        "X-Colab-Runtime-Proxy-Token": proxy.proxy_token,   # HEADER-ONLY. Never Bearer, never query.
        "X-Goog-Colab-Tunnel": "true",
        "X-Goog-Colab-Token": proxy.xsrf_token,             # XSRF
        "X-Goog-Colab-Client-Agent": proxy.client_agent,    # transport-specific agent string
    }
```

The manager exposes `async def with_fresh_token(self) -> RuntimeProxyInfo` used by the kernel client on every WS (re)connect. On `412 TooManyAssignmentsError` it does **not** retry blindly — it raises to the `SessionManager`, which decides between waiting, unassigning a stale assignment, or routing to another backend.

#### 5.3 Execution — `transport/kernel.py`

Standard Jupyter kernel exec over WebSocket via `jupyter-kernel-client`, pointed at `RuntimeProxyInfo.kernel_ws_url`, authenticated with the header recipe above. The client:

1. Ensures a kernel/session (`POST /api/kernels` through the proxy if the VM does not auto-provision; honors XSRF on POST).
2. Sends an `execute_request` and yields `Output` objects from `stream`, `execute_result`, `display_data`, and `error` messages.
3. Detects completion via the `idle` `status` message correlated to the originating `msg_id` (not via DOM, not via a fragile wall-clock timeout).
4. On WS drop, reconnects with a fresh proxy token and resumes monitoring by `msg_id`; if the kernel is gone, escalates to the `SessionManager` for re-assignment.

#### 5.4 Providers — the spine

```python
# providers/base.py
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from colabctl.models.core import Execution, Output, Artifact, Notebook
from colabctl.models.backend import BackendCapabilities, GpuSpec, SubmitSpec

class Backend(ABC):
    name: str

    @property
    @abstractmethod
    def capabilities(self) -> BackendCapabilities: ...

    @abstractmethod
    async def submit(self, spec: SubmitSpec) -> Execution: ...

    @abstractmethod
    async def status(self, execution_id: str) -> Execution: ...

    @abstractmethod
    async def logs(self, execution_id: str, *, follow: bool = False) -> AsyncIterator[Output]:
        """Live stream if capabilities.live_logs else poll-then-fetch shim."""

    @abstractmethod
    async def fetch(self, execution_id: str) -> list[Artifact]: ...

    @abstractmethod
    async def cancel(self, execution_id: str) -> None: ...

    # notebook/file verbs
    @abstractmethod
    async def run_notebook(self, nb: Notebook, params: dict, spec: GpuSpec) -> Execution: ...
    @abstractmethod
    async def put_file(self, src: bytes, dest: str) -> Artifact: ...
    @abstractmethod
    async def get_file(self, path: str) -> bytes: ...
```

`registry.py` holds an ordered, capability-tagged list of backends. `core/router.py` selects a backend by: (1) explicit user/agent override → (2) capability match against the request (`needs_gpu`, `needs_interactive`, `needs_live_logs`) → (3) health/circuit-breaker state → (4) policy priority (`colab > modal > vertex > hf_jobs > kaggle > runpod`, configurable). When `ColabBackend` reports unhealthy (ban, drift, quota), the router transparently routes the same `SubmitSpec` to the next capable backend and records a `RoutingDecision` on the `Execution`.

#### 5.5 File-sync — `filesync/drive.py` (CORE)

Durable `.ipynb` and result artifacts upload as **plain blobs to the human's My Drive via user-OAuth** (`google-api-python-client` `files().create(media_body=..., body={"name": ..., "parents": [...]})` with a *non-native* MIME so ownership stays with the human and counts against their quota). The implementation explicitly **refuses** a service-account credential for `.ipynb` writes (`ServiceAccountOwnershipError` with a remediation message) because an SA cannot own Google-native files. Transient in-VM I/O uses `kernel_comms.py` (the `google.colab.files` mechanism Google ships).

### 6. End-to-End Data Flow — "Run this code on a Colab GPU and stream results back"

The canonical happy path through the layers. The agent calls the MCP verb `submit` (or the dev runs `colab run script.py --gpu T4 --follow`). The same `ColabctlEngine.run()` is invoked underneath.

```
Caller (CLI/MCP)
  │  RunRequest{code, gpu=T4, stream=True, backend=auto}
  ▼
ColabctlEngine.run()
  │
  ├─1► Router.select(req) ───────────────► picks ColabBackend (capable: gpu, exec, live_logs)
  │
  ├─2► auth: CredentialProvider.get("colab", account_email)
  │       └─ secrets.SecretStore.read() → AuthBlob (reassemble >4KB chunks)
  │       └─ refresh if access token stale; abort+surface if 7-day refresh death detected
  │
  ├─3► SessionManager.acquire(GpuSpec(T4))
  │       └─ ColabTransport(cli_adapter).allocate()  →  subprocess `colab new --gpu T4`
  │             └─ parse → Runtime{id, endpoint, gpu=T4, state=ALLOCATING}
  │       └─ poll until Runtime.state == READY  (handle QuotaOutcome: DENYLISTED/QUOTA_* → raise/route)
  │       └─ RuntimeProxyManager.proxy_info(runtime) → RuntimeProxyInfo{proxy_token, ttl, ws_url, xsrf}
  │       └─ start KeepAlive task (~60s)  + idle/lifetime watchdog
  │       └─ Session{id, runtime, kernel=None, state=ACTIVE}
  │
  ├─4► ExecutionEngine.submit(session, code)
  │       └─ kernel.ensure_kernel(proxy)  → Kernel{id, ws connected, header-only token}
  │       └─ send execute_request(msg_id) over WS
  │       └─ Execution{id, session_id, state=RUNNING, msg_id}
  │
  ├─5► STREAM:  async for msg in kernel.iter(msg_id):
  │       stream/execute_result/display_data/error → Output{...}
  │       │      └─ EventBus.publish(ExecutionEvent(output)) ──► back to caller (SSE / MCP notification / CLI tail)
  │       status==idle(msg_id) → break
  │       (WS drop → reconnect w/ fresh proxy token, resume by msg_id;
  │        kernel-dead → SessionManager.reassign() + replay-from-checkpoint policy)
  │
  ├─6► COLLECT: Execution.state = SUCCEEDED|FAILED; aggregate Outputs;
  │       capture .ipynb → filesync.drive.put_notebook(nb)  → Artifact{drive_file_id, ...}
  │
  └─7► RETURN Execution{outputs[], artifacts[], routing_decision, quota_outcome}
          (KeepAlive stopped; Session held per keep_warm policy or unassigned)
```

**Sequence (compact):**

| # | Actor | Operation | Key object produced |
|---|-------|-----------|---------------------|
| 1 | Router | capability match | `RoutingDecision` |
| 2 | Auth/Secrets | resolve + refresh creds | `AuthBlob`, `OAuthCredential` |
| 3 | SessionManager + Transport | allocate runtime, proxy info, keepalive | `Runtime`, `RuntimeProxyInfo`, `Session` |
| 4 | ExecutionEngine + Kernel | open kernel, send code | `Kernel`, `Execution` |
| 5 | Kernel → EventBus | stream messages | `Output`, `ExecutionEvent` |
| 6 | ExecutionEngine + FileSync | finalize + persist | `Artifact` |
| 7 | Engine | return result | final `Execution` |

### 7. State Machines

**Session (Runtime) FSM** — `core/session.py`:

```
REQUESTED → ALLOCATING → READY → ACTIVE → (IDLE) → ACTIVE ...
                │           │       │         │
                │           │       │         └─(idle >~90m)→ RECLAIMED → [reassign?] → ALLOCATING
                │           │       └─(lifetime 12/24h)──────→ EXPIRED   → [reassign?] → ALLOCATING
                │           └─(QUOTA_DENIED/DENYLISTED)──────→ DENIED (terminal for this backend → route)
                └─(TooManyAssignments 412)────────────────────→ BLOCKED (await/unassign/route)
ACTIVE/IDLE → UNASSIGNED (explicit release) → terminal
```

**Execution FSM** — `core/execution.py`:

```
PENDING → SUBMITTED → RUNNING → SUCCEEDED
                         │  └────→ FAILED        (error message / non-zero)
                         │  └────→ CANCELLED     (cancel())
                         └───────→ INTERRUPTED   (kernel/session lost)
INTERRUPTED → (retry policy) → RESUBMITTED → RUNNING ...   (state externalized; re-run from checkpoint)
```

Re-assignment loses VM state by definition, so `INTERRUPTED → RESUBMITTED` is only attempted when the `SubmitSpec.checkpoint_policy` allows it and durable inputs live in Drive/GCS. Otherwise the `Execution` ends `INTERRUPTED` and the caller is told the runtime was reclaimed.

### 8. Error Model — `errors.py`

```python
class ColabctlError(Exception): ...                       # base

class SecretStoreError(ColabctlError): ...
class AuthError(ColabctlError): ...
class RefreshTokenExpiredError(AuthError): ...            # 7-day Testing-status death
class ServiceAccountOwnershipError(AuthError): ...        # SA cannot own native .ipynb

class TransportError(ColabctlError): ...
class TransportUnsupportedError(TransportError): ...       # CLI version outside pin range
class CliContractDriftError(TransportError): ...           # required field missing in stdout
class BridgeTabNotConnectedError(TransportError): ...      # colab-mcp 60s fe_connected window
class BridgeBusyError(TransportError): ...                 # close code 1013
class ProxyTokenExpiredError(TransportError): ...

class RuntimeAllocationError(ColabctlError): ...
class TooManyAssignmentsError(RuntimeAllocationError): ... # 412 per-account cap
class QuotaDeniedError(RuntimeAllocationError): ...        # QUOTA_* outcome
class AccountDenylistedError(RuntimeAllocationError): ...  # DENYLISTED / suspected-abuse block

class KernelError(ColabctlError): ...
class KernelDisconnectedError(KernelError): ...
class ExecutionFailedError(ColabctlError): ...
class ExecutionInterruptedError(ColabctlError): ...

class BackendUnavailableError(ColabctlError): ...          # triggers router fallback
class NoCapableBackendError(ColabctlError): ...
```

`AccountDenylistedError` is treated specially: it is **surfaced to the user as the disclosed abuse-detection risk**, marks the Colab backend unhealthy in the registry (circuit-breaker open), and routes subsequent work to a sanctioned alternative (Modal/Vertex) rather than retrying Colab.

### 9. Core Domain Model (pydantic v2)

All wire and orchestration objects. `model_config = ConfigDict(frozen=True, extra="forbid")` for value objects; mutable state objects use `extra="forbid"` only. Enums use `StrEnum`. Field aliases mirror the upstream JSON (`PostAssignmentResponse`) so we round-trip cleanly.

```python
# models/backend.py
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field

class GpuType(StrEnum):
    NONE = "NONE"; T4 = "T4"; L4 = "L4"; A100 = "A100"; H100 = "H100"
    TPU_V2_8 = "V2-8"; TPU_V5E_1 = "V5E-1"

class MachineShape(StrEnum):
    STANDARD = "STANDARD"; HIGH_RAM = "HIGH_RAM"

class GpuSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    gpu: GpuType = GpuType.T4
    count: int = 1
    machine_shape: MachineShape = MachineShape.STANDARD
    region_hint: str | None = None

class QuotaOutcomeKind(StrEnum):
    SUCCESS = "SUCCESS"; DENYLISTED = "DENYLISTED"
    QUOTA_DENIED = "QUOTA_DENIED"; QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TOO_MANY_ASSIGNMENTS = "TOO_MANY_ASSIGNMENTS"

class QuotaOutcome(BaseModel):
    """Result of an allocation attempt. NOTE: enum values below are plausible-but-unverified
    against primary sources (see risk register); the parser tolerates unknown kinds via UNKNOWN."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: QuotaOutcomeKind
    compute_units_remaining: float | None = None
    detail: str | None = None
    granted_gpu: GpuType | None = None          # may be a SILENT downgrade from requested

class BackendCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    supports_gpu: bool
    gpu_types: tuple[GpuType, ...] = ()
    interactive: bool = False                   # cell-by-cell REPL state
    batch: bool = True
    live_logs: bool = True                      # else poll-then-fetch
    headless: bool = True                       # mcp_bridge → False
    requires_browser_tab: bool = False
    sanctioned: bool = True                     # tun escape hatch → False
    owns_durable_storage: bool = False          # Vertex(GCS)=True, Colab=False
    max_session_seconds: int | None = None

class SubmitSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str | None = None
    notebook_ref: str | None = None             # path or drive_file_id
    params: dict[str, object] = Field(default_factory=dict)
    gpu: GpuSpec = GpuSpec()
    backend: str = "auto"                        # "auto" | concrete name
    stream: bool = True
    needs_interactive: bool = False
    checkpoint_policy: str = "none"              # none|drive|gcs
    accept_unsanctioned_risk: bool = False       # gate for tun escape hatch
    idempotency_key: str | None = None
```

```python
# models/auth.py
from datetime import datetime
from pydantic import BaseModel, ConfigDict, SecretStr, Field

class OAuthCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_email: str
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: datetime
    scopes: tuple[str, ...]
    client_id: str                              # CLI-mirrored loopback client
    is_testing_status: bool = False             # → 7-day refresh death risk flag

class RuntimeProxyInfo(BaseModel):
    """Mirrors PostAssignmentResponse / runtime-proxy-token. Proxy token is HEADER-ONLY."""
    model_config = ConfigDict(extra="forbid")
    proxy_token: SecretStr                      # X-Colab-Runtime-Proxy-Token (header only!)
    token_expires_in_seconds: int = Field(alias="tokenExpiresInSeconds")
    issued_at: datetime
    kernel_ws_url: str
    contents_url: str | None = None
    xsrf_token: SecretStr                       # X-Goog-Colab-Token
    client_agent: str = "colabctl"

    @property
    def expires_at(self) -> datetime: ...

class AuthBlob(BaseModel):
    """What lands in the SecretStore (chunked if >4KB)."""
    model_config = ConfigDict(extra="forbid")
    account_email: str
    oauth: OAuthCredential | None = None
    provider_keys: dict[str, SecretStr] = Field(default_factory=dict)  # modal/hf/kaggle/runpod
    schema_version: int = 1
```

```python
# models/core.py
from datetime import datetime
from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field
from colabctl.models.backend import GpuType, QuotaOutcome
from colabctl.models.auth import RuntimeProxyInfo

class RuntimeState(StrEnum):
    REQUESTED="REQUESTED"; ALLOCATING="ALLOCATING"; READY="READY"; ACTIVE="ACTIVE"
    IDLE="IDLE"; RECLAIMED="RECLAIMED"; EXPIRED="EXPIRED"; DENIED="DENIED"
    BLOCKED="BLOCKED"; UNASSIGNED="UNASSIGNED"

class Runtime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    backend: str                                # "colab" | "modal" | ...
    transport: str | None = None               # cli | mcp_bridge | tun (Colab only)
    endpoint: str | None = None                # assignment endpoint id
    gpu: GpuType
    state: RuntimeState = RuntimeState.REQUESTED
    quota: QuotaOutcome | None = None
    allocated_at: datetime | None = None
    max_lifetime_seconds: int | None = None     # 12h free / 24h Pro (community-observed)
    idle_deadline: datetime | None = None

class KernelState(StrEnum):
    STARTING="STARTING"; IDLE="IDLE"; BUSY="BUSY"; DEAD="DEAD"; DISCONNECTED="DISCONNECTED"

class Kernel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    session_id: str
    runtime_id: str
    ws_url: str
    state: KernelState = KernelState.STARTING
    proxy_info: RuntimeProxyInfo                # carries header-only token

class SessionState(StrEnum):
    ACTIVE="ACTIVE"; IDLE="IDLE"; LOST="LOST"; CLOSED="CLOSED"

class Session(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    backend: str
    runtime: Runtime
    kernel: Kernel | None = None
    state: SessionState = SessionState.ACTIVE
    created_at: datetime
    keep_warm: bool = False

class OutputKind(StrEnum):
    STDOUT="stdout"; STDERR="stderr"; RESULT="execute_result"
    DISPLAY="display_data"; ERROR="error"

class Output(BaseModel):
    """One Jupyter output message, normalized."""
    model_config = ConfigDict(extra="forbid")
    kind: OutputKind
    msg_id: str                                 # correlates to the execute_request
    seq: int                                    # monotonic per execution
    text: str | None = None
    data: dict[str, object] | None = None       # MIME bundle (text/plain, image/png, ...)
    ename: str | None = None                    # error name/value/traceback
    evalue: str | None = None
    traceback: tuple[str, ...] | None = None
    ts: datetime

class ExecutionState(StrEnum):
    PENDING="PENDING"; SUBMITTED="SUBMITTED"; RUNNING="RUNNING"
    SUCCEEDED="SUCCEEDED"; FAILED="FAILED"; CANCELLED="CANCELLED"; INTERRUPTED="INTERRUPTED"

class RoutingDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    requested_backend: str
    selected_backend: str
    reason: str                                 # "explicit"|"capability"|"fallback:colab_unhealthy"
    fallback_chain: tuple[str, ...] = ()

class Execution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    session_id: str | None = None
    backend: str
    state: ExecutionState = ExecutionState.PENDING
    msg_id: str | None = None
    outputs: list[Output] = Field(default_factory=list)
    artifacts: list["Artifact"] = Field(default_factory=list)
    routing: RoutingDecision | None = None
    quota: QuotaOutcome | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    error: str | None = None

class ArtifactKind(StrEnum):
    NOTEBOOK="notebook"; FILE="file"; CHECKPOINT="checkpoint"

class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ArtifactKind
    name: str
    location: str                               # "drive://<file_id>" | "gcs://..." | "vm:/path"
    drive_file_id: str | None = None
    mime: str = "application/octet-stream"       # plain blob; never native colab MIME on upload
    size_bytes: int | None = None
    owned_by_user: bool = True                   # MUST be True for Drive .ipynb

class Notebook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    cells: list[dict]                            # nbformat cells
    metadata: dict = Field(default_factory=dict)
    source_ref: str | None = None               # drive_file_id or path
```

```python
# models/events.py — the streaming union returned by Backend.logs(follow=True)
from pydantic import BaseModel, ConfigDict
from colabctl.models.core import Output, ExecutionState

class StatusChange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str; old: ExecutionState; new: ExecutionState

class ExecutionEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    execution_id: str
    output: Output | None = None
    status: StatusChange | None = None          # exactly one of output/status set
```

### 10. Configuration — `config.py`

```toml
# ~/.config/colabctl/config.toml  (also overridable by COLABCTL_* env vars)
[core]
default_backend = "auto"
account_email   = "iris@analyticsandsociety.com"
secret_backend  = "auto"          # auto|keyring|secretservice|wincred|age

[colab]
transport             = "cli"     # cli|mcp_bridge   (tun is NOT selectable here)
cli_version_pin       = "0.5.7"   # exact pin; outside range → TransportUnsupportedError
cli_isolated_env      = true      # uv tool run isolation (preserves 3.11+ core floor)
keepalive_seconds     = 60
proxy_refresh_ratio   = 0.75
enable_escape_hatch   = false     # MUST be true AND per-call accept_unsanctioned_risk=True

[router]
fallback_chain = ["colab", "modal", "vertex", "hf_jobs", "kaggle", "runpod"]
circuit_breaker_cooldown_seconds = 900

[drive]
parent_folder_id = null           # null → My Drive root
require_user_oauth = true         # refuse service-account .ipynb writes

[vertex]                          # ONLY layer that uses ADC/service-account
project = null
region  = "us-central1"
gcs_output_uri = null
```

`ColabctlConfig` is a `pydantic-settings` model; resolution order is **explicit arg > env (`COLABCTL_*`) > TOML > defaults**, with secrets always pulled from the `SecretStore`, never from the TOML file.

### 11. Edge Cases & Failure Handling (architecture-specific)

| Edge case | Where handled | Behavior |
|-----------|---------------|----------|
| CLI version drift / yanked release | `cli_adapter.probe()` | `TransportUnsupportedError` → router fallback; never parse an unknown version |
| CLI stdout format change (no JSON mode) | `CliOutputParser` | unknown lines → `raw_stdout`; missing required field → `CliContractDriftError` → fallback |
| Proxy token sent wrongly | `kernel._kernel_headers` | enforced single header path; unit-tested that token never appears as Bearer/query |
| 7-day refresh-token death (Testing status) | `auth/refresh.py` | detect on refresh failure → `RefreshTokenExpiredError` with re-consent guidance |
| `412 TooManyAssignmentsError` | `proxy_token` / `SessionManager` | unassign stale → retry once → else route to alt backend |
| DENYLISTED / suspected abuse | `RuntimeAllocationError` handling | `AccountDenylistedError`, mark Colab unhealthy, surface disclosed risk, route away |
| Runtime reclaimed mid-run (idle/lifetime) | Session FSM + Execution FSM | `INTERRUPTED`; resubmit only if `checkpoint_policy != none` and inputs in Drive/GCS |
| WS drop, kernel alive | `kernel.iter()` | reconnect w/ fresh proxy token, resume by `msg_id` |
| Service account given for Drive `.ipynb` | `filesync/drive.py` | `ServiceAccountOwnershipError` (SA cannot own native files) before any upload |
| Secret blob >4 KB (cookie/large token) | `keyring_store.py` | chunk across multiple Keychain items; reassemble on read |
| Headless host but transport=mcp_bridge | `mcp_bridge.probe()` | refuse (`requires_browser_tab`); router selects `cli` or alt backend |
| colab-mcp tab not connected in 60s | `mcp_bridge` | `BridgeTabNotConnectedError` |
| Silent GPU downgrade (requested A100, got T4) | `QuotaOutcome.granted_gpu` | compare to request; warn + record on `Execution`; do not fail unless `strict_gpu` |
| Escape hatch used without opt-in | `tun_client` | `PermissionError`/`TransportUnsupportedError`; both config flag and per-call flag required |
| Backend SDK API churn (Modal/HF/etc.) | each `providers/*` adapter | pinned SDK + capability probe; degrade behind `BackendCapabilities`, not crash |

### 12. Why this topology survives

The product is named after its most fragile dependency (Colab), so the architecture deliberately makes Colab *replaceable*: the surfaces and the domain model never mention Colab internals; they speak only `Session/Runtime/Execution/Output/Artifact/Backend`. Colab's two irreducible risks — Google interface churn and opaque abuse bans — are confined to `transport/*` and `providers/colab.py`, behind the `Backend` ABC. When either risk fires, the router re-emits the identical `SubmitSpec` against Modal or Vertex and the caller's code is unchanged. That containment, plus the corrected header-only auth, the user-OAuth Drive sync, and the externalized-durable-state rule, is what turns a fast-moving, partly-undocumented surface into a production-grade package.

### 3.x Key decisions

- Provider abstraction (Backend ABC: submit/status/logs/fetch/cancel + nb/file ops) is the architectural spine; Colab is one first-class node, not the architecture's identity. Surfaces and the domain model never reference Colab internals, so the same SubmitSpec routes to Modal/Vertex when Colab degrades or an account is banned.
- Layered design L0 secrets -> L1 auth -> L2 transport -> L3 runtime/session -> L4 execution -> L5 file-sync -> L6 providers -> L7 surfaces, with the hard rule that L2-L5 are private to ColabBackend; CLI/MCP/library talk ONLY to L6.
- Three pluggable Colab transports behind ColabTransport ABC: cli_adapter (PRIMARY, subprocess to google-colab-cli in an isolated uv tool env so the 3.13 requirement never constrains the 3.11+ core), mcp_bridge (SECONDARY, human-in-the-loop), tun_client (ESCAPE HATCH, double-gated by config flag + per-call accept_unsanctioned_risk, never default-routed).
- Proxy-token correctness is encoded in the model and a single _kernel_headers function: X-Colab-Runtime-Proxy-Token is header-only and distinct from the OAuth Authorization: Bearer identity token; plus X-Goog-Colab-Tunnel and X-Goog-Colab-Token XSRF. Unit-tested that the proxy token never appears as Bearer or query param.
- Durable artifacts persist as plain-blob .ipynb uploads to the human's My Drive via user-OAuth (ownership stays with the human); filesync/drive.py actively refuses service-account credentials for .ipynb writes (ServiceAccountOwnershipError) because an SA cannot own Google-native files.
- Full pydantic v2 domain model (Session, Runtime, Kernel, Notebook, Execution, Output, Artifact, Backend/BackendCapabilities, GpuSpec, QuotaOutcome, RuntimeProxyInfo, RoutingDecision) with explicit Runtime/Session/Execution state machines, frozen value objects, extra='forbid', and JSON aliases mirroring PostAssignmentResponse/tokenExpiresInSeconds.
- Runtimes are ephemeral by contract: all durable state externalized to Drive/GCS; INTERRUPTED->RESUBMITTED is only attempted when checkpoint_policy != none and inputs live in durable storage.
- DENYLISTED/suspected-abuse blocks are surfaced to the user as the disclosed abuse-detection risk, open a circuit breaker on the Colab backend, and route subsequent work to a sanctioned alternative rather than retrying Colab.

### 3.y Section risks

- No confirmed stable JSON output mode in google-colab-cli forces stdout parsing in cli_adapter; mitigated by per-version pinned grammar + CliContractDriftError on missing required fields, but cosmetic CLI output changes (v0.5.x, yanked releases) can still break the primary transport and trigger fallback.
- QuotaOutcome enum values (DENYLISTED/QUOTA_*) and the literal 412 TooManyAssignments HTTP binding are plausible-but-unverified against primary sources; the model tolerates UNKNOWN kinds, but hard branching on these specifics is fragile and may need a validation spike.
- Idle (~90 min) and lifetime (12h free / 24h Pro) limits are community-observed, not contractual, so the keepalive/re-assign watchdog timing is heuristic and will misfire when Google tightens limits during peak demand.
- OAuth path inherits the 7-day refresh-token death when the consent app is in Testing status; auth/refresh.py can detect and surface it but cannot prevent forced re-consent, which is hostile to fully unattended agents.
- The mcp_bridge (colab-mcp) is structurally not headless (requires an open logged-in browser tab, single-connection, 60s fe_connected window), so it cannot satisfy the unattended-agent contract; routing must correctly demote it to interactive-only or the agent surface silently degrades.
- The tun_client escape hatch rides undocumented /tun/m/* internals Google has removed from its own repo 'for launch'; even gated and opt-in, enabling it exposes account-ban risk and ongoing reverse-engineering maintenance burden that the team owns alone.
- Each non-Colab backend (Modal, Vertex, HF Jobs, Kaggle, RunPod) has an independently drifting SDK; the abstraction must track 5-6 moving dependencies plus pinned google-colab-cli, and capability-parity gaps (e.g. Kaggle poll-then-fetch only, no live logs) push branching complexity onto the router and every caller.
- Cross-interpreter boundary (3.11+ core invoking a 3.13-only CLI via uv tool subprocess) adds an operational dependency on uv being present and correctly provisioned on the host; a missing or misconfigured isolated env breaks the PRIMARY Colab transport at runtime.

---

## 4. Authentication & Session Management

This section specifies the `colabctl.auth` subsystem: how the package obtains, validates, persists, refreshes, and invalidates the credentials required by every transport, how it stores those credentials securely, and how it supports multiple Google accounts behind a single stable interface. The defining architectural constraint is that **authentication is per-transport, not global.** Colab's sanctioned path (the official `google-colab-cli`) owns its own OAuth loopback flow and token file; the Drive sync path needs user-OAuth with a Drive scope owned by *us*; the Vertex/Enterprise backend uses GCP ADC/service accounts; and the opt-in escape hatch (direct `/tun/m/*`) needs a header-only runtime-proxy token that is **not** an OAuth Bearer token. The subsystem's job is to present all of these as one `AuthProvider` interface while keeping the per-transport credential mechanics honest and isolated.

A second non-negotiable: **the OS keychain is defense-in-depth, not a security boundary** (verdict score 7 makes this explicit). Any same-user Python process can read a credential after "always allow." We store secrets in `keyring` to avoid plaintext-on-disk and git leaks, we chunk blobs over the ~4 KB Keychain soft limit, and we ship a non-Mac backend so the design generalizes to headless Linux/CI. We never claim it protects against a local attacker who is already the user.

### 1. Module layout

```
src/colabctl/auth/
├── __init__.py              # re-exports AuthProvider, get_auth_provider(), AccountProfile
├── base.py                  # AuthProvider ABC, Credential models, enums, exceptions
├── registry.py              # provider factory + capability registry keyed by TransportKind
├── store/
│   ├── __init__.py
│   ├── keychain.py          # KeychainSecretStore (keyring wrapper, chunking, ACL handling)
│   ├── backends.py          # backend selection: macOS Keychain / SecretService / WinCred / age-file
│   └── agefile.py           # AgeEncryptedFileStore (headless Linux / CI fallback)
├── profiles.py              # AccountProfile, ProfileManager, profiles.toml on-disk index
├── providers/
│   ├── __init__.py
│   ├── colab_cli.py         # ColabCliAuthProvider  (CORE: sanctioned loopback via official CLI)
│   ├── drive_oauth.py       # DriveOAuthProvider     (CORE: user-OAuth, Drive scope, our client)
│   ├── gcp_adc.py           # GcpAdcAuthProvider     (CORE for Vertex/Enterprise backend only)
│   ├── runtime_proxy.py     # RuntimeProxyAuthProvider (ESCAPE HATCH: header-only proxy token)
│   └── browser_cookie.py    # BrowserCookieAuthProvider (DISABLED-by-default, AVOID-tier, gated)
├── session.py               # SessionManager: persist/resume/invalidate, refresh scheduling
├── lock.py                  # cross-process file lock for token files & refresh races
└── models.py                # pydantic v2 RuntimeProxyInfo, TokenBundle, OAuthClientConfig, etc.
```

### 2. Core enums and pydantic v2 models (`auth/models.py`, `auth/base.py`)

```python
from __future__ import annotations
import time
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, Field, SecretStr, field_validator


class TransportKind(StrEnum):
    COLAB_CLI = "colab_cli"            # sanctioned primary
    COLAB_MCP = "colab_mcp"            # browser-bridge, human-in-the-loop
    RUNTIME_PROXY = "runtime_proxy"    # opt-in /tun/m/* escape hatch
    DRIVE = "drive"                    # Google Drive user-OAuth file sync
    VERTEX = "vertex"                  # Colab Enterprise / Vertex AI (ADC/SA)
    MODAL = "modal"                    # token-id/secret
    HF_JOBS = "hf_jobs"
    KAGGLE = "kaggle"


class CredentialKind(StrEnum):
    OAUTH_USER = "oauth_user"          # refresh+access token pair (Colab CLI, Drive)
    SERVICE_ACCOUNT = "service_account"
    ADC = "adc"
    RUNTIME_PROXY_TOKEN = "runtime_proxy_token"  # HEADER-only, runtime-scoped
    BROWSER_SESSION = "browser_session"          # cookie blob (AVOID-tier)
    STATIC_TOKEN = "static_token"                # Modal/HF/Kaggle api keys


class AuthState(StrEnum):
    VALID = "valid"
    EXPIRED_REFRESHABLE = "expired_refreshable"   # access token dead, refresh ok
    EXPIRED_TERMINAL = "expired_terminal"          # refresh token dead -> re-consent
    REVOKED = "revoked"
    NOT_AUTHENTICATED = "not_authenticated"
    INVALID = "invalid"                            # malformed / wrong account


class TokenBundle(BaseModel):
    """OAuth user-credential bundle. Persisted (encrypted) in the secret store."""
    model_config = {"frozen": False}

    account_email: str
    kind: CredentialKind = CredentialKind.OAUTH_USER
    access_token: SecretStr
    refresh_token: SecretStr | None = None
    token_uri: str = "https://oauth2.googleapis.com/token"
    client_id: str
    client_secret: SecretStr | None = None
    scopes: list[str] = Field(default_factory=list)
    # Absolute UNIX epoch seconds. Never trust expires_in deltas across persistence.
    expiry_epoch: float | None = None
    # Provenance: which provider minted this, so the right refresh path is used.
    issued_by: TransportKind
    obtained_at_epoch: float = Field(default_factory=time.time)

    @field_validator("scopes")
    @classmethod
    def _dedupe_scopes(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    def is_access_expired(self, skew_seconds: int = 120) -> bool:
        if self.expiry_epoch is None:
            return True
        return time.time() >= (self.expiry_epoch - skew_seconds)


class RuntimeProxyInfo(BaseModel):
    """The /tun/m/assign response surface (escape hatch). CORRECTED auth model:
    `proxy_token` is sent ONLY as the X-Colab-Runtime-Proxy-Token header. It is NOT
    an OAuth Bearer token and MUST NOT be placed in Authorization or any query param."""
    model_config = {"frozen": True}

    endpoint: str                       # runtimeProxyInfo.url
    proxy_token: SecretStr              # X-Colab-Runtime-Proxy-Token (header-only)
    token_expires_in_seconds: int      # tokenExpiresInSeconds at fetch time
    fetched_at_epoch: float = Field(default_factory=time.time)
    # The XSRF + tunnel headers the real client sends alongside.
    requires_tunnel_header: bool = True            # X-Goog-Colab-Tunnel: true
    xsrf_token: SecretStr | None = None            # X-Goog-Colab-Token

    def expires_at_epoch(self) -> float:
        return self.fetched_at_epoch + self.token_expires_in_seconds

    def is_expired(self, skew_seconds: int = 60) -> bool:
        return time.time() >= (self.expires_at_epoch() - skew_seconds)


class OAuthClientConfig(BaseModel):
    """How a provider sources its OAuth client identity."""
    client_id: str
    client_secret: SecretStr | None = None
    redirect_kind: Literal["loopback", "delegated"] = "loopback"
    auth_uri: str = "https://accounts.google.com/o/oauth2/auth"
    token_uri: str = "https://oauth2.googleapis.com/token"
    use_pkce: bool = True               # S256
```

### 3. The `AuthProvider` abstraction (`auth/base.py`)

This is the single stable contract the rest of the package (transports, MCP server, CLI) depends on. Every concrete provider maps a transport's idiosyncratic auth onto these verbs. All methods are `async` (the core is asyncio/httpx/websockets); synchronous library calls (e.g. `google-auth` refresh, `keyring`) are wrapped with `asyncio.to_thread`.

```python
import abc
from collections.abc import Sequence


class AuthProvider(abc.ABC):
    """One instance per (TransportKind, account_email). Stateless w.r.t. network
    where possible; durable state lives in the SecretStore + SessionManager."""

    transport: TransportKind
    credential_kind: CredentialKind

    def __init__(self, profile: "AccountProfile", store: "SecretStore",
                 sessions: "SessionManager", *, config: dict | None = None) -> None: ...

    # --- Capability feature-detection (mirrors the provider abstraction) ---
    @property
    @abc.abstractmethod
    def supports_headless(self) -> bool:
        """False for COLAB_MCP (needs open browser tab) and BROWSER_COOKIE."""

    @property
    @abc.abstractmethod
    def supports_unattended_refresh(self) -> bool:
        """False when re-consent is interactive (Testing-status 7-day death)."""

    @property
    @abc.abstractmethod
    def required_scopes(self) -> Sequence[str]: ...

    # --- Lifecycle ---
    @abc.abstractmethod
    async def authenticate(self, *, interactive: bool = True,
                           force: bool = False) -> TokenBundle:
        """Run the first-time flow (loopback OAuth, SA load, cookie extract).
        Persists the result via the store. Raises InteractiveAuthRequired in a
        headless context if interactive=False and no valid credential exists."""

    @abc.abstractmethod
    async def get_state(self) -> AuthState:
        """Cheap local check: does a credential exist and is it within expiry?
        Does NOT hit the network unless a remote tokeninfo probe is required."""

    @abc.abstractmethod
    async def ensure_valid(self) -> TokenBundle:
        """Idempotent: return a non-expired credential, refreshing transparently.
        This is the hot-path method every transport calls before each request."""

    @abc.abstractmethod
    async def refresh(self) -> TokenBundle:
        """Force a refresh-token exchange. Raises RefreshTokenDead on terminal
        failure (invalid_grant) so the session layer can mark EXPIRED_TERMINAL."""

    @abc.abstractmethod
    async def apply(self, request_headers: dict[str, str],
                    *, params: dict[str, str] | None = None) -> None:
        """Inject the credential into an outbound request the CORRECT way for this
        transport. See §7 — proxy token is header-only; OAuth is Authorization:
        Bearer. Providers own this so callers cannot send a token three ways."""

    @abc.abstractmethod
    async def revoke(self) -> None:
        """Best-effort server-side revoke (OAuth /revoke) + local purge."""

    async def purge_local(self) -> None:
        """Delete all stored secrets for this (transport, account) without
        touching the server. Default impl delegates to the store."""
```

`get_auth_provider()` in `auth/registry.py` is the factory:

```python
def get_auth_provider(
    transport: TransportKind,
    account_email: str | None = None,   # None => profiles.default_email
    *, config: dict | None = None,
) -> AuthProvider:
    """Resolve profile, select secret-store backend, construct the provider.
    Raises UnknownAccount / ProviderNotConfigured."""
```

### 4. Secret storage (`auth/store/`)

`SecretStore` is a thin abstract interface; `KeychainSecretStore` is the default. Every value is keyed `service="colabctl"`, `username=f"{transport}:{account_email}:{slot}"`. A `slot` is a logical secret name (`token_bundle`, `proxy_token`, `oauth_client`, `cookie_blob`).

```python
class SecretStore(abc.ABC):
    @abc.abstractmethod
    def set(self, key: str, value: bytes) -> None: ...
    @abc.abstractmethod
    def get(self, key: str) -> bytes | None: ...
    @abc.abstractmethod
    def delete(self, key: str) -> None: ...
    @abc.abstractmethod
    def list_keys(self, prefix: str) -> list[str]: ...
```

**Chunking algorithm (mandatory — the verdict's #1 keychain failure mode).** macOS generic-password items have a ~4 KB soft limit; OAuth blobs are tiny but cookie blobs and some JSON SA keys exceed it. We chunk *all* writes uniformly:

```python
CHUNK_BYTES = 3072          # below the 4KB soft limit, leaves headroom for metadata

def _set_chunked(self, key: str, value: bytes) -> None:
    blob = base64.b64encode(value)                  # keyring stores str; b64 is safe
    n = math.ceil(len(blob) / CHUNK_BYTES) or 1
    # Header item records chunk count + sha256 for integrity/torn-write detection.
    header = json.dumps({"chunks": n, "sha256": hashlib.sha256(value).hexdigest()})
    keyring.set_password("colabctl", f"{key}#hdr", header)
    for i in range(n):
        part = blob[i * CHUNK_BYTES : (i + 1) * CHUNK_BYTES].decode()
        keyring.set_password("colabctl", f"{key}#{i}", part)

def _get_chunked(self, key: str) -> bytes | None:
    raw = keyring.get_password("colabctl", f"{key}#hdr")
    if raw is None:
        return None
    meta = json.loads(raw)
    parts = [keyring.get_password("colabctl", f"{key}#{i}") for i in range(meta["chunks"])]
    if any(p is None for p in parts):                # torn write / partial delete
        raise SecretStoreCorrupt(key)
    value = base64.b64decode("".join(parts))
    if hashlib.sha256(value).hexdigest() != meta["sha256"]:
        raise SecretStoreCorrupt(key)                # integrity failure -> re-auth
    return value
```

Writes are wrapped in `auth/lock.py`'s cross-process lock so a concurrent refresh in another process cannot interleave chunk writes. Deletes remove `#hdr` last so a crash mid-delete is still detected as corrupt (fail-closed → forces clean re-auth rather than serving half a token).

**Backend selection (`store/backends.py`).** Order of preference, overridable via `COLABCTL_KEYRING_BACKEND`:

| Platform / context | Backend | Notes |
|---|---|---|
| macOS (GUI session) | `keyring` macOS Keychain | Default. ACL via `-T` not set → prompt on binary change is surfaced, not deadlocked (§8). |
| Linux desktop | `keyring` SecretService (libsecret) | Requires an unlocked collection / D-Bus session. |
| Windows | `keyring` Windows Credential Manager | |
| Headless Linux / CI / Docker | `AgeEncryptedFileStore` | No keychain daemon. age-encrypted file at `$COLABCTL_HOME/secrets.age`; key from `COLABCTL_AGE_KEY` env or `~/.config/colabctl/age.key` (0600). |

`AgeEncryptedFileStore` uses the same chunk-header integrity scheme inside a single encrypted JSON document (chunking is a no-op there but the sha256 header is retained for uniform corruption handling). Backend probing is explicit and logged at startup; we never silently fall through to the plaintext `keyring.backends.fail.Keyring` or `chainer` — if no secure backend is available and no age key is configured, `authenticate()` raises `NoSecureSecretStore` rather than writing plaintext.

### 5. Account profiles & multi-account (`auth/profiles.py`)

Multiple Google accounts are first-class. The keychain is keyed per-email, but humans need a friendly index and a notion of "current account."

```python
class AccountProfile(BaseModel):
    email: str                          # canonical key; lowercased, validated
    alias: str | None = None            # e.g. "work", "research"
    enabled_transports: set[TransportKind] = Field(default_factory=set)
    # Per-transport OAuth client overrides (e.g. self-registered client fallback).
    oauth_clients: dict[TransportKind, OAuthClientConfig] = Field(default_factory=dict)
    drive_root_folder_id: str | None = None   # My Drive folder for .ipynb sync
    created_at_epoch: float = Field(default_factory=time.time)
    last_used_epoch: float | None = None
    notes: str | None = None


class ProfilesIndex(BaseModel):
    default_email: str | None = None
    profiles: dict[str, AccountProfile] = Field(default_factory=dict)  # keyed by email
```

The index is **non-secret** and stored at `$COLABCTL_HOME/profiles.toml` (not the keychain — it must be listable without unlocking secrets). Secrets for each profile live in the store under the per-email key. `ProfileManager` API:

```python
class ProfileManager:
    def add(self, email: str, *, alias=None, transports=...) -> AccountProfile: ...
    def remove(self, email: str, *, purge_secrets: bool = True) -> None: ...
    def get(self, ref: str) -> AccountProfile:   # ref = email OR alias
        ...
    def set_default(self, email: str) -> None: ...
    def list(self) -> list[AccountProfile]: ...
    def resolve(self, ref: str | None) -> AccountProfile:
        """None -> default_email; raises NoDefaultAccount if unset."""
```

Account selection precedence at call time: explicit `account_email` arg → `COLABCTL_ACCOUNT` env → `--account` CLI flag → `profiles.default_email`. The MCP server threads an `account` argument through every verb so an agent can operate multiple accounts in one process. Email identity is verified after authentication by reading the OAuth `id_token`/`tokeninfo` and comparing to the requested profile; a mismatch raises `AccountMismatch` and refuses to persist (prevents storing account B's token under account A's slot — a real footgun when a user picks the wrong account in the loopback consent screen).

### 6. Per-transport providers — flows

#### 6.1 `ColabCliAuthProvider` (CORE, sanctioned primary)

The official `google-colab-cli` owns its OAuth loopback flow and writes `~/.colab-cli-oauth-config.json`. We **do not reimplement** the loopback or borrow a client_id; we drive the CLI's own auth and then **adopt** its token file into our store so refresh and multi-account work through us.

Sequence (`authenticate`):
1. Resolve the pinned CLI via the isolated `uv tool` env (Python 3.13 interpreter — see risk register; the core stays 3.11+).
2. Run `colab auth login` (or the CLI's documented auth subcommand) as a subprocess with a per-account `HOME`/config-dir override (`--config` or `COLAB_CLI_CONFIG_DIR`) so each account writes a distinct token file. Capture stdout; if a browser is unavailable and `interactive=False`, raise `InteractiveAuthRequired`.
3. After success, read the CLI's token JSON, verify the email via `tokeninfo`, build a `TokenBundle(issued_by=COLAB_CLI)`, and persist to the store. Delete the on-disk token file (or leave it and treat the store as source of truth — configurable; default: keep CLI file as the CLI also needs it, but mirror into store for our refresh/inspection).
4. `apply()` for this provider is mostly a no-op at the HTTP layer because exec goes *through the CLI subprocess*; the provider's real job is keeping the CLI's token file fresh (see refresh) and surfacing `AuthState` to the session layer.

`refresh`: prefer letting the CLI refresh itself on next invocation. If we detect the CLI token file is stale and the CLI exposes no refresh subcommand, fall back to a direct `google-auth` refresh using the bundle's `refresh_token`/`client_id` and **write the refreshed token back into the CLI's config file** (atomic write under the file lock) so the next subprocess call sees fresh creds. `supports_headless = True`; `supports_unattended_refresh = True` *only if* the refresh token survives (the borrowed/first-party client is not in Testing status — but we still handle `invalid_grant` → `EXPIRED_TERMINAL`).

#### 6.2 `DriveOAuthProvider` (CORE, file sync)

User-OAuth with scope `https://www.googleapis.com/auth/drive.file` (least privilege — only files the app creates/opens; avoids full `auth/drive` blast radius). Uses **our own** registered Desktop OAuth client (this scope *is* publicly grantable, unlike `colaboratory`). PKCE S256 loopback flow implemented with `google-auth-oauthlib`'s `InstalledAppFlow` wrapped in `asyncio.to_thread`.

Sequence:
1. `InstalledAppFlow.from_client_config(OAuthClientConfig, scopes=[drive.file]).run_local_server(port=0, open_browser=interactive)`.
2. Verify email; persist `TokenBundle(issued_by=DRIVE)`.
3. `refresh` uses `google.oauth2.credentials.Credentials.refresh(Request())`. Handle the **7-day refresh-token death** when our app is in Testing status: catch `invalid_grant`, set `EXPIRED_TERMINAL`, and emit a clear actionable error ("Drive re-consent required; move OAuth app to Production or re-run `colabctl auth login --transport drive`"). `supports_unattended_refresh` returns `False` while the app's publishing status is Testing (configurable flag `drive_app_in_production`).
4. `apply` sets `Authorization: Bearer <access_token>`.

All durable artifacts (`.ipynb`) are **plain-blob uploads to the human's My Drive** — never a service account (SA cannot own Google-native files → 403 `storageQuotaExceeded`). The provider only handles auth; the upload logic lives in the file-sync section.

#### 6.3 `GcpAdcAuthProvider` (CORE for Vertex/Enterprise only)

Wraps `google.auth.default()` (ADC) and explicit service-account JSON. Scopes: `https://www.googleapis.com/auth/cloud-platform`. This is **not** a path to consumer Colab Pro — it authenticates Vertex/Colab Enterprise only, and the registry refuses to bind it to `TransportKind.COLAB_CLI`/`RUNTIME_PROXY`. `supports_headless = True`, `supports_unattended_refresh = True` (SA tokens self-refresh, no 7-day death). `refresh` calls `credentials.refresh(Request())`; `apply` sets `Authorization: Bearer`.

#### 6.4 `RuntimeProxyAuthProvider` (OPT-IN ESCAPE HATCH, disabled by default)

Constructing this provider requires `config["escape_hatch_acknowledged"] is True` (set by the user via `colabctl config enable-escape-hatch` after reading a printed disclosure of fragility + abuse-detection exposure); otherwise the registry raises `EscapeHatchNotEnabled`. It depends on a *separate* OAuth identity provider (the Colab CLI bundle) for the `assign` call, then manages the runtime-proxy token lifecycle.

Sequence (`ensure_valid` / `authenticate`):
1. Use the OAuth identity (delegated to `ColabCliAuthProvider`) to call `/tun/m/assign` (handled in the transport layer); receive `RuntimeProxyInfo`.
2. Persist `RuntimeProxyInfo` (chunked, store slot `proxy_token`) keyed to the active assignment id.
3. `refresh`: when `RuntimeProxyInfo.is_expired()`, POST `/tun/m/runtime-proxy-token` to mint a fresh token (this is the token-lifecycle manager; it does *not* re-`assign` unless the assignment itself is gone). On `412 TooManyAssignmentsError`/quota outcomes, surface a typed error to the session layer for routing/backoff.
4. **`apply` is the corrected-auth recipe** (see §7): proxy token header-only; never Bearer; never query param.

`supports_headless = True` but `supports_unattended_refresh = True` only within the token's `tokenExpiresInSeconds` window and the runtime's 24h lifetime; beyond that, re-assignment (fresh VM, state loss) is required and is signaled, not silently retried.

#### 6.5 `BrowserCookieAuthProvider` (AVOID-tier, hard-disabled)

Present only as a stub that raises `DisabledByPolicy` with the DBSC/ToS rationale, unless `COLABCTL_ALLOW_COOKIE_AUTH=1` AND `config["i_understand_account_ban_risk"]`. Not wired into the registry's default routing. Included so the abstraction is complete and the rejection is documented in code, not folklore.

### 7. Credential application — the corrected recipe (`apply`)

The single most-flagged technical error in the verdicts is sending the runtime-proxy token "three ways." The `apply()` contract eliminates this by making providers — not callers — own header construction.

| Transport | Header(s) set by `apply()` | Never do |
|---|---|---|
| `DRIVE`, `VERTEX`, `COLAB_CLI` (HTTP) | `Authorization: Bearer <access_token>` | — |
| `RUNTIME_PROXY` | `X-Colab-Runtime-Proxy-Token: <proxy_token>`, `X-Goog-Colab-Tunnel: true`, `X-Goog-Colab-Token: <xsrf>` | Do **not** set `Authorization: Bearer <proxy_token>`; do **not** add `proxy_token` as a query param; do **not** combine proxy token with the OAuth identity token in one header |
| Kernel WebSocket (within RUNTIME_PROXY) | proxy token in the WS connect `header=` list only | same as above |

```python
# RuntimeProxyAuthProvider.apply
async def apply(self, request_headers, *, params=None):
    info = await self._current_proxy_info()          # refreshes if expired
    request_headers["X-Colab-Runtime-Proxy-Token"] = info.proxy_token.get_secret_value()
    request_headers["X-Goog-Colab-Tunnel"] = "true"
    if info.xsrf_token is not None:
        request_headers["X-Goog-Colab-Token"] = info.xsrf_token.get_secret_value()
    # Deliberately do NOT touch Authorization here. The OAuth identity (if the
    # endpoint also needs it) is applied by the COLAB_CLI provider separately.
```

### 8. Session persistence, resume, refresh & invalidation (`auth/session.py`)

`SessionManager` is the stateful coordinator that sits between providers and the store. It owns: (a) cached in-memory credentials, (b) the cross-process refresh lock, (c) the proactive refresh scheduler, and (d) state transitions.

```python
class SessionManager:
    async def load(self, transport, account_email) -> TokenBundle | None:
        """Read from in-memory cache, else store. Validates integrity (sha256)."""

    async def persist(self, bundle: TokenBundle) -> None:
        """Atomic, locked write to the store + cache update."""

    async def with_fresh(self, provider: AuthProvider) -> TokenBundle:
        """Single-flight refresh: if a refresh is already in progress for this
        (transport, account) in ANY process, wait on the lock instead of racing
        (avoids the 100-refresh-token/account limit footgun)."""

    async def invalidate(self, transport, account_email, *, reason: AuthState) -> None:
        """Mark state, optionally purge. Emits an event the provider abstraction
        can use to route around a dead/blocked account."""

    def schedule_refresh(self, provider: AuthProvider) -> None:
        """Background asyncio task: refresh access tokens at expiry - skew. For
        RUNTIME_PROXY, refresh the proxy token before tokenExpiresInSeconds."""
```

**Single-flight refresh algorithm** (prevents refresh storms that burn the per-account/per-client refresh-token quota and trip abuse heuristics):
1. `ensure_valid()` checks `is_access_expired(skew=120s)`. If valid, return cached.
2. If expired, acquire the cross-process lock (`auth/lock.py`, an OS file lock at `$COLABCTL_HOME/locks/{transport}_{email}.lock`).
3. **Re-read** the store after acquiring the lock (another process may have just refreshed). If now-valid, release and return.
4. Else call `provider.refresh()`, persist atomically, release lock.

**Resume across restarts:** there is no special "resume" — every `ensure_valid()` is a resume. Cold start reads the store, checks expiry, refreshes if needed. The proxy-token case additionally checks whether the *assignment* still exists; if not, the runtime is gone and the session is marked `EXPIRED_TERMINAL` with reason `runtime_reclaimed` so the caller re-allocates rather than retrying a dead endpoint.

**Invalidation triggers and handling:**

| Trigger | Detection | State | Action |
|---|---|---|---|
| Access token expired, refresh OK | local expiry check | `EXPIRED_REFRESHABLE` | transparent refresh |
| `invalid_grant` on refresh | refresh exception | `EXPIRED_TERMINAL` | purge access token, keep profile, raise `RefreshTokenDead` → user re-consent |
| 7-day Testing-status death (Drive) | `invalid_grant` + Testing flag | `EXPIRED_TERMINAL` | actionable message: move to Production or re-auth |
| Server revoke / password change | `401` despite local validity | `REVOKED` | full purge of that transport's secrets, re-auth required |
| Proxy token expired | `is_expired()` / `401` from proxy | `EXPIRED_REFRESHABLE` | mint new proxy token via `/tun/m/runtime-proxy-token` |
| `412 TooManyAssignmentsError` | assign response | `INVALID` (transient) | surface to provider abstraction → backoff / route elsewhere; never spin up extra accounts (ToS) |
| Account abuse-block / `DENYLISTED` | quota outcome / `403` | `REVOKED` (account-level) | mark profile blocked, emit event so the provider abstraction routes to Modal/Vertex; do **not** auto-retry |
| Keychain prompt deadlock risk (binary changed) | keyring raises / times out | `INVALID` | fail fast with remediation ("re-grant Keychain access or set COLABCTL_KEYRING_BACKEND") — never block headless on an invisible GUI dialog |
| Torn write / corruption | sha256 mismatch | `INVALID` | purge slot, force clean re-auth |

A `KeychainPromptTimeout` guard wraps every keyring read in `asyncio.wait_for` (default 10s, env `COLABCTL_KEYCHAIN_TIMEOUT`) so an unattended agent gets a typed error instead of hanging forever on the macOS "Chrome Safe Storage"-style prompt that re-appears after a binary/code-signature change.

### 9. Configuration

| Setting | Env var | Default | Purpose |
|---|---|---|---|
| Home dir | `COLABCTL_HOME` | `~/.config/colabctl` | profiles.toml, locks, age file |
| Active account | `COLABCTL_ACCOUNT` | profiles default | per-call override |
| Keyring backend | `COLABCTL_KEYRING_BACKEND` | auto-probe | force a backend |
| Age key | `COLABCTL_AGE_KEY` / file | `~/.config/colabctl/age.key` | headless secret encryption |
| Keychain read timeout | `COLABCTL_KEYCHAIN_TIMEOUT` | `10` (s) | avoid prompt deadlock |
| Access-token skew | `COLABCTL_TOKEN_SKEW` | `120` (s) | proactive refresh window |
| Escape hatch | `COLABCTL_ENABLE_ESCAPE_HATCH` | `false` | gate RuntimeProxyAuthProvider |
| Cookie auth | `COLABCTL_ALLOW_COOKIE_AUTH` | `false` | gate BrowserCookieAuthProvider (AVOID) |
| Drive app prod status | `COLABCTL_DRIVE_APP_PRODUCTION` | `false` | controls `supports_unattended_refresh` |

CLI surface (Typer): `colabctl auth login --transport <t> [--account <email>]`, `auth status [--account]`, `auth refresh`, `auth logout [--purge]`, `auth list-accounts`, `auth set-default <email>`. MCP verbs mirror these (`auth.status`, `auth.login` returns an `InteractiveAuthRequired` payload with the consent URL when run headless so the agent can hand it to a human).

### 10. Edge cases & failure handling specific to auth

- **Wrong account in consent screen:** post-auth `tokeninfo` email check → `AccountMismatch`, refuse to persist under the requested slot.
- **Concurrent processes / agents:** single-flight refresh + per-(transport,email) file lock prevent both refresh storms and torn chunk writes; the store's sha256 header detects any torn write that slips through and fails closed.
- **Python 3.13-only official CLI vs 3.11+ core:** the CLI runs in an isolated `uv tool` interpreter; the auth provider only exchanges token JSON and never imports the CLI's package — no interpreter coupling.
- **Keychain >4 KB blobs & prompt re-appearance:** uniform chunking + timeout guard (§4, §8).
- **Headless with no browser:** `authenticate(interactive=False)` raises `InteractiveAuthRequired` carrying the loopback consent URL and the documented "auth on a browser machine, copy token, import via `colabctl auth import-token`" fallback path; `auth import-token --transport <t> --file <json>` ingests a token JSON minted elsewhere.
- **No secure store available:** `NoSecureSecretStore` rather than silent plaintext.
- **Refresh-token quota (100/account/client):** single-flight refresh + never minting redundant tokens; logout calls `/revoke` so abandoned tokens free a slot.
- **Account-level abuse block:** treated as terminal `REVOKED` at the account level, surfaced to the provider abstraction for re-routing; never auto-retried and never worked around with multiple accounts (explicit ToS line).

### 4.x Key decisions

- Authentication is per-transport, not global: a single `AuthProvider` ABC (authenticate/get_state/ensure_valid/refresh/apply/revoke + capability properties supports_headless/supports_unattended_refresh) unifies five concrete providers (ColabCli, DriveOAuth, GcpAdc, RuntimeProxy, BrowserCookie) while keeping each transport's credential mechanics isolated.
- Adopt the official google-colab-cli's loopback OAuth (drive the CLI's auth, mirror its token file into our store) instead of borrowing/reimplementing a client_id; the CLI runs in an isolated uv-tool Python 3.13 env so the 3.11+ core floor is preserved and no interpreter coupling exists.
- Drive sync uses OUR own registered Desktop OAuth client with the publicly-grantable drive.file scope and does plain-blob .ipynb uploads to the human's My Drive — never a service account (SA cannot own Google-native files -> 403 storageQuotaExceeded).
- The runtime-proxy token is modeled and applied as a HEADER-ONLY credential (X-Colab-Runtime-Proxy-Token + X-Goog-Colab-Tunnel + X-Goog-Colab-Token), explicitly never Authorization: Bearer and never a query param; `apply()` is owned by providers so callers physically cannot send it three ways.
- Keychain is defense-in-depth, not a boundary: keyring with per-(transport:email:slot) keying, uniform ~3KB chunking with a sha256 integrity header (fail-closed on torn writes), a pluggable non-Mac backend chain (SecretService/WinCred/age-encrypted file), and a hard refusal to fall through to plaintext.
- Multi-account is first-class via a non-secret profiles.toml index (default_email + per-email AccountProfile) plus per-email secret slots; account selection precedence is arg > env > CLI flag > default, with post-auth tokeninfo email verification to prevent storing the wrong account's token.
- SessionManager provides single-flight, cross-process-locked refresh (re-read-after-lock) to avoid refresh storms that burn the 100-token/account quota and trip abuse heuristics; every ensure_valid() doubles as resume across restarts.
- Explicit AuthState machine (VALID / EXPIRED_REFRESHABLE / EXPIRED_TERMINAL / REVOKED / INVALID / NOT_AUTHENTICATED) maps every failure (invalid_grant, 7-day Testing death, 401 revoke, 412 TooManyAssignments, DENYLISTED, keychain prompt timeout, corruption) to a typed action and routes account-level blocks up to the provider abstraction.
- The /tun/m/* RuntimeProxyAuthProvider is gated behind an explicit acknowledged escape-hatch flag, and BrowserCookieAuthProvider is a hard-disabled stub (DBSC/ToS rationale in code) — both present for completeness, neither in default routing.

### 4.y Section risks

- The OS keychain provides no protection against a same-user local process after 'always allow'; the security benefit is limited to no-plaintext-on-disk/no-git-leaks. Mitigation is documentation honesty + age-file backend for headless, but a compromised local user account still exposes all stored Google credentials.
- macOS Keychain prompts re-appear after any binary/code-signature change (Python upgrade, repackaging) and can deadlock an unattended agent; mitigated by a wait_for timeout that converts hangs to typed errors, but this means some auth operations will hard-fail on CI/agent hosts until COLABCTL_KEYRING_BACKEND=age is set.
- The ColabCliAuthProvider depends on the fast-moving v0.5.x official CLI (yanked releases, Python-3.13-only, no confirmed stable JSON output, no public OAuth-client guarantee); adopting its token file and refresh behavior is brittle if Google changes the config path/format or first-party-locks the client. The hard adapter interface contains but does not eliminate this.
- Drive OAuth in 'Testing' publishing status suffers 7-day refresh-token death, breaking unattended Drive sync weekly; supports_unattended_refresh correctly returns False but the only real fix (OAuth verification to Production for a sensitive scope) may be unreachable for a niche tool.
- Runtime-proxy auth (escape hatch) sits on undocumented /tun/m/* internals Google removed from colab-mcp 'for launch'; token-lifecycle, header names, and XSRF flow can change silently and the headless agent profile is exactly what trips opaque, no-appeal abuse blocks (DENYLISTED). Contained behind a gate + provider re-routing, but residual account-ban risk is real and surfaced, not eliminated.
- Cross-process single-flight refresh relies on OS file locks at COLABCTL_HOME; on shared/networked filesystems or if COLABCTL_HOME differs per process, the lock can fail to serialize, reintroducing refresh races and torn-write windows (partially backstopped by sha256 fail-closed corruption detection).
- Email-identity verification via tokeninfo/id_token adds a network round-trip and a dependency on that endpoint's stability; if it is unavailable, AccountMismatch protection degrades and the system may need to trust the provider-reported email.

---

## 5. Transport Layer & Colab Connection

### 1. Purpose & Scope

The transport layer is the single component that turns an abstract "I want a GPU runtime and I want to run code on it" request into bytes on the wire to Colab's backend. Everything above it (the provider abstraction in §6 of the architecture, the MCP server, the Typer CLI) depends on it **only** through the `Transport` Protocol defined below. Nothing above the transport layer is allowed to know whether the underlying connection is the sanctioned `google-colab-cli` subprocess, a browser bridge, or the raw `/tun/m/*` reverse-engineered client.

This section specifies three concrete Colab transports plus the interface they all implement:

| Transport | Module | Default? | Headless | ToS band | Fragility |
|---|---|---|---|---|---|
| `CliTransport` (wraps official `google-colab-cli`) | `colabctl.transport.cli` | **YES (primary)** | Yes | LOW | Medium (0.x dep churn) |
| `BrowserBridgeTransport` (colab-mcp-style) | `colabctl.transport.browser` | No (interactive fallback) | **No** (needs open tab) | LOW | High (undocumented FE handshake) |
| `DirectTunTransport` (raw `/tun/m/*` + Jupyter WS) | `colabctl.transport.direct` | **No — opt-in escape hatch** | Yes | MEDIUM | High (undocumented backend) |

The Jupyter-WebSocket execution client (`JupyterKernelClient`) and the runtime-proxy-token lifecycle manager are **shared** between `CliTransport` (when the CLI hands us a kernel URL) and `DirectTunTransport`. They live in `colabctl.transport.jupyter` and `colabctl.transport.proxy_token`.

> **Architecture invariant (from the verdicts):** The proxy token is a *header-only* credential (`X-Colab-Runtime-Proxy-Token`), **distinct** from the OAuth Bearer identity token. We send it exactly one way. The default path is the official CLI; the direct client is contained, version-gated, and disclosed-risk.

---

### 2. Package Layout

```
src/colabctl/transport/
  __init__.py            # exports Transport, TransportRegistry, build_transport()
  base.py                # Transport Protocol, TransportCapabilities, enums, exceptions
  models.py              # pydantic v2 models (RuntimeSpec, RuntimeHandle, RuntimeProxyInfo, ...)
  registry.py            # name -> factory, capability negotiation, fallback routing
  jupyter/
    __init__.py
    client.py            # JupyterKernelClient (websockets-based wire protocol)
    envelope.py          # JupyterMessage, header builders, msg_id correlation
    channels.py          # shell / iopub / control / stdin channel demux
  proxy_token.py         # RuntimeProxyTokenManager (refresh lifecycle)
  cli/
    __init__.py
    transport.py         # CliTransport
    process.py           # PinnedCliProcess (uv-isolated subprocess, capability probe)
    parser.py            # CLI stdout/JSON adapter (hard adapter interface)
  browser/
    __init__.py
    transport.py         # BrowserBridgeTransport
    bridge.py            # local websocket relay + CDP interception
  direct/
    __init__.py
    transport.py         # DirectTunTransport
    tun_client.py        # TunBackendClient (/tun/m/* REST)
    assign.py            # runtime assignment + accelerator selection
  reconnect.py           # ReconnectPolicy, backoff, resume orchestration
  errors.py              # TooManyAssignmentsError, QuotaDeniedError, TransportDegraded, ...
```

---

### 3. Shared Data Models (`colabctl.transport.models`)

All models are pydantic v2 (`model_config = ConfigDict(frozen=True, extra="forbid")` unless mutation is required). These cross the transport boundary and are what higher layers see.

```python
# colabctl/transport/models.py
from __future__ import annotations
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr

class Accelerator(StrEnum):
    NONE = "NONE"
    T4 = "T4"
    L4 = "L4"
    A100 = "A100"
    H100 = "H100"
    V2_8 = "TPU_V2_8"      # legacy TPU
    V5E_1 = "TPU_V5E_1"
    V6E_1 = "TPU_V6E_1"

class MachineShape(StrEnum):
    STANDARD = "STANDARD"
    HIGH_RAM = "HIGH_RAM"

class RuntimeTier(StrEnum):
    FREE = "FREE"
    PRO = "PRO"
    PRO_PLUS = "PRO_PLUS"
    ENTERPRISE = "ENTERPRISE"

class RuntimeSpec(BaseModel):
    """Abstract 'give me a runtime' request. Backend-agnostic."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    accelerator: Accelerator = Accelerator.T4
    machine_shape: MachineShape = MachineShape.STANDARD
    # Caller may relax accelerator if exact is unavailable (Colab silently downgrades).
    allow_downgrade: bool = True
    min_idle_keepalive_s: int = 60          # transport keep-alive cadence
    max_lifetime_s: int | None = None       # None => backend default (~24h Pro)
    region_hint: str | None = None          # only honored by Enterprise/Vertex backends
    labels: dict[str, str] = Field(default_factory=dict)

class AssignmentOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    QUOTA_DENIED = "QUOTA_DENIED"
    DENYLISTED = "DENYLISTED"
    TOO_MANY_ASSIGNMENTS = "TOO_MANY_ASSIGNMENTS"
    NO_CAPACITY = "NO_CAPACITY"

class RuntimeProxyInfo(BaseModel):
    """Returned by /tun/m/assign (direct) or synthesized from CLI output."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    endpoint_id: str                         # opaque per-VM id used in /tun/m/<id>/...
    proxy_base_url: HttpUrl                   # https://.../tun/m/<id>/
    proxy_token: SecretStr                    # X-Colab-Runtime-Proxy-Token (HEADER ONLY)
    token_expires_in_s: int                   # tokenExpiresInSeconds
    token_issued_at: float                    # monotonic-ish epoch we recorded at receipt
    xsrf_token: SecretStr | None = None       # X-Goog-Colab-Token (two-phase XSRF)

    def expiry_epoch(self) -> float:
        return self.token_issued_at + self.token_expires_in_s

class GrantedRuntime(BaseModel):
    """What we actually got (may differ from RuntimeSpec on downgrade)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    accelerator: Accelerator
    machine_shape: MachineShape
    tier: RuntimeTier
    outcome: AssignmentOutcome

class RuntimeHandle(BaseModel):
    """Opaque-ish handle the higher layers hold. Mutable: token rotates."""
    model_config = ConfigDict(extra="forbid")
    transport_name: str                      # "cli" | "browser" | "direct"
    account_email: str
    granted: GrantedRuntime
    proxy_info: RuntimeProxyInfo
    kernel_id: str | None = None             # set after kernel start
    session_id: str | None = None            # Jupyter session id
    created_at: float

class ExecRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    silent: bool = False
    store_history: bool = True
    allow_stdin: bool = False
    stop_on_error: bool = True
    timeout_s: float | None = None           # None => no client-side cap

class StreamKind(StrEnum):
    STDOUT = "stdout"
    STDERR = "stderr"
    EXECUTE_RESULT = "execute_result"
    DISPLAY_DATA = "display_data"
    ERROR = "error"
    STATUS = "status"

class ExecChunk(BaseModel):
    """One streamed iopub/shell event, correlated by msg_id."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    parent_msg_id: str
    kind: StreamKind
    text: str | None = None
    mime_bundle: dict[str, object] | None = None   # for display_data/execute_result
    ename: str | None = None                        # for error
    evalue: str | None = None
    traceback: list[str] | None = None

class ExecResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    parent_msg_id: str
    status: Literal["ok", "error", "abort"]
    execution_count: int | None = None
    ename: str | None = None
    evalue: str | None = None
    traceback: list[str] | None = None
```

---

### 4. The `Transport` Interface (`colabctl.transport.base`)

This is the load-bearing contract. It is a `typing.Protocol` (structural) so the three implementations and any future backend can satisfy it without a shared base class, and so it composes cleanly with the provider abstraction one layer up.

```python
# colabctl/transport/base.py
from __future__ import annotations
from typing import AsyncIterator, Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict
from colabctl.transport.models import (
    RuntimeSpec, RuntimeHandle, ExecRequest, ExecChunk, ExecResult,
)

class TransportCapabilities(BaseModel):
    """Feature-detection descriptor. Higher layers branch on this, never on type()."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    headless: bool                  # can run with no human browser tab
    live_logs: bool                 # streams iopub vs poll-then-fetch
    interactive_stdin: bool         # supports input()/stdin channel
    file_io_in_vm: bool             # kernel-comms upload/download available
    accelerator_selection: bool     # honors RuntimeSpec.accelerator
    reassign_on_loss: bool          # can transparently re-provision a dead VM
    max_concurrent_runtimes: int    # per-account structural cap (e.g. direct: small)

@runtime_checkable
class Transport(Protocol):
    """The ONLY surface higher layers may use to reach Colab."""

    @property
    def capabilities(self) -> TransportCapabilities: ...

    async def probe(self) -> None:
        """Validate the transport is usable (CLI present & pinned version,
        browser reachable, OAuth creds valid). Raises TransportUnavailable."""

    async def allocate(self, spec: RuntimeSpec) -> RuntimeHandle:
        """Acquire/assign a GPU/TPU runtime. Raises TooManyAssignmentsError,
        QuotaDeniedError, DenylistedError, NoCapacityError."""

    async def start_kernel(self, handle: RuntimeHandle) -> RuntimeHandle:
        """Ensure a Jupyter kernel + session exist on the runtime. Returns
        a handle with kernel_id/session_id populated."""

    def execute(
        self, handle: RuntimeHandle, req: ExecRequest
    ) -> AsyncIterator[ExecChunk]:
        """Stream iopub/shell events for a single execute_request.
        The final ExecResult is also surfaced via collect_result(parent_msg_id)."""
        ...

    async def collect_result(self, parent_msg_id: str) -> ExecResult:
        """Await the shell-channel execute_reply for a given msg_id."""

    async def interrupt(self, handle: RuntimeHandle) -> None:
        """Send KeyboardInterrupt via the control channel."""

    async def keepalive(self, handle: RuntimeHandle) -> None:
        """One keep-alive tick (called by the runtime supervisor, not the user)."""

    async def release(self, handle: RuntimeHandle) -> None:
        """Unassign / stop the runtime. Idempotent; never raises on already-gone."""

    async def aclose(self) -> None:
        """Close sockets, terminate subprocess, release browser context."""
```

**Why `execute` returns an async iterator of `ExecChunk` (not a coroutine):** live-streaming is the common case (iopub). For poll-then-fetch backends (Kaggle, registered elsewhere) the same iterator yields a single terminal chunk; callers therefore write one consumption loop regardless of `capabilities.live_logs`. This is the concrete realization of "capability feature-detection vs lowest-common-denominator."

---

### 5. Transport Errors (`colabctl.transport.errors`)

```python
class TransportError(Exception): ...
class TransportUnavailable(TransportError): ...        # probe() failed
class TransportDegraded(TransportError):               # FE tab closed, CLI drift
    """Raised to trigger fallback routing in the registry."""
class AllocationError(TransportError): ...
class TooManyAssignmentsError(AllocationError):        # HTTP 412 (direct) / parsed (CLI)
    """Per-account concurrent-assignment cap. NOT retryable by re-trying;
    must release an existing runtime first."""
class QuotaDeniedError(AllocationError): ...           # outcome QUOTA_DENIED / zero CCU
class DenylistedError(AllocationError):
    """Abuse-detection block. Surfaced to the user verbatim; never auto-retried,
    never worked around via multi-account (FAQ-prohibited)."""
class NoCapacityError(AllocationError): ...            # transient region/accel stockout
class KernelProtocolError(TransportError): ...
class ProxyTokenExpired(TransportError): ...
class RuntimeLost(TransportError):
    """VM reclaimed (idle/lifetime). Caller may re-allocate if spec allows."""
```

Error → routing decisions:

| Error | Transport-local handling | Escalation to registry |
|---|---|---|
| `ProxyTokenExpired` | refresh via `RuntimeProxyTokenManager`, retry once | none |
| `RuntimeLost` | if `reassign_on_loss`, re-allocate + restart kernel | none unless re-allocate fails |
| `TooManyAssignmentsError` | none (cannot self-heal) | propagate; offer "release N then retry" |
| `NoCapacityError` | bounded retry w/ backoff (≤3), then downgrade accel if allowed | route to next backend |
| `DenylistedError` | none — surface verbatim, halt | **stop**, do not fall back to multi-account |
| `TransportDegraded` | none | route to next backend (e.g. CLI → direct if opted in) |

---

### 6. Primary Transport: `CliTransport` (`colabctl.transport.cli`)

Wraps Google's official `google-colab-cli` as a **pinned, uv-isolated subprocess** behind a hard adapter. The core package floor is Python 3.11+, but the CLI requires 3.13; we never import it — we shell out to it inside its own interpreter env created by `uv tool install --python 3.13 google-colab-cli==<pin>`.

#### 6.1 `PinnedCliProcess` (`cli/process.py`)

```python
class PinnedCliConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version_pin: str                         # exact, e.g. "0.5.7"
    uv_tool_dir: Path                         # isolated env location
    config_path: Path                         # ~/.colab-cli-oauth-config.json (per email)
    prefer_json: bool = True                  # try --json/--output=json, fall back to text
    invoke_timeout_s: float = 120.0

class PinnedCliProcess:
    async def ensure_installed(self) -> None:
        """uv tool install --python 3.13 google-colab-cli==<pin> if not present.
        Verifies `colab --version` == pin; raises TransportUnavailable on mismatch
        (defends against PyPI yanks / silent upgrades)."""

    async def capability_probe(self) -> _CliProbe:
        """Run `colab --help` + `colab --version`; detect whether a stable
        machine-readable mode exists (--json). Caches result. Sets
        TransportCapabilities accordingly (json => robust; text => brittle-mode)."""

    async def run(self, args: list[str], *, env: dict[str,str]) -> _CliInvocation:
        """asyncio.create_subprocess_exec with the isolated interpreter on PATH.
        Streams stdout/stderr line-by-line. Never uses shell=True."""
```

#### 6.2 Adapter / parser (`cli/parser.py`) — the hard interface

Because the CLI is a fast-moving 0.x with **no confirmed stable JSON mode**, parsing is isolated to one module with two strategies and a strict contract:

```python
class CliOutputAdapter(Protocol):
    def parse_allocate(self, inv: _CliInvocation) -> tuple[GrantedRuntime, RuntimeProxyInfo]: ...
    def parse_kernel_url(self, inv: _CliInvocation) -> RuntimeProxyInfo: ...

class JsonCliAdapter:   # used when capability_probe() confirmed --json
    ...
class TextCliAdapter:   # regex/line scanner; tolerant; emits CliDriftWarning on
    ...                 # unrecognized lines so drift is observable, not silent
```

**Drift handling:** `TextCliAdapter` matches a curated set of anchored regexes (e.g. accelerator grant line, proxy URL line). Any allocate invocation whose stdout matches *zero* expected anchors raises `TransportDegraded("cli output schema drift")`, which the registry uses to route to `DirectTunTransport` **only if the user opted into the escape hatch**, otherwise it surfaces a clear "pin a known CLI version" error. We never silently mis-parse.

#### 6.3 `CliTransport` flow

```python
class CliTransport:
    capabilities = TransportCapabilities(
        name="cli", headless=True, live_logs=True, interactive_stdin=False,
        file_io_in_vm=True, accelerator_selection=True,
        reassign_on_loss=True, max_concurrent_runtimes=1,
    )

    async def allocate(self, spec: RuntimeSpec) -> RuntimeHandle:
        args = ["new", "--gpu", _accel_to_cli_flag(spec.accelerator)]
        if spec.machine_shape is MachineShape.HIGH_RAM:
            args += ["--high-ram"]
        inv = await self._proc.run(args, env=self._oauth_env())
        granted, proxy = self._adapter.parse_allocate(inv)
        if granted.outcome is AssignmentOutcome.TOO_MANY_ASSIGNMENTS:
            raise TooManyAssignmentsError(...)
        if granted.outcome is AssignmentOutcome.DENYLISTED:
            raise DenylistedError(_inv_tail(inv))   # verbatim
        if not spec.allow_downgrade and granted.accelerator != spec.accelerator:
            await self._release_endpoint(proxy.endpoint_id)
            raise QuotaDeniedError(f"exact {spec.accelerator} unavailable")
        return RuntimeHandle(transport_name="cli", account_email=self._email,
                             granted=granted, proxy_info=proxy, created_at=now())
```

Once `allocate` yields a `RuntimeProxyInfo` (kernel URL + header token), `start_kernel`/`execute` delegate to the **shared** `JupyterKernelClient` (§8). The CLI is used for *allocation, keep-alive, and release*; code execution rides the standard Jupyter WS so we get correct streaming and msg_id correlation regardless of CLI maturity.

**OAuth env (`_oauth_env`):** we point the CLI at a per-account config file (`~/.config/colabctl/cli-oauth/<email>.json`) whose contents are materialized from the keyring secret store at call time and deleted after (defense-in-depth; keyring is not a boundary). Loopback OAuth happens on first auth only; refresh handled by the CLI.

---

### 7. Runtime-Proxy-Token Lifecycle (`colabctl.transport.proxy_token`)

Shared by `CliTransport` and `DirectTunTransport`. The proxy token is short-lived (`token_expires_in_s`) and **header-only**.

```python
class RuntimeProxyTokenManager:
    """Owns one RuntimeProxyInfo, refreshes it before expiry, and provides
    the correct headers for every proxied request."""

    REFRESH_SKEW_S = 30   # refresh this many seconds before stated expiry

    def __init__(self, refresher: Callable[[str], Awaitable[RuntimeProxyInfo]]):
        self._info: RuntimeProxyInfo | None = None
        self._refresher = refresher           # bound to /tun/m/runtime-proxy-token
        self._lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        info = await self._fresh()
        h = {
            "X-Colab-Runtime-Proxy-Token": info.proxy_token.get_secret_value(),
            "X-Goog-Colab-Tunnel": "true",
        }
        if info.xsrf_token is not None:
            h["X-Goog-Colab-Token"] = info.xsrf_token.get_secret_value()
        # NOTE: OAuth Bearer (identity) is added SEPARATELY by the http client and
        # is a DIFFERENT credential. We never put the proxy token in Authorization,
        # and never send it as a query param. (Verdict-mandated correction.)
        return h

    async def _fresh(self) -> RuntimeProxyInfo:
        async with self._lock:
            if self._info is None:
                raise ProxyTokenExpired("no token; allocate first")
            if time.time() >= self._info.expiry_epoch() - self.REFRESH_SKEW_S:
                self._info = await self._refresher(self._info.endpoint_id)
            return self._info
```

Refresh algorithm (pseudocode):

```
loop on demand (lazy, inside headers()):
  if now >= expiry - SKEW:
     resp = POST /tun/m/runtime-proxy-token  (Bearer=OAuth, X-Goog-Colab-Tunnel)
     parse new proxy_token + tokenExpiresInSeconds
     record token_issued_at = now
  if refresh returns 401/403 -> raise ProxyTokenExpired -> RuntimeLost path
```

A background `KeepAliveSupervisor` (in `reconnect.py`) calls `transport.keepalive()` every `min_idle_keepalive_s`; for direct/CLI this also implicitly exercises the token so it never silently lapses on a long idle gap.

---

### 8. Jupyter WebSocket Client (`colabctl.transport.jupyter`)

We use `jupyter-kernel-client` where it fits, but Colab's proxy needs custom headers, XSRF, and keep-alive that the upstream client does not natively model, so we wrap `websockets` directly and keep the dependency optional. The wire protocol is the standard Jupyter messaging spec v5.3.

#### 8.1 Kernel/session discovery

```python
class JupyterKernelClient:
    def __init__(self, proxy: RuntimeProxyTokenManager, base_url: HttpUrl,
                 http: httpx.AsyncClient): ...

    async def ensure_session(self, handle: RuntimeHandle) -> RuntimeHandle:
        """
        1. GET  {base}/api/kernels        -> list existing kernels
        2. if none: POST {base}/api/sessions  (name, path, type=notebook,
              kernel={name:'python3'})  -> kernel_id, session_id
           (two-phase XSRF: GET first to obtain X-Goog-Colab-Token, then POST)
        3. return handle with kernel_id/session_id set
        Raises KernelProtocolError on 403 (XSRF), 404 (proxy path drift).
        """
```

WebSocket URL is derived from the proxy base:
```
ws_url = base_url.replace("https://", "wss://") + f"api/kernels/{kernel_id}/channels?session_id={session_id}"
```
Subprotocols offered, in order: `v1.kernel.websocket.jupyter.org`, then default. Headers on connect: the full proxy header set (`X-Colab-Runtime-Proxy-Token`, `X-Goog-Colab-Tunnel`, optional `X-Goog-Colab-Token`) **plus** `Authorization: Bearer <oauth_identity>`.

#### 8.2 Message envelope (`jupyter/envelope.py`)

```python
class JupyterHeader(BaseModel):
    msg_id: str            # uuid4().hex
    session: str
    username: str = "colabctl"
    date: str              # ISO8601 UTC
    msg_type: str
    version: str = "5.3"

class JupyterMessage(BaseModel):
    header: JupyterHeader
    parent_header: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)
    content: dict
    # buffers handled out-of-band (binary frames), not in this model

def build_execute_request(session: str, code: str, *, silent: bool,
                          store_history: bool, allow_stdin: bool,
                          stop_on_error: bool) -> JupyterMessage:
    return JupyterMessage(
        header=JupyterHeader(msg_id=uuid4().hex, session=session,
                             date=utcnow_iso(), msg_type="execute_request"),
        content={"code": code, "silent": silent, "store_history": store_history,
                 "user_expressions": {}, "allow_stdin": allow_stdin,
                 "stop_on_error": stop_on_error},
    )
```

The wire frame Colab/Jupyter expects is the multipart structure `[<IDS|MSG>, signature, header, parent_header, metadata, content, ...buffers]`. Because the proxy authenticates the socket (token + XSRF) rather than per-message HMAC, the **signature is empty** (`hmac_key = b""`). We serialize each part as JSON text frames in the `v1.kernel.websocket.jupyter.org` subprotocol; if the server negotiated the default subprotocol we fall back to the single-JSON-blob framing. `jupyter/channels.py` owns this serialization difference behind one `encode(msg, channel)` / `decode(frame)` pair.

#### 8.3 Channels & msg_id correlation

Four logical channels multiplexed over the one WebSocket: `shell`, `iopub`, `control`, `stdin`. Each decoded frame carries `channel` (subprotocol v1) or we infer it from `msg_type`.

```python
class _MsgRouter:
    """Correlates every inbound message to its originating execute_request."""
    def __init__(self):
        self._chunk_queues: dict[str, asyncio.Queue[ExecChunk | _Sentinel]] = {}
        self._results: dict[str, asyncio.Future[ExecResult]] = {}

    def register(self, msg_id: str) -> asyncio.Queue: ...

    def dispatch(self, msg: JupyterMessage) -> None:
        parent = msg.parent_header.get("msg_id")
        mt = msg.header.msg_type
        if mt == "execute_reply":               # shell channel -> terminal result
            self._results[parent].set_result(_to_exec_result(parent, msg))
        elif mt in ("stream", "execute_result", "display_data", "error"):
            self._chunk_queues[parent].put_nowait(_to_chunk(parent, mt, msg))
        elif mt == "status":                    # iopub kernel state
            if msg.content["execution_state"] == "idle" and parent in self._chunk_queues:
                self._chunk_queues[parent].put_nowait(_IDLE_SENTINEL)
```

**Execution completion rule (critical correctness detail):** an execution is complete only when **both** (a) the `execute_reply` arrives on shell, *and* (b) an iopub `status: idle` with matching `parent_msg_id` arrives. We wait for both because either alone can arrive first and stream output can trail the reply. `execute()` ends its iterator on the idle sentinel; `collect_result()` awaits the shell future. A per-request timeout (`ExecRequest.timeout_s`) cancels both and issues a control-channel interrupt.

```python
async def execute(self, handle, req) -> AsyncIterator[ExecChunk]:
    msg = build_execute_request(handle.session_id, req.code, ...)
    q = self._router.register(msg.header.msg_id)
    self._router._results[msg.header.msg_id] = asyncio.get_running_loop().create_future()
    await self._send(msg, channel="shell")
    while True:
        item = await asyncio.wait_for(q.get(), timeout=req.timeout_s) if req.timeout_s \
               else await q.get()
        if item is _IDLE_SENTINEL:
            return
        yield item
```

A single background reader task drains the socket, decodes frames, and calls `_MsgRouter.dispatch`; this avoids interleaving reads across concurrent executions and is the only thing that touches `recv()`.

---

### 9. Opt-In Escape Hatch: `DirectTunTransport` (`colabctl.transport.direct`)

Disabled by default. Enabled only via explicit config (`transport.direct.enabled = true`) **and** an interactive/programmatic acceptance flag (`accept_disclosed_risk = true`) — the constructor raises `TransportUnavailable("direct transport requires explicit opt-in + risk acceptance")` otherwise. Version-gated: it pins the exact `/tun/m/*` contract revision it understands and refuses to run if a probe detects an unexpected schema.

#### 9.1 `TunBackendClient` (`direct/tun_client.py`)

```python
class TunBackendClient:
    XSSI_PREFIX = b")]}'"

    async def assign(self, spec: RuntimeSpec) -> tuple[GrantedRuntime, RuntimeProxyInfo]:
        """
        Two-phase XSRF:
          1. GET  /tun/m/assign  (Bearer OAuth, X-Goog-Colab-Tunnel)
             -> read X-Goog-Colab-Token from response (XSRF)
          2. POST /tun/m/assign  with body {variant, accelerator, machineShape}
             headers: Bearer OAuth, X-Goog-Colab-Tunnel, X-Goog-Colab-Token
        Strip XSSI prefix )]}' before json.loads.
        Map HTTP 412 -> TooManyAssignmentsError.
        Map outcome enum -> AssignmentOutcome (SUCCESS/QUOTA_DENIED/DENYLISTED).
        """

    async def runtime_proxy_token(self, endpoint_id: str) -> RuntimeProxyInfo:
        """POST /tun/m/runtime-proxy-token -> fresh token + tokenExpiresInSeconds."""

    async def assignments(self) -> list[str]: ...          # GET /tun/m/assignments
    async def unassign(self, endpoint_id: str) -> None: ... # POST /tun/m/unassign/{id}
    async def ccu_info(self) -> CcuInfo: ...                # GET /tun/m/ccu-info
```

Request body for accelerator selection:
```json
{"variant": "GPU", "accelerator": "T4", "machineShape": "STANDARD"}
```
(`variant=DEFAULT` for CPU, `variant=TPU` for TPU types.) Because these enum strings are reverse-engineered and undocumented, they live in one `direct/_schema.py` table with the pinned contract revision; the capability probe in `probe()` issues a dry `GET /tun/m/assign` and validates the XSSI prefix + presence of the XSRF header before the transport reports itself usable.

#### 9.2 Capabilities & caps

```python
capabilities = TransportCapabilities(
    name="direct", headless=True, live_logs=True, interactive_stdin=True,
    file_io_in_vm=True, accelerator_selection=True,
    reassign_on_loss=True,
    max_concurrent_runtimes=1,     # 412 cap is small; do NOT multi-account around it
)
```

`allocate` reads existing `assignments()` first; if at the cap it raises `TooManyAssignmentsError` *before* attempting a POST (cheaper, and avoids triggering abuse heuristics with repeated denied assigns). Execution reuses the same `JupyterKernelClient` as the CLI path; the only difference is who minted the `RuntimeProxyInfo`.

---

### 10. Fallback Transport: `BrowserBridgeTransport` (`colabctl.transport.browser`)

Sanctioned, low-ToS, but **explicitly not headless** — it is the human-in-the-loop / interactive path, never the autonomous default. Modeled on the official `colab-mcp` browser bridge.

```python
capabilities = TransportCapabilities(
    name="browser", headless=False, live_logs=True, interactive_stdin=True,
    file_io_in_vm=True, accelerator_selection=True,
    reassign_on_loss=False,        # tab liveness is human-owned
    max_concurrent_runtimes=1,     # single-connection design
)
```

#### 10.1 Local relay + handshake (`browser/bridge.py`)

```python
class LocalBridge:
    """Origin-locked, token-authed local WebSocket the real Colab frontend
    connects back to. We are a JSON-RPC relay; the browser does privileged work."""
    ALLOWED_ORIGINS = {"https://colab.research.google.com", "https://colab.google.com"}
    UI_CONNECTION_TIMEOUT_S = 60.0

    async def open(self) -> _BridgeSession:
        token = secrets.token_urlsafe(16)
        port = await self._bind_loopback()
        url = (f"https://colab.research.google.com/notebooks/empty.ipynb"
               f"#mcpProxyToken={token}&mcpProxyPort={port}")
        webbrowser.open_new(url)           # requires a human + logged-in browser
        fe = await asyncio.wait_for(self._await_frontend(token), UI_CONNECTION_TIMEOUT_S)
        return _BridgeSession(fe)           # rejects a 2nd client with close code 1013
```

If the frontend does not connect within 60s, `allocate` raises `TransportDegraded("browser frontend never connected")`. Tab close / laptop sleep mid-session surfaces as `RuntimeLost` (because `reassign_on_loss=False`, the registry will not auto-fall-back to a headless transport here — it surfaces to the human, since the whole point of this transport is human supervision).

#### 10.2 Optional CDP WebSocket interception (advanced/diagnostic only)

When the user runs the bridge against a **persistent context** they control (their own logged-in profile, for diagnostics — not the autonomous product path), we can observe the kernel socket read-only via CDP. This is gated behind `transport.browser.cdp_observe = true` and is documented as diagnostic-grade, not a control channel:

```python
class CdpKernelObserver:
    """Read-only. Attaches via CDP Network.webSocketFrameReceived to mirror
    iopub frames for ExecChunk synthesis when the relay path is unavailable.
    NEVER injects; cannot reach the cross-origin sandboxed cell iframe."""
    async def attach(self, ws_endpoint: str) -> AsyncIterator[ExecChunk]: ...
```

We explicitly do **not** drive the Colab DOM and do **not** attempt to inject into per-cell output iframes (cross-origin sandbox, `window.google` undefined — confirmed dead end). Persistent context + `storageState` is also avoided as a control mechanism because (a) it is internally contradictory in current Playwright and (b) it implies cookie replay, which DBSC structurally defeats and ToS prohibits.

---

### 11. Reconnection & Resilience (`colabctl.transport.reconnect`)

```python
class ReconnectPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: float = 0.3
    reallocate_on_runtime_lost: bool = True   # honored only if capability allows
```

Backoff: `delay = min(max_delay, base * 2**attempt) * (1 ± jitter)`.

#### 11.1 WebSocket reconnection state machine

```
CONNECTED
   │ socket closed / 1006 / ping timeout
   ▼
RECONNECTING ── (refresh proxy token if expired) ──► reconnect WS
   │  success: re-attach to SAME kernel_id via GET /api/kernels
   │           (if kernel still alive, in-flight msg_ids are resumed:
   │            we re-await execute_reply by replaying nothing — the kernel
   │            kept running; we just rejoin iopub. If iopub gap detected
   │            via missing execution_count, mark result INDETERMINATE.)
   ▼
CONNECTED
   │  kernel gone (404 on /api/kernels/<id>)
   ▼
RUNTIME_LOST ── if reassign_on_loss & spec.allow → ALLOCATE+START_KERNEL
   │                                              (fresh VM, NO in-VM state)
   ▼  emits RuntimeReassigned event so higher layers re-run checkpoint/restore
RESUMED  (durable state must come from Drive/GCS — runtimes are ephemeral)
```

**Idempotency & at-least-once semantics:** re-sending an `execute_request` after a mid-flight disconnect risks double execution. We therefore **never** automatically resend an execute that has already left the shell channel; on reconnect we attempt to rejoin and await the original `execute_reply`. If the kernel is gone, the result is reported `status="abort"` with an `INDETERMINATE` flag rather than silently retried. Re-running is a higher-layer decision (it owns idempotency keys / checkpointing).

#### 11.2 Keep-alive supervisor

```python
class KeepAliveSupervisor:
    """One asyncio.Task per live RuntimeHandle. Calls transport.keepalive()
    every spec.min_idle_keepalive_s. Exponential back-off + DenylistedError
    short-circuit (stop immediately, never hammer a denylisted account)."""
```

The supervisor refreshes the proxy token as a side effect (via `RuntimeProxyTokenManager.headers()`), so long idle periods do not lapse the token. It honors the runtime's `max_lifetime_s`/backend reclamation: at ~24h (Pro) the supervisor stops and emits `RuntimeLost` proactively rather than waiting for a failed exec.

---

### 12. Registry, Selection & Fallback Routing (`colabctl.transport.registry`)

```python
class TransportSelection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    primary: str = "cli"
    fallbacks: list[str] = Field(default_factory=lambda: [])   # e.g. ["browser"]
    allow_direct_escape_hatch: bool = False
    require_headless: bool = False

async def build_transport(cfg: ColabctlConfig, account_email: str) -> Transport:
    """Construct the primary transport, probe() it, and wrap it in a
    FallbackTransport that, on TransportDegraded/NoCapacityError, advances
    to the next configured fallback whose capabilities satisfy the request
    (e.g. require_headless filters out 'browser')."""
```

Routing rules:
- `require_headless=True` removes `browser` from the candidate list at build time.
- `direct` is only ever a candidate when `allow_direct_escape_hatch=True` **and** risk accepted.
- `DenylistedError` halts the whole chain (no fallback, no multi-account) and is surfaced verbatim.
- A `TooManyAssignmentsError` is **not** a fallback trigger (a different transport on the same account hits the same cap); it surfaces with a "release existing runtime" remediation.

Selection of GPU/TPU type maps `RuntimeSpec.accelerator` → backend-specific flags/enums in exactly one place per transport (`_accel_to_cli_flag`, `direct/_schema.py`). The granted accelerator is read back into `GrantedRuntime`; on silent downgrade (e.g. requested A100, got T4) with `allow_downgrade=False` the transport releases and raises `QuotaDeniedError`.

---

### 13. Configuration (`[transport]` block)

```toml
[transport]
primary = "cli"               # "cli" | "browser" | "direct"
fallbacks = []                # e.g. ["browser"] for human-in-the-loop dev
require_headless = true        # filters non-headless transports out of routing

[transport.cli]
version_pin = "0.5.7"          # EXACT pin; refuse mismatched installed version
prefer_json = true
invoke_timeout_s = 120

[transport.browser]
ui_connection_timeout_s = 60
cdp_observe = false            # diagnostic read-only kernel mirror; off by default

[transport.direct]
enabled = false                # OPT-IN escape hatch
accept_disclosed_risk = false  # MUST be explicitly true to construct
contract_revision = "2026.02"  # pinned /tun/m/* schema table
max_concurrent_runtimes = 1

[transport.reconnect]
max_attempts = 5
base_delay_s = 1.0
max_delay_s = 30.0
reallocate_on_runtime_lost = true
```

---

### 14. End-to-End Sequence (primary path)

```
caller → registry.build_transport(cfg, email)        # CliTransport, probed
caller → transport.allocate(RuntimeSpec(accel=T4))
  CliTransport → PinnedCliProcess.run(["new","--gpu","T4"])
  parser → (GrantedRuntime[T4,SUCCESS], RuntimeProxyInfo{base_url, header_token, expiry})
  → RuntimeHandle
caller → transport.start_kernel(handle)
  JupyterKernelClient.ensure_session → GET /api/kernels (XSRF) → POST /api/sessions
  → handle{kernel_id, session_id}
KeepAliveSupervisor starts (every 60s; refreshes proxy token before expiry)
caller → async for chunk in transport.execute(handle, ExecRequest(code="...")):
  build_execute_request → send on shell WS
  background reader → _MsgRouter.dispatch (iopub stream/result, shell execute_reply)
  iterator yields ExecChunk(stdout/display_data/...) until iopub status:idle
caller → result = await transport.collect_result(parent_msg_id)   # execute_reply
... (disconnect) → ReconnectPolicy: refresh token, rejoin kernel; if 404 → RuntimeLost
caller → transport.release(handle)  # CLI stop / direct unassign; idempotent
caller → transport.aclose()
```

---

### 15. Edge Cases & Failure Handling (transport-specific)

| Scenario | Detection | Handling |
|---|---|---|
| CLI installed version ≠ pin (PyPI yank/upgrade) | `colab --version` vs pin in `ensure_installed` | `TransportUnavailable`; do not run; instruct re-pin |
| CLI stdout schema drift | zero anchors match in `TextCliAdapter` | `TransportDegraded`; route to direct *iff* opted in, else clear error |
| Proxy token expires mid-stream | 401/403 on WS or REST; `expiry_epoch` reached | `RuntimeProxyTokenManager` refreshes (skew 30s), reconnect WS, resume |
| Two credentials confused | n/a (prevented by design) | proxy token sent header-only; OAuth Bearer separate; never query param |
| XSRF required on POST | 403 on first POST | two-phase: GET to harvest `X-Goog-Colab-Token`, then POST |
| `412 TooManyAssignmentsError` | HTTP 412 (direct) / parsed (CLI) | surface remediation; **never** multi-account workaround |
| Silent accelerator downgrade | `granted.accelerator != spec.accelerator` | if `allow_downgrade` keep & record; else release + `QuotaDeniedError` |
| Abuse-detection block | `DENYLISTED` outcome / block response | `DenylistedError` verbatim; halt; no fallback, no retry, no appeal logic |
| Region/accel stockout | `NO_CAPACITY` | bounded retry+backoff, then downgrade (if allowed), then route to next backend |
| WS 1006 / ping timeout | reader task sees close | reconnect state machine (§11.1), rejoin same kernel_id |
| Kernel gone (VM reclaimed) | 404 on `/api/kernels/<id>` | `RuntimeLost`; re-allocate if `reassign_on_loss` & spec allows; emit reassign event |
| In-flight exec during disconnect | reader gap; missing `execution_count` | never auto-resend; await original `execute_reply`; if gone → `status=abort` INDETERMINATE |
| Browser tab never connects (60s) | `UI_CONNECTION_TIMEOUT_S` exceeded | `TransportDegraded`; surface to human (no headless fallback) |
| Browser second client | bridge sends close 1013 | reject; single-connection by design |
| Idle/24h lifetime reclamation | supervisor tracks `max_lifetime_s` | proactive `RuntimeLost` before failed exec; externalize state to Drive/GCS |
| Direct transport not opted in | constructor flag check | `TransportUnavailable("requires explicit opt-in + risk acceptance")` |
| Direct `/tun/m/*` schema drift | probe XSSI/XSRF mismatch vs `contract_revision` | refuse to run; do not guess; surface contract-pin error |
| TPU requested, GPU-only account | granted variant mismatch | treat as downgrade/`QuotaDenied` per `allow_downgrade` |
| Keep-alive hammering a blocked account | `DenylistedError` from keepalive | supervisor short-circuits immediately; stops all ticks |

### 5.x Key decisions

- Define a single structural Transport Protocol (typing.Protocol) in colabctl.transport.base that all three Colab transports (CliTransport, BrowserBridgeTransport, DirectTunTransport) satisfy; higher layers branch on a TransportCapabilities descriptor (live_logs, headless, accelerator_selection, reassign_on_loss, max_concurrent_runtimes) via feature-detection, never on concrete type.
- CliTransport is the default/primary: wrap the official google-colab-cli as a version-pinned, uv-isolated Python-3.13 subprocess behind a hard CliOutputAdapter (JSON when probe confirms it, tolerant anchored-regex text parser otherwise that raises TransportDegraded on drift rather than mis-parsing).
- Code execution always rides the shared JupyterKernelClient (standard Jupyter v5.3 wire protocol over websockets) regardless of who allocated the runtime; the CLI is used only for allocate/keepalive/release. Empty HMAC signature because the proxy authenticates the socket, not per-message.
- Runtime-proxy token is modeled as a header-only credential (X-Colab-Runtime-Proxy-Token + X-Goog-Colab-Tunnel + optional X-Goog-Colab-Token XSRF), strictly distinct from the OAuth Bearer identity token, refreshed lazily with a 30s skew by RuntimeProxyTokenManager — never sent as Bearer or query param.
- Execution completion requires BOTH the shell execute_reply AND the iopub status:idle for the same parent_msg_id; a single background reader task owns recv() and routes frames via _MsgRouter to per-msg_id chunk queues and result futures.
- DirectTunTransport (raw /tun/m/* assign + runtime-proxy-token + Jupyter WS) is a non-default opt-in escape hatch, double-gated (enabled + accept_disclosed_risk) and version-pinned to a contract_revision with a probe that validates the XSSI prefix and two-phase XSRF before reporting usable.
- BrowserBridgeTransport is the human-in-the-loop fallback only: origin-locked token-authed local relay opening a real Colab tab; capabilities mark it headless=false and reassign_on_loss=false so the registry never auto-selects it for autonomous/headless requests; optional CDP interception is read-only/diagnostic and never injects into cell iframes.
- Reconnection rejoins the SAME kernel_id and never auto-resends an in-flight execute_request (at-least-once hazard); kernel-gone => RuntimeLost => re-allocate only if reassign_on_loss + spec allow, emitting a reassign event so higher layers restore durable state from Drive/GCS.
- Registry fallback routing advances on TransportDegraded/NoCapacity but HALTS on DenylistedError (surfaced verbatim, no fallback, no multi-account) and does NOT fall back on TooManyAssignmentsError (same account hits the same cap); require_headless filters out the browser transport at build time.
- Accelerator/TPU selection maps RuntimeSpec.accelerator to backend flags/enums in exactly one place per transport; granted accelerator is read back and a silent downgrade with allow_downgrade=false triggers release + QuotaDeniedError.

### 5.y Section risks

- The primary path depends on google-colab-cli, a 0.x dependency with yanked releases, Python-3.13-only requirement, no confirmed stable JSON output, and Google rejecting external PRs — stdout parsing can break on cosmetic changes; mitigated by exact version pinning, capability probe, and a drift-detecting adapter, but breakage cadence is set by Google, not us.
- The /tun/m/* enum strings (variant/accelerator/machineShape), the 412->TooManyAssignmentsError binding, the SUCCESS/QUOTA_DENIED/DENYLISTED outcomes, and the XSSI/two-phase-XSRF dance are reverse-engineered, undocumented, and unconfirmed in primary sources; the DirectTunTransport contract_revision pin will need continuous re-verification and can silently break.
- Abuse-detection / account bans (DenylistedError) are opaque, without appeal SLA, and triggered by exactly the headless sustained-GPU pattern this transport enables even on paid Pro; we surface it verbatim and refuse multi-account workarounds, but residual ban risk to the user's account cannot be engineered away.
- BrowserBridgeTransport structurally requires an open, logged-in human browser tab (single connection, 60s connect window, dies on tab close/sleep) so it cannot serve the autonomous/headless goal; it is only a human-in-the-loop fallback and the registry must never auto-route headless work to it.
- Reconnection cannot guarantee exactly-once execution: a disconnect after an execute_request leaves the shell channel makes the outcome INDETERMINATE; correctness depends on higher layers owning idempotency keys/checkpointing and treating all in-VM state as ephemeral (externalized to Drive/GCS).
- The proxy-token-as-header invariant and the empty-HMAC WS framing are correct per the verdicts but rest on Colab's current proxy auth behavior; if Google adds per-message signing or changes the subprotocol/framing, the shared JupyterKernelClient breaks for both CLI and direct paths simultaneously.
- OAuth for the CLI path relies on the official tool's loopback flow / public-client assumption, which is unverified for arbitrary external users and could be gated/rotated by Google; headless/unattended refresh-token durability (7-day Testing-status death on self-registered clients) remains a fallback-only liability.

---

## 6. Execution Engine & Runtime Lifecycle

This section specifies the layer that turns an authenticated, capability-described backend into a **live, addressable compute runtime** and drives **code and whole-notebook execution** against it, streaming typed events back to the caller. It is the heart of the `colabctl` runtime, and it is deliberately built so that the same engine drives Colab (via the official CLI adapter or the opt-in direct `/tun/m/*` escape hatch), Colab Enterprise/Vertex, and Modal behind one interface.

Two architectural facts from the design dominate every decision below and are repeated because they are load-bearing:

1. **Runtimes are ephemeral and Google-scheduled.** Idle reclamation (~90 min observed), max-lifetime caps (~12 h free / ~24 h Pro, *not contractual*), and opaque preemption mean the engine MUST treat disconnects and re-assignment as the normal case, not the exception. All durable state is externalized to Drive/GCS (see Notebook/File Sync section); the engine only manages *transient* in-VM state and re-establishes connectivity.
2. **The Colab runtime-proxy token is a HEADER-only credential** (`X-Colab-Runtime-Proxy-Token`), distinct from the OAuth Bearer identity token. The kernel WebSocket and Contents/kernels REST calls send it as a header alongside `X-Goog-Colab-Tunnel: true` and the `X-Goog-Colab-Token` XSRF value. We never send the proxy token as a Bearer token or query param (the adversarial review flagged the "send it three ways" recipe as wrong; it collides with the real OAuth `Authorization` header).

### Module Layout

```
src/colabctl/
  engine/
    __init__.py
    models.py            # pydantic v2: RuntimeHandle, KernelInfo, ExecutionResult, output events
    state.py             # ExecutionState / RuntimeState enums + transition tables + guard
    runtime.py           # RuntimeManager: allocate/connect/reconnect/release lifecycle
    kernel.py            # KernelSession: start/restart/interrupt + WS protocol driver
    executor.py          # Executor: execute_code / execute_notebook + event demux
    keepalive.py         # KeepaliveDaemon: token refresh + heartbeat + reclamation watch
    reconnect.py         # ReconnectPolicy + backoff + re-assign orchestration
    events.py            # async event bus, typed OutputEvent stream plumbing
    errors.py            # exception hierarchy specific to this layer
  transport/
    base.py              # Transport ABC (allocation + raw HTTP/WS), capability probe
    official_cli.py      # PRIMARY: google-colab-cli subprocess adapter
    direct_tun.py        # OPT-IN ESCAPE HATCH: /tun/m/* client (version-gated)
    enterprise.py        # Vertex/Colab Enterprise notebookExecutionJobs
    modal_backend.py     # Modal Sandbox/Function
  provider/
    capabilities.py      # CapabilityDescriptor (live-logs vs poll, interactive vs batch)
```

The `engine` package depends only on the `Transport` ABC and `CapabilityDescriptor`; it has **no** knowledge of which backend is live. Backend-specific quirks (header recipes, assign payloads, CLI flags) live entirely in `transport/`.

---

### Data Models (`engine/models.py`)

All models are pydantic v2, `model_config = ConfigDict(frozen=True, extra="forbid")` except the mutable lifecycle records (`RuntimeHandle` fields that mutate carry `Field(..., frozen=False)` via a non-frozen subtype).

```python
from __future__ import annotations
from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr

# ----- accelerator request / grant -----

class Accelerator(StrEnum):
    NONE = "NONE"
    T4 = "T4"
    L4 = "L4"
    A100 = "A100"
    H100 = "H100"
    TPU_V2 = "TPU_V2"
    TPU_V5E = "TPU_V5E"

class MachineShape(StrEnum):
    STANDARD = "STANDARD"
    HIGH_RAM = "HIGH_RAM"

class RuntimeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    accelerator: Accelerator = Accelerator.T4
    machine_shape: MachineShape = MachineShape.STANDARD
    backend: str = "colab"                      # provider key, resolved by abstraction
    # allocation budget (engine-enforced, NOT a Google API field)
    allocate_timeout_s: float = 180.0
    idle_shutdown_s: float | None = None        # client-side hint, see keepalive
    labels: dict[str, str] = Field(default_factory=dict)

class AcceleratorGrant(BaseModel):
    """What Google actually gave us (may be a silent downgrade)."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    requested: Accelerator
    granted: Accelerator
    machine_shape: MachineShape
    downgraded: bool                            # granted != requested
    tier: str | None = None                     # "PRO" | "FREE" | provider-specific

# ----- runtime handle (mutable lifecycle record) -----

class RuntimeProxyInfo(BaseModel):
    """Colab-specific connection coordinates. Mirrors the /tun/m/assign response."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    url: HttpUrl                                # runtimeProxyInfo.url base for kernel WS + REST
    proxy_token: SecretStr                      # X-Colab-Runtime-Proxy-Token (HEADER ONLY)
    xsrf_token: SecretStr | None = None         # X-Goog-Colab-Token
    token_expires_at: datetime                  # derived from tokenExpiresInSeconds at fetch
    endpoint_id: str | None = None              # assignment id for /unassign

class RuntimeHandle(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")  # mutable: proxy info rotates
    runtime_id: str                             # stable across re-assign within a session
    backend: str
    grant: AcceleratorGrant
    proxy: RuntimeProxyInfo | None = None        # populated for Colab/direct transports
    assigned_at: datetime
    max_lifetime_at: datetime | None = None      # best-effort estimate (12h/24h), advisory
    reassign_count: int = 0                      # incremented on each re-allocation
    capabilities_ref: str                        # key into CapabilityDescriptor registry

# ----- kernel -----

class KernelInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kernel_id: str
    name: str = "python3"
    connection_path: str                         # /api/kernels/{id}/channels
    started_at: datetime

# ----- execution request + result -----

class ExecuteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    code: str
    silent: bool = False
    store_history: bool = True
    allow_stdin: bool = False                    # we never block on stdin in headless mode
    stop_on_error: bool = True
    timeout_s: float | None = None               # per-cell wall clock; None = use engine default

class ExecutionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    msg_id: str
    status: Literal["ok", "error", "aborted", "timeout"]
    execution_count: int | None = None
    error: "ErrorOutput | None" = None
    started_at: datetime
    finished_at: datetime
    wall_time_s: float
```

#### Typed output events (`engine/events.py` + `engine/models.py`)

Every byte/MIME bundle the kernel emits is normalized into a discriminated union streamed to the caller. This is the single contract both the CLI and the MCP server consume.

```python
class StreamOutput(BaseModel):
    kind: Literal["stream"] = "stream"
    name: Literal["stdout", "stderr"]
    text: str
    msg_id: str

class DisplayDataOutput(BaseModel):
    kind: Literal["display_data"] = "display_data"
    data: dict[str, str]            # MIME -> payload; images base64, text/plain inline
    metadata: dict = Field(default_factory=dict)
    mime_priority: list[str]        # ranked render preference
    msg_id: str

class ExecuteResultOutput(BaseModel):
    kind: Literal["execute_result"] = "execute_result"
    execution_count: int
    data: dict[str, str]
    msg_id: str

class ErrorOutput(BaseModel):
    kind: Literal["error"] = "error"
    ename: str
    evalue: str
    traceback: list[str]            # ANSI-coded frames, as kernel sends them
    msg_id: str

class StatusOutput(BaseModel):
    kind: Literal["status"] = "status"
    execution_state: Literal["busy", "idle", "starting"]
    msg_id: str | None = None

class ClearOutput(BaseModel):
    kind: Literal["clear_output"] = "clear_output"
    wait: bool
    msg_id: str

OutputEvent = Annotated[
    StreamOutput | DisplayDataOutput | ExecuteResultOutput
    | ErrorOutput | StatusOutput | ClearOutput,
    Field(discriminator="kind"),
]
```

**DataFrame handling.** We do not special-case pandas at the protocol layer; a DataFrame arrives as a `display_data`/`execute_result` carrying `text/html` (the rich table) plus `text/plain`. `mime_priority` is computed by the executor (`text/html` > `text/markdown` > `text/plain` for tables; `image/png` > `image/svg+xml` > `text/plain` for figures) so callers that cannot render HTML degrade gracefully. An optional opt-in transform can rewrite oversized `text/html` tables to a structured `application/vnd.colabctl.dataframe+json` (column schema + truncated rows) for agent consumption; this is a post-processor on the event stream, never a kernel-side dependency.

---

### Execution State Machine (`engine/state.py`)

There are **two** state machines that compose: a coarse `RuntimeState` (is there a live, connected VM + kernel?) and a per-execution `ExecutionState`. Keeping them separate is essential because a single runtime survives many executions, and a re-assignment resets runtime state without canceling the caller's logical "session."

#### `RuntimeState`

| State | Meaning | Entry actions |
|---|---|---|
| `UNALLOCATED` | No VM assigned. | — |
| `ALLOCATING` | `/assign` (or CLI `colab new`) in flight. | start allocate timer |
| `CONNECTING` | VM assigned; opening kernel WS + creating/attaching kernel. | open WS, GET/POST kernels |
| `READY` | Kernel connected, idle, accepting executions. | start keepalive daemon |
| `EXECUTING` | At least one execution in flight (delegates to `ExecutionState`). | — |
| `DEGRADED` | WS dropped or token expired; reconnect in progress. | start reconnect policy |
| `REASSIGNING` | Reconnect failed → VM gone; re-running allocation. | increment `reassign_count` |
| `RELEASING` | Graceful teardown (`/unassign`, kernel shutdown). | stop keepalive |
| `TERMINATED` | Final. VM released or unrecoverable. | flush events, close bus |

```
UNALLOCATED ──allocate()──> ALLOCATING ──assigned──> CONNECTING ──kernel_ready──> READY
ALLOCATING ──TooManyAssignments / QUOTA_DENIED──> TERMINATED(error)
ALLOCATING ──allocate_timeout──> TERMINATED(error)
READY ──submit──> EXECUTING ──all_done──> READY
READY|EXECUTING ──ws_drop / token_expired / 1006──> DEGRADED
DEGRADED ──reconnect_ok──> READY            (proxy token refreshed, same VM)
DEGRADED ──reconnect_exhausted / VM_gone──> REASSIGNING
REASSIGNING ──assigned──> CONNECTING        (reassign_count++)
REASSIGNING ──exhausted / DENYLISTED──> TERMINATED(error)
any ──release()──> RELEASING ──> TERMINATED
```

#### `ExecutionState` (per `msg_id`)

| State | Meaning |
|---|---|
| `QUEUED` | Submitted to engine, not yet sent on shell channel. |
| `SENT` | `execute_request` written to WS; awaiting `status: busy`. |
| `RUNNING` | Kernel `busy`; streaming outputs. |
| `COMPLETED` | `execute_reply` `ok` received and kernel back to `idle`. |
| `FAILED` | `execute_reply` `error`. |
| `INTERRUPTED` | User interrupt or `stop_on_error` upstream abort. |
| `TIMED_OUT` | Per-cell `timeout_s` exceeded → interrupt issued. |
| `LOST` | Runtime entered `REASSIGNING` mid-execution → result indeterminate. |

```
QUEUED ─send─> SENT ─busy─> RUNNING ─execute_reply(ok)+idle─> COMPLETED
RUNNING ─execute_reply(error)─> FAILED
RUNNING ─interrupt()─> INTERRUPTED
RUNNING ─timeout_s elapsed─> TIMED_OUT (engine sends interrupt, then KernelInfo.interrupt)
SENT|RUNNING ─runtime->REASSIGNING─> LOST
```

A central `TransitionGuard` validates every move; illegal transitions raise `IllegalStateTransition` (caught and logged, never propagated to the caller as a crash). The guard is the only writer of state, mutated under an `asyncio.Lock` held by the `RuntimeManager`.

```python
class TransitionGuard:
    _runtime_edges: dict[RuntimeState, frozenset[RuntimeState]] = ...
    _exec_edges: dict[ExecutionState, frozenset[ExecutionState]] = ...

    def runtime(self, frm: RuntimeState, to: RuntimeState) -> None: ...
    def execution(self, frm: ExecutionState, to: ExecutionState) -> None: ...
```

---

### Transport Interface (`transport/base.py`)

The engine speaks to exactly this ABC. The two Colab implementations differ only in how they produce a `RuntimeProxyInfo` and whether they expose a live kernel WS.

```python
class Transport(ABC):
    name: str
    capabilities: CapabilityDescriptor

    @abstractmethod
    async def probe(self) -> CapabilityDescriptor:
        """Detect version, JSON-mode support, live-logs vs poll, interactive vs batch."""

    @abstractmethod
    async def allocate(self, req: RuntimeRequest) -> RuntimeHandle: ...

    @abstractmethod
    async def refresh_proxy_token(self, handle: RuntimeHandle) -> RuntimeProxyInfo:
        """Colab: POST /tun/m/runtime-proxy-token. No-op for batch backends."""

    @abstractmethod
    async def open_kernel_ws(self, handle: RuntimeHandle, kernel_id: str): ...

    @abstractmethod
    async def list_kernels(self, handle: RuntimeHandle) -> list[KernelInfo]: ...

    @abstractmethod
    async def create_kernel(self, handle: RuntimeHandle, name: str = "python3") -> KernelInfo: ...

    @abstractmethod
    async def keep_alive(self, handle: RuntimeHandle) -> None: ...

    @abstractmethod
    async def release(self, handle: RuntimeHandle) -> None: ...
```

#### Header recipe (Colab transports only)

Both `official_cli` (when it surfaces the proxy info) and `direct_tun` build kernel-reaching requests with **exactly** these headers — codified once in `transport/_colab_headers.py` so the corrected recipe is never duplicated:

```python
def colab_headers(proxy: RuntimeProxyInfo, oauth_bearer: str | None) -> dict[str, str]:
    h = {
        "X-Colab-Runtime-Proxy-Token": proxy.proxy_token.get_secret_value(),  # header-only
        "X-Goog-Colab-Tunnel": "true",
    }
    if proxy.xsrf_token:
        h["X-Goog-Colab-Token"] = proxy.xsrf_token.get_secret_value()         # XSRF
    if oauth_bearer:                       # SEPARATE identity credential
        h["Authorization"] = f"Bearer {oauth_bearer}"
    # X-Goog-Colab-Client-Agent intentionally set to our own UA, NOT spoofing "vscode"
    h["X-Goog-Colab-Client-Agent"] = "colabctl/<version>"
    return h
```

The proxy token appears **once**, as a header. The OAuth bearer (identity) is a different header carrying a different credential. We do not spoof the VS Code client-agent string (the review flagged spoofing as a detectable ToS-circumvention signal and a fragility trap); we send our own UA and accept that Google may throttle it — that risk is surfaced to the user, and routing to another backend is the mitigation.

#### Official-CLI adapter specifics (`transport/official_cli.py`)

`google-colab-cli` is invoked as a **pinned, isolated subprocess** (its own `uv tool` env, Python 3.13) so the core package's 3.11+ floor is preserved. The adapter:

- Probes version via `colab --version`; refuses unpinned/yanked versions, falling back per `ReconnectPolicy.on_transport_unavailable`.
- Prefers a structured-output flag if present; **`CapabilityDescriptor.stable_json` is set by the probe**. When false, output is parsed by a **versioned line-grammar parser** in `official_cli_parse.py` keyed to the detected CLI version, and any parse miss raises `TransportContractError` (does not silently produce empty results).
- For execution, if the CLI exposes a kernel endpoint / proxy info, the engine drives the standard WS path below. If the CLI only offers `colab run <file>`, the executor routes through the **batch path** (see Notebook Execution) and the runtime is marked `interactive=False` in capabilities so callers don't request cell-by-cell streaming we can't deliver.

---

### Kernel Session (`engine/kernel.py`)

`KernelSession` wraps the Jupyter wire protocol over the runtime WS using `jupyter-kernel-client`'s WS plumbing where it fits, and a thin in-house `KernelChannels` driver where the Colab proxy needs custom headers / XSRF / subprotocol negotiation (`v1.kernel.websocket.jupyter.org` falling back to default). We do **not** monkeypatch `jupyter-kernel-client`; we use its `WebSocketApp`-based client with `header=` injection and own the message framing.

```python
class KernelSession:
    def __init__(self, transport: Transport, handle: RuntimeHandle, bus: EventBus): ...

    async def start(self, name: str = "python3") -> KernelInfo:
        """Attach to existing kernel if list_kernels() returns one; else create_kernel().
        Idempotent: re-entrant on reconnect (same kernel_id reused when alive)."""

    async def restart(self, *, now: bool = True) -> KernelInfo:
        """POST /api/kernels/{id}/restart. Clears in-VM state; emits StatusOutput(starting)."""

    async def interrupt(self) -> None:
        """POST /api/kernels/{id}/interrupt. Used for user cancel AND timeout enforcement."""

    async def shutdown(self) -> None: ...

    async def _channels_loop(self) -> AsyncIterator[OutputEvent]:
        """Demux iopub/shell. Correlates by parent_header.msg_id. Yields typed events."""
```

**Channel demux algorithm** (`_channels_loop`):

1. Read frame, parse `header.msg_type` and `parent_header.msg_id`.
2. Route:
   - `stream` → `StreamOutput`
   - `display_data` / `update_display_data` → `DisplayDataOutput` (compute `mime_priority`)
   - `execute_result` → `ExecuteResultOutput`
   - `error` → `ErrorOutput`
   - `status` (iopub) → `StatusOutput`; drives `ExecutionState` busy/idle transitions
   - `clear_output` → `ClearOutput`
   - `execute_reply` (shell) → completes the pending future for that `msg_id`
3. Unknown `msg_type` → debug-log and drop (forward-compat).
4. On WS close/error → push a sentinel onto the internal queue so the executor's await wakes, the `RuntimeManager` flips to `DEGRADED`, and in-flight executions transition `RUNNING → LOST` (their futures resolve with `status="aborted"`/`LOST`, never hang).

**Completion detection** is dual-gated to avoid the documented "WS shows kernel available but execution silently stalls" failure: an execution is `COMPLETED` only when **both** the shell `execute_reply` future resolves **and** an iopub `status: idle` with the matching `parent msg_id` is seen. A configurable `idle_grace_s` (default 5 s) bounds the wait for the trailing idle; if `execute_reply` arrives but idle never does, we still complete but flag `wall_time` and emit a debug warning (covers kernels that batch the idle late).

---

### Runtime Manager (`engine/runtime.py`) — allocate / connect / reconnect

```python
class RuntimeManager:
    def __init__(self, transport: Transport, policy: ReconnectPolicy,
                 guard: TransitionGuard, bus: EventBus): ...

    async def acquire(self, req: RuntimeRequest) -> RuntimeHandle:
        """UNALLOCATED -> ALLOCATING -> CONNECTING -> READY. Raises on quota/deny."""

    async def reconnect(self) -> None:
        """DEGRADED recovery: refresh token, reopen WS, reattach kernel."""

    async def reassign(self, req: RuntimeRequest) -> None:
        """REASSIGNING: full new allocation; preserves runtime_id, bumps reassign_count."""

    async def release(self) -> None: ...

    @property
    def state(self) -> RuntimeState: ...
```

#### `acquire` sequence of operations

1. Guard `UNALLOCATED → ALLOCATING`. Start `allocate_timeout_s` timer.
2. `handle = await transport.allocate(req)`.
   - Map `TooManyAssignmentsError` (HTTP 412 for direct transport; CLI exit-code/stderr match for the CLI adapter) → `RuntimeBusyError` → terminal, surfaced with the documented advice *"an existing assignment exists on this account; release it or wait."* We never auto-multi-account (FAQ-prohibited).
   - Map quota `Outcome` (`QUOTA_DENIED` / `DENYLISTED`) → `QuotaDeniedError` / `AccountBlockedError`. `DENYLISTED`/`AccountBlocked` is **non-retriable** and triggers the provider abstraction's recommendation to route to another backend.
3. Inspect `handle.grant`: if `downgraded`, emit a `RuntimeDowngraded` notice on the bus (caller may accept or release). Never silently proceed when the caller passed `accelerator` with `strict=True` (a `RuntimeRequest` extension) — then we `release()` and raise `AcceleratorUnavailable`.
4. Guard `ALLOCATING → CONNECTING`. For Colab transports, `handle.proxy` is now populated; for batch backends, skip WS and mark `interactive=False`.
5. `kernel = await KernelSession.start()`.
6. Guard `CONNECTING → READY`. Start `KeepaliveDaemon`.

#### `reconnect` sequence (DEGRADED → READY)

```
1. classify_drop(exc) -> {TOKEN_EXPIRED, WS_TRANSIENT(1006/1011), VM_GONE(404/410)}
2. if TOKEN_EXPIRED or WS_TRANSIENT:
     proxy = await transport.refresh_proxy_token(handle)   # POST /tun/m/runtime-proxy-token
     handle.proxy = proxy
     await kernel.start()        # reattach to same kernel_id if list_kernels() shows it alive
     if reattach_ok: guard DEGRADED -> READY; resume; return
3. if VM_GONE or reattach failed after policy.max_reconnect attempts:
     guard DEGRADED -> REASSIGNING; await reassign(last_request)
```

The distinction between **reconnect** (same VM, refresh token + reopen socket — cheap, in-memory state preserved) and **reassign** (new VM — in-memory state lost) is explicit and visible to the caller via events, because the caller's notebook-replay / checkpoint-restore logic depends on it.

---

### Executing Code and Notebooks (`engine/executor.py`)

```python
class Executor:
    def __init__(self, manager: RuntimeManager, kernel: KernelSession,
                 bus: EventBus, defaults: ExecConfig): ...

    async def execute_code(self, req: ExecuteRequest) -> ExecutionResult:
        """Single cell. Streams OutputEvents to bus; returns final ExecutionResult."""

    def stream(self, req: ExecuteRequest) -> AsyncIterator[OutputEvent]:
        """Lower-level: async-iterate typed events; final event is StatusOutput(idle)."""

    async def execute_notebook(
        self, nb: NotebookNode | Path, *,
        parameters: dict | None = None,
        on_cell: Callable[[CellResult], Awaitable[None]] | None = None,
        stop_on_error: bool = True,
    ) -> NotebookRunResult: ...
```

#### `execute_code` sequence

1. `manager.state` must be `READY`; else `await manager.ensure_ready()` (drives reconnect/reassign first).
2. Allocate `msg_id`; register `ExecutionState.QUEUED` in the per-runtime exec table.
3. Send `execute_request` on shell channel → `QUEUED → SENT`. Start per-cell `timeout_s` timer (`defaults.cell_timeout_s` if unset).
4. Consume `_channels_loop` events filtered to this `msg_id`; forward each as `OutputEvent` to the bus. First iopub `status: busy` → `SENT → RUNNING`.
5. On `execute_reply`:
   - `ok` + trailing idle → `RUNNING → COMPLETED`.
   - `error` → capture `ErrorOutput` into `ExecutionResult.error`; `RUNNING → FAILED`.
6. On `timeout_s` elapsed → `kernel.interrupt()`, `RUNNING → TIMED_OUT`, result `status="timeout"`.
7. On runtime `DEGRADED`/`REASSIGNING` mid-cell → `RUNNING → LOST`, result `status="aborted"`. The executor does **not** auto-resend (re-execution may have side effects); it surfaces `ExecutionLost` so the caller (or the notebook driver) decides.

#### Whole-notebook execution (`execute_notebook`)

We support **two** execution strategies, chosen by capability:

- **Interactive kernel replay (default for Colab/Modal interactive).** Iterate cells, call `execute_code` per code cell, inject a `parameters` cell (papermill `injected-parameters` tag convention) after the tagged `parameters` cell, capture per-cell outputs into the in-memory `NotebookNode`, and invoke `on_cell` for live progress. `stop_on_error` halts the run and marks downstream cells `skipped`. This preserves cell-by-cell streaming an agent needs.
- **Batch submit (for `interactive=False` backends: Colab Enterprise, Modal Function, CLI `colab run`).** Hand the whole `.ipynb` to the transport's batch path (`nbclient.NotebookClient` with a custom remote `KernelManager`, or the provider's native notebook-execution job), then poll-then-fetch outputs. Live per-cell events are unavailable here; the capability descriptor advertises `live_logs=False` and `execute_notebook` returns the executed notebook + a single terminal `NotebookRunResult`.

```python
class CellResult(BaseModel):
    index: int
    cell_type: Literal["code", "markdown", "raw"]
    status: Literal["ok", "error", "skipped", "timeout", "lost"]
    outputs: list[OutputEvent]
    execution_count: int | None

class NotebookRunResult(BaseModel):
    status: Literal["ok", "error", "partial", "aborted"]
    cells: list[CellResult]
    executed_notebook_ref: str           # path or Drive id of the written .ipynb
    started_at: datetime; finished_at: datetime
```

Rich/widget caveat is documented and handled: `ipywidgets` render only as their text repr in batch/headless mode (no browser), and high-frequency `tqdm`-style streams are coalesced by a **throttling debouncer** (`StreamCoalescer`, default 50 ms / 64 KB flush window) so the event bus is not flooded.

---

### Keepalive, Token Refresh & Idle/Preemption (`engine/keepalive.py`)

A single `KeepaliveDaemon` per runtime runs as a supervised `asyncio.Task`, restarted by the `RuntimeManager` after every successful (re)connect.

```python
class KeepaliveDaemon:
    def __init__(self, transport, handle, manager, *,
                 heartbeat_s: float = 55.0,           # < Colab's ~60s expectation
                 token_refresh_skew_s: float = 90.0,  # refresh before expiry
                 idle_shutdown_s: float | None = None):
        ...
    async def run(self) -> None: ...
```

**Loop (per tick, ~5 s):**

1. **Heartbeat.** If `now - last_heartbeat >= heartbeat_s`: `await transport.keep_alive(handle)` (Colab: ping the keep-alive endpoint). On failure → signal `manager` to enter `DEGRADED`.
2. **Proactive token refresh.** If `handle.proxy.token_expires_at - now <= token_refresh_skew_s`: `await manager.refresh_token_only()` (refresh without dropping the WS). This prevents the *mid-execution* token-expiry drop that the corrected auth recipe makes possible to anticipate.
3. **Idle governance (client-side).** If `idle_shutdown_s` set and no execution has run for that window, the daemon initiates a graceful `release()` to stop burning compute units (the official skill's explicit guidance: "always stop idle sessions"). This is opt-in; default is `None` (never auto-release) because an interactive agent may legitimately idle between turns.
4. **Reclamation watch.** If `max_lifetime_at` estimate is within a warning window, emit a `RuntimePreemptionWarning` event so the caller can checkpoint to Drive/GCS before the VM is reclaimed. The estimate is advisory (limits are unpublished/variable); we never *rely* on it, only warn.

We do **not** simulate fake user activity to defeat idle timeouts; the keepalive uses only the sanctioned keep-alive endpoint. Faking interaction is the exact abuse fingerprint flagged in the review, and on paid Pro the legitimate keep-alive is sufficient.

---

### Reconnect Policy & Backoff (`engine/reconnect.py`)

```python
class ReconnectPolicy(BaseModel):
    max_reconnect: int = 5                 # same-VM token/WS retries before reassign
    max_reassign: int = 2                  # new-VM allocations before giving up
    base_backoff_s: float = 1.0
    max_backoff_s: float = 30.0
    jitter: float = 0.3
    on_transport_unavailable: Literal["fallback_backend", "fail"] = "fallback_backend"

def next_backoff(attempt: int, p: ReconnectPolicy) -> float:
    raw = min(p.max_backoff_s, p.base_backoff_s * 2 ** attempt)
    return raw * (1 + random.uniform(-p.jitter, p.jitter))
```

Drop classification (`classify_drop`) maps low-level failures to actions:

| Signal | Classification | Action |
|---|---|---|
| WS close 1006 / 1011, `ConnectionReset` | `WS_TRANSIENT` | reconnect (reopen WS, no token refresh unless also expired) |
| 401/403 on REST, token past `expires_at` | `TOKEN_EXPIRED` | refresh proxy token → reconnect |
| 404/410 on kernels, `keep_alive` 404 | `VM_GONE` | reassign |
| HTTP 412 / `TooManyAssignmentsError` on reassign | `BUSY` | fail with guidance (do not loop) |
| `DENYLISTED` / account block | `BLOCKED` | non-retriable → escalate to provider abstraction for backend reroute |
| CLI binary missing/yanked/incompatible | `TRANSPORT_UNAVAILABLE` | per `on_transport_unavailable` |

---

### Configuration (`ExecConfig`)

```python
class ExecConfig(BaseModel):
    cell_timeout_s: float = 600.0
    idle_grace_s: float = 5.0
    notebook_stop_on_error: bool = True
    stream_coalesce_ms: int = 50
    stream_coalesce_bytes: int = 65536
    max_output_bytes_per_cell: int = 50 * 1024 * 1024   # hard cap; truncate + flag
    image_inline_max_bytes: int = 4 * 1024 * 1024        # larger images spilled to Drive ref
    keepalive_heartbeat_s: float = 55.0
    token_refresh_skew_s: float = 90.0
```

Surfaced via the standard `colabctl` settings file and overridable per call. `max_output_bytes_per_cell` and `image_inline_max_bytes` guard against a runaway cell flooding the event bus / MCP transport; over-cap payloads are truncated with a `truncated=True` flag and large images are written to Drive with a reference returned instead of inline base64.

---

### Edge Cases & Failure Handling (engine-specific)

| Case | Handling |
|---|---|
| **Token expires mid-execution** | Keepalive refreshes proactively (skew window). If it still drops, `RUNNING → LOST`, refresh, reconnect; the cell is **not** auto-resent (side-effect safety) — caller decides via `ExecutionLost`. |
| **WS reports "kernel available" but exec stalls** | Dual-gate completion (shell reply **and** iopub idle) + per-cell `timeout_s` → interrupt. Never hang indefinitely. |
| **Silent accelerator downgrade** (e.g. requested A100, got T4) | `AcceleratorGrant.downgraded=True`; emit notice; honor `strict` flag (release + raise) or proceed with explicit log. |
| **`TooManyAssignmentsError` (412)** | Terminal `RuntimeBusyError`; do **not** spin up another account (FAQ-prohibited). Advise release/wait. |
| **Account `DENYLISTED` / abuse block** | Non-retriable `AccountBlockedError`; escalate to provider abstraction to reroute to Modal/Vertex; surface the opaque-ban risk to the user verbatim. |
| **VM reclaimed mid-notebook** | In-flight cell → `LOST`; runtime → `REASSIGNING`; emit `RuntimePreemptionWarning` *before* if estimate fired; notebook driver resumes from last checkpoint (caller-owned, Drive-backed). |
| **CLI output format changes (no stable JSON)** | Versioned line-grammar parser; parse miss → `TransportContractError` (loud), never empty results. Pin CLI version; on incompatible version → `TRANSPORT_UNAVAILABLE` fallback. |
| **Kernel restart requested while executions in flight** | Pending executions transition to `INTERRUPTED`; `restart` emits `StatusOutput(starting)`; in-VM state cleared (documented to caller). |
| **Interrupt that the kernel ignores** | After `interrupt_grace_s` (default 10 s) with no return to idle, escalate to `kernel.restart()` and mark cell `TIMED_OUT`. |
| **Oversized / binary output flood** | `StreamCoalescer` + `max_output_bytes_per_cell` truncation; large images spilled to Drive. |
| **Two callers sharing one runtime** | A `RuntimeManager` is single-writer; concurrent `execute_code` calls are serialized through an `asyncio.Queue` (Colab kernels are single-execution-context). MCP server enforces one in-flight execution per runtime, queueing the rest, exposing queue depth in `status`. |
| **`allocate_timeout_s` exceeded** | `ALLOCATING → TERMINATED(error)` with `AllocationTimeout`; release any partial assignment. |

---

### How this engine stays survivable

The engine never imports a backend; it depends on `Transport` + `CapabilityDescriptor`. When Colab churns its `/tun/m/*` contract (direct escape hatch), yanks a CLI release (primary), or bans an account, the failure surfaces as a typed, classified engine error that the **provider abstraction** consumes to reroute to Modal or Colab Enterprise — exactly the "contain Colab's two irreducible risks behind a stable interface" property the architecture is built around. The execution semantics (events, state machine, completion detection, timeouts) are identical across backends; only the transport's allocation + (optional) live-WS capability differs, and the executor branches on capability, not on backend identity.

### 6.x Key decisions

- Split lifecycle into TWO composed state machines: a coarse RuntimeState (allocate/connect/reconnect/reassign/release) and a per-msg_id ExecutionState. A single runtime survives many executions, and re-assignment resets VM state without canceling the caller's logical session — the explicit RuntimeState vs ExecutionState separation makes that distinction visible and testable.
- Distinguish RECONNECT (same VM: refresh proxy token + reopen WS, in-memory state preserved) from REASSIGN (new VM: in-memory state lost, reassign_count bumped). The caller's checkpoint/replay logic depends on knowing which happened, so both are surfaced as typed events.
- Codify the corrected Colab auth recipe in ONE place (transport/_colab_headers.py): X-Colab-Runtime-Proxy-Token is header-only, distinct from the OAuth Bearer identity header, plus X-Goog-Colab-Tunnel and X-Goog-Colab-Token XSRF. Never send the proxy token three ways; never spoof the vscode client-agent.
- Normalize every kernel output into a discriminated pydantic union (OutputEvent: stream/display_data/execute_result/error/status/clear_output) with computed mime_priority, so the CLI and MCP server consume one typed contract and degrade gracefully when they can't render HTML/images. DataFrames are not special-cased at the protocol layer.
- Dual-gate execution completion (shell execute_reply AND iopub idle) plus per-cell timeout->interrupt, directly to defeat the documented 'WS shows kernel available but execution silently stalls' failure mode. Never hang indefinitely.
- Proactive token refresh in the KeepaliveDaemon (refresh before token_expires_at minus skew) to anticipate the mid-execution token-expiry drop that the header-only recipe makes predictable; heartbeat at 55s under Colab's ~60s expectation; only the sanctioned keep-alive endpoint is used (no fake-activity simulation).
- Two notebook-execution strategies chosen by CapabilityDescriptor: interactive kernel replay (cell-by-cell streaming, papermill-style parameter injection) for interactive backends, and batch submit (poll-then-fetch via nbclient/native job) for interactive=False backends (Colab Enterprise, Modal Function, CLI 'colab run').
- Engine depends only on the Transport ABC + CapabilityDescriptor, never on a concrete backend. Typed, classified engine errors (RuntimeBusy/QuotaDenied/AccountBlocked/TransportContractError) feed the provider abstraction so it can reroute to Modal/Vertex when Colab degrades or bans — this is how Colab's two irreducible risks are contained.
- Official google-colab-cli runs as a pinned, isolated subprocess (uv tool env, Python 3.13) to preserve the core 3.11+ floor; output parsed by a version-keyed line grammar with loud TransportContractError on parse miss (never silent empty results), since no stable JSON mode is confirmed.
- Never auto-multi-account on TooManyAssignmentsError (FAQ-prohibited); never auto-resend a LOST cell (side-effect safety) — the caller/notebook driver decides re-execution.

### 6.y Section risks

- Colab proxy-token lifecycle specifics are partly unverified: tokenExpiresInSeconds and /tun/m/runtime-proxy-token are confirmed in colab-vscode, but the exact 412 HTTP binding for TooManyAssignmentsError and the SUCCESS/DENYLISTED/QUOTA_* outcome enum are plausible-but-unconfirmed from primary sources. classify_drop and the allocation error mapping are coded against these names and must be validated by a spike before relying on hard branches.
- The official google-colab-cli is v0.5.x with yanked releases, Python 3.13-only, no confirmed stable JSON output mode, and Google rejects external PRs. The subprocess adapter and version-keyed line-grammar parser are a fast-moving dependency; cosmetic CLI output changes can break parsing and the engine owns the fix.
- Whether the CLI exposes runtime-proxy info / a live kernel WS at all is uncertain. If it only offers 'colab run <file>' batch execution, the interactive cell-by-cell streaming path is unavailable for the primary sanctioned transport, and Colab interactive execution would depend on the opt-in direct /tun/m/* escape hatch (fragile, drift-prone, abuse-detection-exposed).
- Idle/max-lifetime numbers (~90 min idle, 12h/24h caps) are community-observed and explicitly variable/unpublished by Google; the reclamation-warning and idle-governance windows are advisory only and will misfire when Google tightens limits during peak demand.
- Opaque, non-appealable abuse-detection bans (colabtools #4979/#4986) can hit even paid Pro accounts running sustained headless GPU workloads — exactly this engine's profile. The engine surfaces AccountBlockedError and reroutes, but cannot prevent the ban; the residual risk must be disclosed to the user.
- jupyter-kernel-client is a small/young library (v0.6.x) not designed for Colab's bespoke header/XSRF/keep-alive/subprotocol requirements. The in-house KernelChannels driver mitigates this but adds maintenance surface and risk of protocol drift if Colab changes the WS framing.
- Completion-detection edge cases (kernels that emit the trailing idle late, or interrupts the kernel ignores) require the idle_grace_s and interrupt_grace_s escalation-to-restart heuristics; these are timing-based and may produce false TIMED_OUT/restart on a slow but healthy A100/H100 long-running cell.
- Rich/widget output does not round-trip headlessly (ipywidgets render as text repr only), so notebooks relying on interactive display produce degraded outputs — a correctness gap callers must be warned about, not a bug the engine can fix.

---

## 7. Notebook & File Synchronization

This section specifies the **durable-state and transfer layer** of `colabctl`. It is deliberately split from the transport/execution layers because of the single most important fact the adversarial review surfaced: **Colab runtimes are ephemeral and every transport in the provider abstraction can lose its VM at any moment** (idle ~90 min, max-lifetime 12h/24h, re-assignment on `TooManyAssignmentsError`, opaque abuse re-allocation). Therefore **all durable artifacts are externalized to Google Drive (or GCS for the Enterprise/Modal path), never trusted to the VM's local disk**, and the VM filesystem is treated as a *cache* that can vanish without notice.

The section also bakes in the two corrections the verdict flagged for this layer:

1. **Drive sync MUST be user-OAuth plain-blob `.ipynb` uploads to the human's My Drive**, never a service account writing native-MIME files. An SA has 0-byte quota and *cannot own a Google-native file* (`vnd.google.colaboratory`), so create returns `403 storageQuotaExceeded` even on an empty drive (reproduced in n8n #26050, Google Dev forum #194265). Ownership stays with the human and counts against the human's quota — that is correct and intended.
2. **The `/tun/m/.../api/contents` proxy is GET-only** — uploads almost certainly fail. In-VM transient I/O therefore uses the **kernel-comms mechanism Google itself ships** (`google.colab.files`-style base64 over the Jupyter wire protocol), driven through whichever execution transport is active, *not* the Contents API.

### Architectural placement

```
colabctl/
  sync/
    __init__.py
    api.py                 # FileSync facade — the public verbs
    models.py              # pydantic v2 models (this section's data contracts)
    paths.py               # RemotePath / DrivePath / GcsPath normalization
    checksum.py            # content-addressed hashing + manifest diff engine
    ignore.py              # .colabignore / gitignore-style matching
    notebooks.py           # NotebookStore: Drive .ipynb CRUD (Colab format)
    drive/
      __init__.py
      client.py            # DriveClient — user-OAuth google-api-python-client wrapper
      resumable.py         # ResumableUploader — chunked, resumable, checksum-verified
    transfer/
      __init__.py
      base.py              # TransferChannel ABC + capability flags
      kernel_comms.py      # in-VM transfer via Jupyter kernel (default, sanctioned)
      drive_mount.py       # drive.mount() orchestration inside the runtime
      gcs.py               # GCS channel for Enterprise/Modal backends
    engine.py              # SyncEngine — push/pull orchestration + reconciliation
    errors.py              # typed exceptions
```

`sync` depends *downward* on:
- `colabctl.auth` for user-OAuth credentials (`colabctl.auth.UserCredentials`, the same loopback-flow identity used for the sanctioned CLI path).
- `colabctl.providers.base.RuntimeHandle` for the active execution transport (used only to drive kernel-comms transfer and to issue `drive.mount`).
- `colabctl.secrets` only indirectly (credentials already resolved upstream).

`sync` is consumed *upward* by the CLI (`colabctl push|pull|nb`), the MCP server (`fs.push`, `fs.pull`, `nb.create`, etc.), and the provider abstraction's `fetch` verb.

---

### Data models (`colabctl/sync/models.py`)

```python
from __future__ import annotations
import enum
from datetime import datetime
from pathlib import PurePosixPath
from pydantic import BaseModel, Field, field_validator

# Colab's reverse-engineered, source-disputed native MIME. We NEVER write this.
# We always upload plain blobs with the generic notebook MIME so ownership +
# quota semantics stay sane and the file round-trips.
COLAB_NATIVE_MIME = "application/vnd.google.colaboratory"
IPYNB_BLOB_MIME = "application/x-ipynb+json"     # what we actually PUT
DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"

CHUNK_SIZE = 8 * 1024 * 1024                      # 8 MiB resumable chunks
SMALL_FILE_THRESHOLD = 10 * 1024 * 1024           # <10 MiB => kernel-comms ok
LARGE_FILE_THRESHOLD = 256 * 1024 * 1024          # >=256 MiB => prefer Drive mount/GCS


class HashAlgo(str, enum.Enum):
    sha256 = "sha256"
    md5 = "md5"        # only for cross-checking Drive's md5Checksum field


class SyncDirection(str, enum.Enum):
    push = "push"      # local -> runtime
    pull = "pull"      # runtime -> local
    both = "both"      # bidirectional reconcile (last-writer-wins by mtime+hash)


class TransferMethod(str, enum.Enum):
    kernel_comms = "kernel_comms"   # default; works on any active kernel
    drive_mount = "drive_mount"     # drive.mount in-VM, then cp
    drive_api = "drive_api"         # host<->Drive direct (durable layer)
    gcs = "gcs"                     # Enterprise/Modal backends


class FileEntry(BaseModel):
    """One file in a manifest. Path is always relative & POSIX."""
    path: str                                     # "src/train.py"
    size: int
    sha256: str
    mtime: float                                  # epoch seconds, source-of-truth side
    mode: int = 0o644
    is_symlink: bool = False
    symlink_target: str | None = None

    @field_validator("path")
    @classmethod
    def _posix_relative(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"manifest path must be relative & contained: {v!r}")
        return str(p)


class SyncManifest(BaseModel):
    """Content-addressed snapshot of a directory tree at one endpoint."""
    root: str                                     # logical root id (local abspath / remote cwd)
    generated_at: datetime
    algo: HashAlgo = HashAlgo.sha256
    entries: dict[str, FileEntry] = Field(default_factory=dict)   # keyed by path

    def by_hash(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for e in self.entries.values():
            out.setdefault(e.sha256, []).append(e.path)
        return out


class SyncOp(str, enum.Enum):
    create = "create"
    update = "update"
    delete = "delete"
    rename = "rename"      # detected via identical hash, different path
    skip = "skip"          # hash matches, no transfer needed


class SyncAction(BaseModel):
    op: SyncOp
    path: str
    size: int = 0
    from_path: str | None = None                  # for rename
    reason: str = ""                              # human-readable diff cause


class SyncPlan(BaseModel):
    direction: SyncDirection
    method: TransferMethod
    actions: list[SyncAction]
    bytes_to_transfer: int
    n_skipped: int
    n_renamed: int

    @property
    def is_noop(self) -> bool:
        return all(a.op == SyncOp.skip for a in self.actions)


class SyncResult(BaseModel):
    plan: SyncPlan
    transferred: int                              # files actually moved
    bytes_transferred: int
    failed: list[SyncAction] = Field(default_factory=list)
    duration_s: float
    method_used: TransferMethod


class DriveFileRef(BaseModel):
    """Result of a Drive notebook/file operation."""
    file_id: str
    name: str
    mime_type: str
    size: int | None = None
    md5_checksum: str | None = None               # Drive-computed, blob files only
    web_view_link: str | None = None              # opens in Colab if .ipynb
    parents: list[str] = Field(default_factory=list)
    modified_time: datetime | None = None
    owners_self: bool = True                       # must be True (user-OAuth invariant)


class NotebookRef(DriveFileRef):
    """A Colab-openable notebook in Drive."""
    colab_url: str                                 # https://colab.research.google.com/drive/<id>
    nbformat: int = 4
    nbformat_minor: int = 0
    n_cells: int = 0
```

---

### `FileSync` facade — public API (`colabctl/sync/api.py`)

This is the single object the CLI and MCP server hold. Every method is `async` (the core stack is `asyncio` + `httpx`); Drive's blocking `google-api-python-client` calls are wrapped via `anyio.to_thread.run_sync`.

```python
from __future__ import annotations
from pathlib import Path
from typing import AsyncIterator

from colabctl.auth import UserCredentials
from colabctl.providers.base import RuntimeHandle
from colabctl.sync.models import (
    SyncDirection, SyncManifest, SyncPlan, SyncResult,
    TransferMethod, NotebookRef, DriveFileRef, FileEntry,
)


class FileSync:
    def __init__(
        self,
        creds: UserCredentials,
        runtime: RuntimeHandle | None = None,
        *,
        drive_root_folder: str | None = None,   # Drive folder id; None => "colabctl/" auto-created
        progress: "ProgressSink | None" = None,
    ) -> None: ...

    # ---- notebook CRUD (Drive, Colab .ipynb) ----
    async def create_notebook(
        self, name: str, *,
        from_local: Path | None = None,         # seed cells from a local .ipynb
        folder_id: str | None = None,
        overwrite: bool = False,
    ) -> NotebookRef: ...

    async def open_notebook(self, file_id_or_url: str) -> NotebookRef: ...

    async def read_notebook(self, file_id_or_url: str) -> "nbformat.NotebookNode": ...

    async def write_notebook(
        self, file_id_or_url: str, nb: "nbformat.NotebookNode",
        *, expected_md5: str | None = None,     # optimistic concurrency guard
    ) -> NotebookRef: ...

    async def delete_notebook(self, file_id_or_url: str, *, trash: bool = True) -> None: ...

    async def list_notebooks(
        self, *, folder_id: str | None = None, query: str | None = None,
    ) -> list[NotebookRef]: ...

    async def export_notebook(
        self, file_id_or_url: str, dest: Path, *, include_outputs: bool = True,
    ) -> Path: ...

    # ---- directory sync (project <-> runtime) ----
    async def push(
        self, local_dir: Path, remote_dir: str = "/content/project",
        *, method: TransferMethod | None = None,    # None => auto-select
        delete_extraneous: bool = False,
        dry_run: bool = False,
    ) -> SyncResult: ...

    async def pull(
        self, remote_dir: str, local_dir: Path,
        *, globs: list[str] | None = None,          # e.g. ["outputs/**", "*.ckpt"]
        method: TransferMethod | None = None,
        delete_extraneous: bool = False,
        dry_run: bool = False,
    ) -> SyncResult: ...

    async def plan(
        self, local_dir: Path, remote_dir: str, direction: SyncDirection,
        *, method: TransferMethod | None = None,
    ) -> SyncPlan: ...

    # ---- single-artifact convenience (durable) ----
    async def upload_file(
        self, local: Path, *, folder_id: str | None = None,
        name: str | None = None, mime: str | None = None,
    ) -> DriveFileRef: ...

    async def download_file(self, file_id_or_url: str, dest: Path) -> Path: ...

    # ---- large data / datasets ----
    async def mount_drive(self, mountpoint: str = "/content/drive") -> str: ...

    async def stage_dataset(
        self, source: str,                          # drive://<id> | gcs://... | local path
        runtime_path: str = "/content/data",
        *, method: TransferMethod | None = None,
    ) -> str: ...

    # ---- manifests / introspection ----
    async def local_manifest(self, local_dir: Path) -> SyncManifest: ...
    async def remote_manifest(self, remote_dir: str) -> SyncManifest: ...
```

---

### Notebook CRUD over the Drive API (`colabctl/sync/notebooks.py`)

#### The Colab `.ipynb` format and metadata

A Colab notebook is a **standard `nbformat` v4 document** with Colab-specific keys under `metadata.colab` and per-cell `metadata.id`. We preserve these on round-trip but never *require* them. The keys we read/write:

| Key | Location | Purpose | Our handling |
|-----|----------|---------|--------------|
| `metadata.colab.provenance` | notebook | edit history breadcrumbs | preserve verbatim |
| `metadata.colab.name` | notebook | display name | sync to Drive file `name` |
| `metadata.colab.gpuType` | notebook | last-used accelerator hint | preserve; **advisory only** (does not allocate GPU) |
| `metadata.accelerator` | notebook | `"GPU"` / `"TPU"` / `"None"` | preserve; advisory |
| `metadata.kernelspec` | notebook | `{name: "python3", display_name: "Python 3"}` | normalize if missing |
| `metadata.id` | cell | stable cell id | generate (uuid4 hex, 12 chars) if absent |

`create_notebook` produces a minimal valid Colab notebook when no `from_local` seed is supplied:

```python
def _new_colab_notebook(name: str) -> nbformat.NotebookNode:
    nb = nbformat.v4.new_notebook()
    nb["metadata"]["colab"] = {"name": name, "provenance": []}
    nb["metadata"]["kernelspec"] = {"name": "python3", "display_name": "Python 3"}
    nb["metadata"]["accelerator"] = "None"
    nb["cells"] = [nbformat.v4.new_code_cell(source="")]
    _ensure_cell_ids(nb)
    return nb
```

#### Why blob upload, not native MIME (load-bearing)

`NotebookStore.create` serializes the notebook with `nbformat.writes(nb, version=4)` and uploads it as a **plain blob** with `mimeType=application/x-ipynb+json`. Critically, we do **not** set the native `application/vnd.google.colaboratory` MIME on create:

- Setting the native MIME turns the file into a Google-native type. A service account cannot own one (`403 storageQuotaExceeded`); even via user-OAuth, native-type create has weird export/import semantics (colabtools #446/#981 — file Colab refuses to open).
- A plain `.ipynb` blob in My Drive **still opens in Colab** via `https://colab.research.google.com/drive/<file_id>`, and Drive auto-associates `.ipynb` with Colab on the user's account. This is the safe path the working `colab-cli` reference uses (user-OAuth/PyDrive blob uploads).
- The blob also gets a Drive-computed `md5Checksum`, which we exploit for checksum-based sync (native files have *no* md5).

```python
class NotebookStore:
    def __init__(self, drive: DriveClient): self._drive = drive

    async def create(self, name, nb, *, folder_id, overwrite) -> NotebookRef:
        fname = name if name.endswith(".ipynb") else f"{name}.ipynb"
        nb.setdefault("metadata", {}).setdefault("colab", {})["name"] = fname
        _ensure_cell_ids(nb)
        body = nbformat.writes(nb, version=4).encode("utf-8")
        existing = await self._drive.find_by_name(fname, folder_id)
        if existing and not overwrite:
            raise NotebookExists(fname, existing.file_id)
        ref = await self._drive.upload_blob(
            data=body, name=fname, mime=IPYNB_BLOB_MIME,
            folder_id=folder_id, file_id=(existing.file_id if existing else None),
        )
        return _to_notebook_ref(ref, nb)
```

`read_notebook` does `files.get_media` (download bytes) then `nbformat.reads`. We **always read the blob bytes** rather than `files.export`, because export only applies to native-MIME files and would fail / mangle a blob.

`write_notebook` supports **optimistic concurrency**: caller passes `expected_md5`; we re-`files.get` the current `md5Checksum` and refuse if it changed (`NotebookConflict`), so an agent editing a notebook the human is also editing in the browser does not silently clobber.

---

### Drive client (`colabctl/sync/drive/client.py`)

Thin async wrapper over `google-api-python-client` v3 Drive, authenticated **only** with user-OAuth (`colabctl.auth.UserCredentials`). Invariant enforced in every write path: `ref.owners_self is True`. If a write ever produces a file the user doesn't own, we raise `DriveOwnershipError` — this catches the SA-misconfiguration footgun loudly instead of returning a broken `403` deep in a sync.

```python
class DriveClient:
    def __init__(self, creds: UserCredentials, *, root_folder_name="colabctl"): ...

    async def ensure_root_folder(self) -> str: ...   # idempotent; creates "colabctl/" in My Drive
    async def find_by_name(self, name: str, folder_id: str | None) -> DriveFileRef | None: ...
    async def list_folder(self, folder_id: str, *, fields=...) -> list[DriveFileRef]: ...
    async def get_meta(self, file_id: str) -> DriveFileRef: ...

    async def upload_blob(
        self, *, data: bytes | Path, name: str, mime: str,
        folder_id: str | None, file_id: str | None = None,
    ) -> DriveFileRef:
        """Resumable for data >= CHUNK_SIZE; simple multipart otherwise.
        Always plain-blob. Verifies returned md5Checksum against local md5."""

    async def download_media(self, file_id: str, dest: Path) -> Path:
        """Chunked MediaIoBaseDownload; verifies md5 post-download."""

    async def delete(self, file_id: str, *, trash=True) -> None: ...
```

Scopes requested: `https://www.googleapis.com/auth/drive.file` by default (only files the app creates/opens — minimal blast radius), escalating to `auth/drive` only when the user opts into syncing a pre-existing arbitrary folder. The broad `auth/drive` scope is *off by default* per the verdict's security note about full-Drive blast radius.

---

### Push/pull transfer methods and auto-selection (`colabctl/sync/transfer/`)

The runtime VM filesystem is a cache. Push/pull move bytes between the **local machine** and the **runtime**, but the *durable* copy of anything that matters also goes to Drive. Three channels implement `TransferChannel`:

```python
class TransferChannel(ABC):
    method: TransferMethod
    supports_directories: bool
    max_recommended_bytes: int

    @abstractmethod
    async def send_file(self, local: Path, remote_path: str) -> None: ...
    @abstractmethod
    async def recv_file(self, remote_path: str, local: Path) -> None: ...
    @abstractmethod
    async def remote_stat_tree(self, remote_dir: str) -> SyncManifest: ...
    @abstractmethod
    async def remote_delete(self, remote_path: str) -> None: ...
```

#### 1. `KernelCommsChannel` (default; the sanctioned in-VM mechanism)

This is the path Google itself ships (`google.colab.files`): transfer rides the **Jupyter kernel wire protocol** through the active `RuntimeHandle`. We do **not** touch the GET-only `/tun/m/.../api/contents` proxy. To put a file *into* the VM we execute a small bootstrap snippet on the kernel that base64-decodes streamed chunks; to get a file *out* we run code that base64-encodes and streams it back over `stream`/`display_data` messages.

```python
# pseudocode for send_file (local -> runtime)
async def send_file(self, local, remote_path):
    await self._ensure_bootstrap()        # defines _colabctl_recv(path, total_chunks) once
    sha = sha256_file(local)
    n = 0
    async for chunk in chunked_b64(local, CHUNK_SIZE):
        await self.runtime.exec(
            f"_colabctl_recv({remote_path!r}, {chunk!r}, idx={n})",
            collect=False,                  # fire chunk, await ack via iopub
        )
        n += 1
    # finalize: verify hash IN the VM, raise on mismatch
    res = await self.runtime.exec(
        f"_colabctl_finalize({remote_path!r}, expected={sha!r}, n={n})")
    if res.text.strip() != "OK":
        raise TransferChecksumError(remote_path, sha, res.text)
```

Properties: works on *any* active kernel (Colab, Modal, Vertex, Kaggle-with-kernel), no extra credentials, ToS-clean (it is literally Google's own I/O mechanism). Limits: throughput is bounded by the websocket and base64 overhead (~33% inflation), so it is the default **only below `LARGE_FILE_THRESHOLD` (256 MiB)**. `tqdm`-style high-frequency progress in the VM is throttled to ≤4 Hz to avoid flooding iopub.

#### 2. `DriveMountChannel` (large files / datasets)

For large inputs that already live in Drive, we mount Drive *inside* the runtime and reference files directly — zero re-upload. `mount_drive()` executes `from google.colab import drive; drive.mount('/content/drive', force_remount=False)`. On Colab this is sanctioned and uses the runtime's own OAuth. On non-Colab backends `drive.mount` is unavailable → channel reports `supported=False` and the engine falls back to `gcs` or `kernel_comms`.

After mount, `stage_dataset("drive://<id>", "/content/data")` resolves the Drive id to its mounted path and `cp`/symlinks it, avoiding a transfer entirely.

#### 3. `GcsChannel` (Enterprise/Vertex + Modal)

For the sanctioned headless backends, durable I/O is GCS. `gsutil`/`google-cloud-storage` from inside the VM (service-account/ADC auth, which *is* the correct auth for that backend per the architecture). `pull` from a Vertex `notebookExecutionJob` reads `gcsOutputUri`.

#### Auto-selection algorithm (`SyncEngine._choose_method`)

```
def choose_method(file_or_tree, runtime, prefer):
    if prefer is not None: return prefer
    backend = runtime.backend_kind            # colab | vertex | modal | kaggle | local
    total = tree_total_bytes(file_or_tree)

    if backend in (vertex, modal):            # sanctioned headless -> object store
        return TransferMethod.gcs
    if backend == colab:
        if total >= LARGE_FILE_THRESHOLD and source_in_drive(file_or_tree):
            return TransferMethod.drive_mount # avoid re-upload of big data
        if total >= LARGE_FILE_THRESHOLD:
            return TransferMethod.drive_api   # upload to Drive once, mount/download in VM
        return TransferMethod.kernel_comms    # small project files, default
    return TransferMethod.kernel_comms
```

The decision is recorded in `SyncPlan.method` and surfaced to the caller (CLI prints it; MCP returns it), because method choice has real cost/latency implications the agent should see.

---

### Checksum-based sync engine (`colabctl/sync/checksum.py`, `engine.py`)

#### Hashing

- Local: streaming `sha256` over file bytes, 1 MiB read buffer. We also compute Drive-style `md5` *only when* the destination is Drive (to compare against Drive's `md5Checksum` field without re-downloading).
- Remote (in-VM): hashing runs **inside the VM via the kernel** (`hashlib.sha256` over the file) so we never download a file just to learn if it changed. The bootstrap snippet exposes `_colabctl_hashtree(root)` returning a JSON manifest of `{path: {size, sha256, mtime}}`.
- Drive: prefer the cached `md5Checksum` (free, server-side). We keep an md5→sha256 association table inside `colabctl/sync/.colabctl-manifest.json` so a Drive md5 match short-circuits without download.

#### Manifest diff algorithm

```
def diff(src: SyncManifest, dst: SyncManifest, *, delete_extraneous) -> SyncPlan:
    actions = []
    src_by_hash = src.by_hash()
    dst_by_hash = dst.by_hash()
    dst_paths = set(dst.entries)

    for path, s in src.entries.items():
        d = dst.entries.get(path)
        if d and d.sha256 == s.sha256:
            actions.append(SkipAction(path)); continue
        if d is None:
            # rename detection: same hash exists at a different dst path
            twin = first(p for p in dst_by_hash.get(s.sha256, []) if p not in src.entries)
            if twin:
                actions.append(RenameAction(from_=twin, to=path)); continue
            actions.append(CreateAction(path, s.size)); continue
        actions.append(UpdateAction(path, s.size, reason="hash differs"))

    if delete_extraneous:
        for path in dst_paths - set(src.entries):
            # don't delete a file we just consumed as a rename source
            if not consumed_as_rename(path): actions.append(DeleteAction(path))

    return SyncPlan(...content-addressed, renames as metadata-only ops...)
```

Key behaviors:
- **Content-addressed**: identical bytes are never transferred twice; renames become metadata operations (a `mv` in the VM via kernel) instead of delete+re-upload.
- **No-op fast path**: if `src` and `dst` root manifests have equal aggregate hash, return `is_noop` immediately — important for agents that call `push` in a tight loop.
- **mtime is a tiebreaker, never the truth**: bidirectional `both` mode uses `(sha256 equal? skip) else (newer mtime wins)`; we never trust mtime alone because VM clocks and Drive `modifiedTime` are not comparable to local mtime.

#### Ignore rules (`colabctl/sync/ignore.py`)

`.colabignore` (gitignore syntax) plus built-in defaults: `__pycache__/`, `.git/`, `*.pyc`, `.venv/`, `node_modules/`, `.ipynb_checkpoints/`, `*.ckpt` (unless explicitly globbed in `pull`). Matching uses `pathspec` (GitWildMatchPattern). Symlinks outside the tree are refused (`UnsafeSymlink`).

---

### Sequence of operations

**`push(local_dir, remote_dir)` — local project → runtime**

1. `creds` resolved upstream; `runtime` is an *active* `RuntimeHandle` (engine asserts liveness via `runtime.ping()`; if dead → `RuntimeGone`, caller re-allocates).
2. `local_manifest(local_dir)` — walk, apply ignore rules, stream-hash → `SyncManifest`.
3. `remote_manifest(remote_dir)` — kernel runs `_colabctl_hashtree`; if the bootstrap isn't installed it is injected first (idempotent).
4. `choose_method(...)` → `TransferMethod`.
5. `diff(local, remote, delete_extraneous)` → `SyncPlan`. If `dry_run`, return plan now.
6. Execute actions in dependency order: renames → creates/updates (parallelized up to `max_concurrency=4`, bounded because Colab's iopub and Drive both throttle) → deletes.
7. Per file: transfer, then **verify hash on the destination side** (in-VM `hashlib`, or Drive `md5Checksum`). Mismatch → retry once, then record in `SyncResult.failed`.
8. Mirror durable artifacts: any path under the configured `durable_globs` (default `["outputs/**", "*.ipynb", "checkpoints/**"]`) is *also* `upload_blob`'d to the Drive root folder, so a VM loss after push doesn't lose them.
9. Return `SyncResult`.

**`pull(remote_dir, local_dir, globs)` — runtime artifacts → local**

1. Assert runtime liveness.
2. `remote_manifest` filtered by `globs` (default: pull everything not ignored).
3. `local_manifest` of `local_dir` (for skip/rename detection).
4. Diff (direction `pull`), choose method.
5. Transfer out via kernel-comms (small) / download from Drive (if artifact was already mirrored there — cheaper and survives VM death) / GCS (Enterprise/Modal).
6. Verify local file hash == manifest hash; atomic write (`*.part` then `os.replace`).
7. Return `SyncResult`.

**Notebook capture after a headless run** — when an execution job finishes, `export_notebook(file_id, dest, include_outputs=True)` (Drive blob path) or, for in-VM notebooks, pull the executed `.ipynb` from the VM via kernel-comms then `upload_blob` to Drive so the human can open the *output* notebook in Colab.

---

### Large-file and dataset handling: decision matrix

| Scenario | Recommended channel | Why |
|----------|--------------------|-----|
| Source code, configs, small project (<10 MiB total) | `kernel_comms` | sanctioned, no extra creds, fast enough |
| Single artifact 10–256 MiB (e.g. a model checkpoint) | `kernel_comms` with resumable chunking, **and** mirror to Drive | survives VM loss; base64 overhead acceptable |
| Dataset already in user's Drive | `drive_mount` | zero re-transfer; reference in place |
| Dataset >256 MiB not in Drive | `drive_api` upload once → `drive_mount` in VM | one upload, reused across runtimes |
| Enterprise/Vertex or Modal backend | `gcs` | the sanctioned object store for that backend; matches its ADC/SA auth |
| Truly huge (>5 GiB) public datasets | direct in-VM download (`!wget`/`gdown`/`kaggle datasets download`) orchestrated by `stage_dataset` | never round-trips through the local machine |

`stage_dataset` is the unifying verb: it inspects the `source` scheme (`drive://`, `gcs://`, `https://`, local path, `kaggle://`) and dispatches to the cheapest channel that lands the data at `runtime_path`, **without ever pulling huge data down to the local machine first** unless the source is local.

---

### Configuration (`colabctl/sync` knobs, surfaced via pyproject `[tool.colabctl.sync]` and env)

```toml
[tool.colabctl.sync]
drive_root_folder_name = "colabctl"
default_remote_dir     = "/content/project"
chunk_size_mib         = 8
max_concurrency        = 4
small_file_threshold_mib = 10
large_file_threshold_mib = 256
durable_globs          = ["outputs/**", "*.ipynb", "checkpoints/**"]
default_ignore         = ["__pycache__/", ".git/", "*.pyc", ".venv/", "node_modules/"]
verify_checksums       = true          # set false only for trusted, latency-critical loops
drive_scope            = "drive.file"  # or "drive" (opt-in, broad)
overwrite_notebooks    = false
```

Env overrides: `COLABCTL_SYNC_CHUNK_MIB`, `COLABCTL_SYNC_DRIVE_SCOPE`, `COLABCTL_SYNC_VERIFY=0`.

---

### Edge cases & failure handling (specific to this section)

| Failure | Detection | Handling |
|--------|-----------|----------|
| **Runtime vanished mid-sync** (idle/lifetime/re-assign) | kernel `exec` raises `RuntimeGone` / websocket close | abort transfer, surface `RuntimeGone`; durable artifacts already mirrored to Drive are safe; caller re-allocates and re-`push`es (skip-on-hash makes this cheap) |
| **SA-owned native-MIME create** (the classic 403) | `owners_self is False` or `403 storageQuotaExceeded` | raise `DriveOwnershipError` with explicit "use user-OAuth, not a service account" message; never silently retry |
| **Contents API upload attempt** | code path guard | `transfer.drive_api`/`kernel_comms` only — the Contents API write path is *not implemented* by design (GET-only); guard raises `NotImplementedError` if ever invoked |
| **Drive `md5Checksum` absent** (file is native-MIME, not a blob) | field missing in `files.get` | fall back to full download + local sha256; warn that the file is native-type and won't round-trip cleanly |
| **Disputed native MIME string** (`vnd.google.colaboratory` vs `vnd.google-apps.colaboratory`) | MIME read on open | accept *both* on read; always write the blob MIME — neutralizes the source-disputed-string risk |
| **Notebook concurrent edit** (human in browser + agent) | `expected_md5` mismatch on `write_notebook` | raise `NotebookConflict`; expose 3-way info (base/local/remote md5) so caller can re-read+merge |
| **base64/iopub flood on large file** | chunk-ack timeout, throttle counter | adaptive backoff; if >`LARGE_FILE_THRESHOLD`, auto-upgrade to `drive_mount`/`gcs` |
| **Checksum mismatch after transfer** | post-transfer verify | retry once; on second failure record in `SyncResult.failed`, do not mark sync successful |
| **Drive quota exceeded (human's My Drive full)** | `403 storageQuotaExceeded` on a *blob* upload | distinct error `DriveQuotaExceeded` (vs the SA-ownership 403); message instructs to free Drive space or target GCS |
| **OAuth token expired/revoked mid-sync** | `401`/`invalid_grant` | one transparent refresh via `UserCredentials.refresh()`; if refresh fails (7-day Testing-status death), raise `AuthExpired` with re-auth instructions |
| **Symlink / `..` traversal in manifest** | `FileEntry` validator + ignore scan | reject with `UnsafeSymlink`/`UnsafePath` before any transfer |
| **Partial download crash** | `*.part` temp + atomic `os.replace` | crash leaves only a `.part`; next pull treats target as absent and re-fetches |
| **Resumable upload interrupted** | `MediaFileUpload` next-chunk 308/5xx | resume from last committed byte using the resumable session URI; exponential backoff (max 5 tries) |
| **Empty / zero-byte files** | size==0 | sha256 of empty string is well-defined; transferred normally (kernel-comms handles 0 chunks) |
| **Notebook with non-UTF-8 / corrupt JSON** | `nbformat.reads` raises | wrap in `NotebookParseError`; never partial-write a corrupt notebook to Drive |

---

### What this layer deliberately does NOT do

- It does **not** use the `/tun/m/.../api/contents` Contents API for uploads (GET-only; verdict score 2 / AVOID).
- It does **not** use a service account to write Colab `.ipynb` files (structural `403`).
- It does **not** treat the VM local disk as durable — every artifact worth keeping is mirrored to Drive/GCS, because runtimes are ephemeral.
- It does **not** hard-code Colab specifics into the channel interface — `TransferChannel` is implemented for Modal/Vertex/Kaggle too, so the provider abstraction can route sync the same way it routes execution.

### 7.x Key decisions

- Drive notebook CRUD uses user-OAuth PLAIN-BLOB .ipynb uploads (mimeType application/x-ipynb+json) to the human's My Drive, never a service account and never the native application/vnd.google.colaboratory MIME — avoids the structural 403 storageQuotaExceeded, keeps ownership with the human, and yields a Drive-computed md5Checksum we use for sync.
- In-VM file transfer defaults to KernelCommsChannel (Google's own files-style base64-over-Jupyter-wire mechanism through the active RuntimeHandle), NOT the GET-only /tun/m/.../api/contents proxy which cannot accept uploads.
- All durable artifacts (.ipynb, outputs/**, checkpoints/**) are mirrored to Drive/GCS on push because runtimes are ephemeral; the VM filesystem is treated strictly as a vanishable cache, so a lost runtime never loses state and re-push is a cheap hash-skip no-op.
- Sync is content-addressed (sha256, with Drive md5Checksum short-circuit): identical bytes are never re-transferred, renames become metadata-only mv operations, and a matching aggregate hash yields an immediate no-op fast path for tight agent loops.
- TransferMethod is auto-selected by backend + size: kernel_comms (<256 MiB, Colab default), drive_mount (large data already in Drive), drive_api (large upload-once-mount-many), and gcs (Enterprise/Vertex/Modal) — and the chosen method is surfaced in SyncPlan so cost/latency is visible.
- The TransferChannel ABC is backend-agnostic (Colab/Modal/Vertex/Kaggle implementations) so the provider abstraction routes sync identically to how it routes execution; large datasets are staged in-VM via stage_dataset without round-tripping through the local machine.
- Default Drive scope is drive.file (only app-created/opened files) to minimize blast radius; broad auth/drive is opt-in only. Notebook writes use optimistic concurrency via expected_md5 to avoid clobbering a human editing in the browser.

### 7.y Section risks

- Kernel-comms transfer is bounded by the Jupyter websocket plus ~33% base64 inflation and Colab iopub throttling; large single files are slow and depend entirely on the (fragile, undocumented) active transport staying alive mid-transfer — a re-assignment aborts the transfer (mitigated by Drive mirroring + hash-skip resume, not eliminated).
- The Colab native MIME string is reverse-engineered and source-disputed (vnd.google.colaboratory vs vnd.google-apps.colaboratory) and appears in no official Google MIME guide; a Drive/Colab change to native-type handling could break notebook recognition. We mitigate by writing plain blobs and accepting both strings on read, but cannot fully control how Colab associates the blob.
- Drive blob uploads count against the HUMAN's My Drive quota and require user-OAuth whose refresh token dies after 7 days while the OAuth app is in Testing status — long-unattended agents can hit AuthExpired and DriveQuotaExceeded; both are surfaced as typed errors but require human remediation.
- drive.mount inside the runtime is Colab-only and itself an interactive-ish flow; on non-Colab backends the channel must fall back, and even on Colab mount can prompt/fail under abuse-detection — the dataset path is therefore not uniformly headless across backends.
- Verifying checksums in-VM relies on executing helper code on the kernel (the bootstrap snippet); if the execution transport changes its output framing or rate-limits iopub, hash-tree/finalize acks can stall — adaptive backoff helps but the dependency on undocumented Colab kernel behavior remains.
- Pulling executed-notebook outputs and large artifacts can race the ~90-min idle / 12–24h lifetime caps; if a job outlives the runtime the only durable copy is whatever was already mirrored to Drive/GCS, so checkpoint cadence (durable_globs) must be tuned per workload or output is lost.
- google-api-python-client is synchronous and wrapped via to_thread; under high file counts the bounded concurrency (4) plus Drive API rate limits make directory sync of many small files latency-bound, and Drive's per-user write QPS can trigger 403 rateLimitExceeded requiring backoff not yet load-tested.

---

## 8. Provider Abstraction & Fallback

This section specifies the layer that the adversarial review scored highest (7, "the single best strategic decision in this product"). Its job is to contain Colab's two irreducible risks — **Google interface churn** and **opaque abuse-detection bans** — behind a stable, capability-negotiated contract so the product keeps working by routing to a sanctioned backend (Modal, Colab Enterprise/Vertex, HF Jobs, Kaggle, RunPod/vast) when Colab degrades, is quota-exhausted, or an account is blocked.

Design invariants enforced here:

- Colab is the **first-class, default** backend, but it is *not* privileged in the interface — it implements the same `Backend` protocol as everything else.
- Every backend is **honest about its capabilities** via a `Capabilities` descriptor; the SDK never assumes a feature (live logs, persistence, interactive REPL, a specific GPU) is present.
- Fallback is **policy-driven and explicit**, never silent by default. A fallback that changes cost, ToS posture, or "is-it-actually-Colab" semantics MUST be surfaced to the caller.
- All durable state is **externalized** (Drive/GCS/Volumes). Runtimes are ephemeral; a re-route to a different backend cannot assume in-VM state survives.

### Module Layout

```
colabctl/
  providers/
    __init__.py            # registry bootstrap, public re-exports
    base.py                # Backend Protocol, abstract base, shared mixins
    models.py              # pydantic v2: Capabilities, JobSpec, JobHandle, JobStatus, LogChunk, Artifact, GpuType...
    errors.py              # BackendError hierarchy (Transient/Permanent/Quota/Banned/Unavailable/Unsupported)
    registry.py            # ProviderRegistry: name -> factory, priority ordering, capability index
    selector.py            # BackendSelector: requirement -> ranked candidate list
    fallback.py            # FallbackEngine: orchestrates submit-with-failover, attempt ledger
    capabilities.py        # CapabilityProbe: live vs cached probing, capability cache
    colab.py               # ColabBackend (first-class) — wraps the transport layer (CLI/MCP/escape-hatch)
    modal_be.py            # ModalBackend (score 8, gVisor sandboxes) — worked example below
    vertex.py              # VertexBackend (Colab Enterprise / notebookExecutionJobs)
    hf_jobs.py             # HFJobsBackend
    kaggle.py              # KaggleBackend (poll-then-fetch only)
    runpod.py              # RunPodBackend / VastBackend (marketplace IaaS)
    nbadapter.py           # NotebookExecutionAdapter (papermill/nbclient over any kernel backend)
```

`colabctl/providers/base.py` depends only on `models.py` and `errors.py`. Concrete backends depend on `base.py` plus their own SDK, behind optional-dependency extras (`pip install colabctl[modal]`, `[vertex]`, `[hf]`, `[kaggle]`, `[runpod]`). A backend whose extra is not installed registers as `available=False` rather than raising at import.

### Core Data Models (`providers/models.py`)

All models are `pydantic.BaseModel` (v2), `model_config = ConfigDict(frozen=True, extra="forbid")` unless noted. Enums are `str`-backed so they serialize cleanly into the CLI/MCP surface.

```python
from __future__ import annotations
import enum
from datetime import datetime, timedelta
from pydantic import BaseModel, ConfigDict, Field, field_validator


class GpuType(str, enum.Enum):
    NONE = "none"
    T4 = "t4"
    L4 = "l4"
    A100 = "a100"      # 40GB
    A100_80 = "a100-80g"
    H100 = "h100"
    H200 = "h200"
    B200 = "b200"
    V100 = "v100"
    P100 = "p100"      # Kaggle
    TPU_V2 = "tpu-v2-8"
    TPU_V5E = "tpu-v5e-1"
    TPU_V6E = "tpu-v6e-1"

    @property
    def is_tpu(self) -> bool:
        return self.value.startswith("tpu-")


# Coarse ordering used for "at least this GPU" matching. Backends may override
# with a finer per-backend ranking; this is the cross-backend default.
GPU_RANK: dict[GpuType, int] = {
    GpuType.NONE: 0, GpuType.T4: 10, GpuType.P100: 12, GpuType.L4: 20,
    GpuType.V100: 25, GpuType.A100: 40, GpuType.A100_80: 45,
    GpuType.H100: 60, GpuType.H200: 70, GpuType.B200: 90,
    GpuType.TPU_V2: 30, GpuType.TPU_V5E: 50, GpuType.TPU_V6E: 80,
}


class ExecMode(str, enum.Enum):
    INTERACTIVE = "interactive"   # persistent kernel, cell-by-cell, read intermediate state (Colab, Modal Notebooks)
    BATCH = "batch"               # submit-poll-collect, whole .ipynb or script (Vertex, HF, Kaggle)


class LogMode(str, enum.Enum):
    LIVE = "live"                 # stream stdout/stderr while running (Colab kernel, Modal, HF, RunPod)
    POLL_THEN_FETCH = "poll"      # only outcome after completion (Kaggle)


class Persistence(str, enum.Enum):
    EPHEMERAL = "ephemeral"       # VM wiped on idle/lifetime; externalize everything
    VOLUME = "volume"             # backend-native durable volume (Modal Volume, RunPod network volume)
    DRIVE = "drive"               # durable via user-OAuth Google Drive sidecar


class Sanction(str, enum.Enum):
    SANCTIONED = "sanctioned"     # first-party supported API (Modal, Vertex, HF, Kaggle, RunPod, official Colab CLI)
    SANCTIONED_INTERACTIVE = "sanctioned-interactive"  # official but needs open browser tab (colab-mcp bridge)
    ESCAPE_HATCH = "escape-hatch" # opt-in, disclosed-risk reverse-engineered /tun/m/* path


class Capabilities(BaseModel):
    """A backend's honest self-description. Returned by Backend.capabilities()."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_name: str
    available: bool = True                     # SDK installed + creds present
    is_colab: bool = False                     # literal Colab Pro vs a substitute backend
    sanction: Sanction
    exec_modes: frozenset[ExecMode]
    log_mode: LogMode
    persistence: frozenset[Persistence]
    gpu_types: frozenset[GpuType]              # what this backend can grant (not guaranteed available)
    max_runtime: timedelta | None              # None == effectively unbounded for batch
    requires_open_browser: bool = False        # colab-mcp bridge
    requires_external_storage: bool = True     # ephemeral VM -> must externalize artifacts
    supports_parameters: bool = False          # papermill-style param injection
    supports_cancel: bool = True
    supports_notebook_ipynb: bool = True       # can run a full .ipynb
    concurrent_session_limit: int | None = None  # None == unknown/unbounded
    cost_model: str = "unknown"                # "flat-subscription" | "pay-per-second" | "free-quota" | "gcp-pay-as-you-go"
    notes: tuple[str, ...] = ()                # human-facing caveats, e.g. "T4/L4/A100/H100 vary over time"
    probed_at: datetime | None = None          # when a live probe last confirmed this


class JobSpec(BaseModel):
    """The backend-neutral description of work to run. The provider abstraction's input."""
    model_config = ConfigDict(extra="forbid")

    # Exactly one of: code, notebook_path, script_path
    code: str | None = None
    notebook_path: str | None = None           # local .ipynb to upload+run
    script_path: str | None = None             # local .py / uv-script
    entrypoint: str | None = None              # for batch backends: "train.py" inside the package

    parameters: dict[str, object] = Field(default_factory=dict)  # papermill injection (if supported)
    requirements: tuple[str, ...] = ()         # pip deps
    env: dict[str, str] = Field(default_factory=dict)

    requirement: "ResourceRequirement"
    artifacts_in: tuple["Artifact", ...] = ()  # files to stage in
    artifacts_out: tuple[str, ...] = ()        # glob paths to collect out
    idempotency_key: str | None = None         # dedupe re-submits across fallback attempts

    @field_validator("code", "notebook_path", "script_path")
    @classmethod
    def _exactly_one_source(cls, v, info):
        return v  # cross-field validation done in model_validator below

    # (model_validator enforces exactly-one-of code/notebook_path/script_path)


class ResourceRequirement(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_gpu: GpuType = GpuType.T4
    gpu_count: int = 1
    high_ram: bool = False
    exec_mode: ExecMode = ExecMode.BATCH
    need_live_logs: bool = False
    need_persistence: bool = False
    max_runtime: timedelta | None = None
    must_be_colab: bool = False                # hard pin: never fall back off Colab
    allowed_sanctions: frozenset[Sanction] = frozenset(  # escape-hatch excluded by default
        {Sanction.SANCTIONED, Sanction.SANCTIONED_INTERACTIVE}
    )
    cost_ceiling_usd: float | None = None      # advisory; backends self-report estimated cost


class JobState(str, enum.Enum):
    PENDING = "pending"
    PROVISIONING = "provisioning"   # allocating runtime / pulling image / cold start
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LOST = "lost"                   # runtime reclaimed/preempted; state may be unrecoverable


class JobHandle(BaseModel):
    """Opaque-ish handle returned by submit(). Serializable so the CLI can persist it."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend_name: str
    job_id: str                     # backend-native id
    runtime_id: str | None = None   # Colab runtime / Modal sandbox / Vertex operation
    exec_mode: ExecMode
    submitted_at: datetime
    extra: dict[str, str] = Field(default_factory=dict)  # backend-private fields (proxy url, gcs uri...)


class JobStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    state: JobState
    detail: str | None = None
    granted_gpu: GpuType | None = None     # what was ACTUALLY allocated (may be downgraded)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    cost_estimate_usd: float | None = None


class LogChunk(BaseModel):
    model_config = ConfigDict(frozen=True)
    stream: str                     # "stdout" | "stderr" | "kernel"
    text: str
    ts: datetime


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    uri: str | None = None          # gs://, drive://, modal-vol://, file://
    size_bytes: int | None = None
    mime: str | None = None
```

### The `Backend` Protocol (`providers/base.py`)

The interface is the five core verbs from the architecture (`submit / status / logs / fetch / cancel`) plus `capabilities()` for negotiation and `estimate_cost()` for the cost-ceiling check. Notebook/file ops are expressed *through* `JobSpec.artifacts_in/out` rather than as separate verbs, so a single contract covers code, scripts, and `.ipynb`.

```python
from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    name: str
    priority: int            # lower == preferred; Colab=0, Modal=10, Vertex=20, HF=30, Kaggle=40, RunPod=50

    def capabilities(self) -> Capabilities: ...

    async def probe(self) -> Capabilities:
        """Live capability/health check (auth valid, quota not exhausted). May be cached."""

    async def estimate_cost(self, spec: JobSpec) -> float | None: ...

    async def submit(self, spec: JobSpec) -> JobHandle:
        """Allocate runtime + start work. Raises a typed BackendError on failure."""

    async def status(self, handle: JobHandle) -> JobStatus: ...

    async def logs(
        self, handle: JobHandle, *, follow: bool = False
    ) -> AsyncIterator[LogChunk]:
        """LIVE backends stream; POLL_THEN_FETCH backends yield once, after completion."""

    async def fetch(self, handle: JobHandle, dest_dir: str) -> list[Artifact]:
        """Pull artifacts_out to dest_dir. Externalizes durable state off the ephemeral VM."""

    async def cancel(self, handle: JobHandle) -> None: ...
```

Concrete backends subclass `BaseBackend(ABC)`, which provides: capability caching, a `_normalize_gpu()` helper (map abstract `GpuType` → backend enum, raising `UnsupportedFeatureError` when impossible), retry/backoff wrappers around transport calls, and a default `estimate_cost()` returning `None` (unknown). Only the transport-specific bodies are implemented per backend.

#### Error taxonomy (`providers/errors.py`)

The fallback engine routes on error *class*, not message strings. This is the contract that makes Colab's opaque failures actionable.

| Exception | Meaning | Fallback engine reaction |
|---|---|---|
| `TransientBackendError` | timeout, 5xx, socket hangup, websocket drop | retry same backend (bounded), then fall over |
| `QuotaExhaustedError` | `TooManyAssignmentsError`, `QUOTA_DENIED`, Kaggle 30h/wk cap, Modal concurrency cap | **skip** this backend, fall over immediately |
| `AccountBannedError` | `DENYLISTED`, "suspected abusive activity", session terminated | **skip + cool-down** (mark backend unhealthy N hours), fall over, surface loudly |
| `BackendUnavailableError` | SDK missing, creds absent, region stockout, capacity denial | fall over immediately |
| `UnsupportedFeatureError` | backend cannot meet a hard requirement (e.g. no H100, no live logs) | excluded at selection time, never reached at submit |
| `PermanentJobError` | user code raised, bad spec, validation failure | **do not** fall over — the next backend would fail identically |
| `AuthExpiredError` | refresh token dead (7-day testing death), proxy token unrefreshable | trigger re-auth hook; fall over if re-auth not interactive |

Mapping backend-native signals into this taxonomy is each backend's responsibility. For Colab specifically: HTTP `412`/`TooManyAssignmentsError` → `QuotaExhaustedError`; quota `Outcome == DENYLISTED` or a "suspected abusive activity" body → `AccountBannedError`; a `tokenExpiresInSeconds` lapse that fails to refresh → `AuthExpiredError`; `/tun/m/*` `5xx`/socket hangup → `TransientBackendError`.

### Capability Negotiation (`providers/capabilities.py` + `selector.py`)

Capability negotiation answers: *given this `ResourceRequirement`, which backends can satisfy it, ranked best-first?* It is a two-phase filter — **static match** (from cached `Capabilities`) then **live probe** (only on the surviving candidates, to confirm auth/quota/capacity right now).

```python
def static_match(req: ResourceRequirement, cap: Capabilities) -> tuple[bool, str | None]:
    if not cap.available:
        return False, "backend unavailable (SDK/creds missing)"
    if cap.sanction not in req.allowed_sanctions:
        return False, f"sanction {cap.sanction} not in allowed set"
    if req.must_be_colab and not cap.is_colab:
        return False, "must_be_colab pin set; backend is not Colab"
    if req.exec_mode not in cap.exec_modes:
        return False, f"exec_mode {req.exec_mode} unsupported"
    if req.need_live_logs and cap.log_mode is not LogMode.LIVE:
        return False, "live logs required, backend is poll-then-fetch"
    if req.need_persistence and Persistence.EPHEMERAL == frozenset(cap.persistence):
        # ephemeral-only backends fail a hard persistence need unless a Drive/Volume sidecar is attached
        return False, "durable persistence required, backend is ephemeral-only"
    # GPU: backend must be able to grant a GPU >= min_gpu by cross-backend rank
    if req.min_gpu is not GpuType.NONE:
        best = max((GPU_RANK[g] for g in cap.gpu_types), default=-1)
        if best < GPU_RANK[req.min_gpu]:
            return False, f"no GPU >= {req.min_gpu} (best={best})"
    if req.max_runtime and cap.max_runtime and req.max_runtime > cap.max_runtime:
        return False, f"job needs {req.max_runtime} > backend cap {cap.max_runtime}"
    return True, None
```

#### `BackendSelector.rank()`

```python
class BackendSelector:
    def __init__(self, registry: ProviderRegistry, policy: FallbackPolicy): ...

    async def rank(self, req: ResourceRequirement) -> list[BackendCandidate]:
        # 1. static filter over cached capabilities
        survivors = [
            BackendCandidate(be, reason=None)
            for be in self.registry.all()
            if (ok := static_match(req, be.capabilities()))[0]
        ]
        # 2. sort by (policy preference, priority, gpu-fit tightness, cost)
        survivors.sort(key=self._score_key(req))
        # 3. live-probe ONLY the top-K (default 3) to confirm auth/quota/health NOW
        confirmed: list[BackendCandidate] = []
        for cand in survivors[: self.policy.probe_top_k]:
            try:
                cap = await self._probe_cached(cand.backend)  # honors probe_ttl
                if static_match(req, cap)[0] and cap.available:
                    confirmed.append(cand)
            except (BackendUnavailableError, QuotaExhaustedError, AuthExpiredError) as e:
                cand.skip_reason = str(e)        # logged, dropped from ordered list
        return confirmed + survivors[self.policy.probe_top_k:]  # unprobed kept as deep fallbacks
```

**Scoring key** (`_score_key`) orders candidates by, in priority:

1. Honors `policy.prefer` (explicit per-call backend pin or ordered preference list).
2. `backend.priority` (Colab `0` first when eligible).
3. **GPU-fit tightness** — prefer the backend whose smallest satisfying GPU is closest to `min_gpu` (don't burn an H100 backend for a T4 job).
4. `cost_estimate` ascending (cheapest among equals; flat-subscription Colab beats pay-per-second).
5. Stable tiebreak on `backend.name`.

**Capability caching.** `CapabilityProbe` caches `probe()` results for `policy.probe_ttl` (default 90 s) keyed by backend name, with a forced refresh after any `AccountBannedError`/`AuthExpiredError`. Static `capabilities()` is cheap and uncached; `probe()` makes a real auth/quota check (e.g. Colab `/tun/m/ccu-info`, Modal token validate, Kaggle `kernels list`) and is rate-limited. This keeps the hot path off the network while still catching "Colab is banned right now."

### Backend Registry (`providers/registry.py`)

```python
@dataclass
class ProviderRegistry:
    _factories: dict[str, Callable[[Settings], Backend]] = field(default_factory=dict)
    _instances: dict[str, Backend] = field(default_factory=dict)

    def register(self, name: str, factory, *, priority: int) -> None: ...
    def get(self, name: str, settings: Settings) -> Backend: ...     # lazy-instantiate
    def all(self) -> list[Backend]: ...                              # instantiated + available
    def by_capability(self, gpu: GpuType) -> list[Backend]: ...      # capability index lookup
```

Bootstrap registers built-ins with their priorities. A backend whose optional extra is uninstalled registers a stub whose `capabilities().available is False`, so it is filtered out at selection without an `ImportError`. Third parties extend via the entry-point group `colabctl.backends`:

```toml
# pyproject.toml of a plugin package
[project.entry-points."colabctl.backends"]
mybackend = "mypkg.backend:MyBackend"
```

`registry.py` discovers these via `importlib.metadata.entry_points(group="colabctl.backends")` at startup; each must satisfy the `Backend` `@runtime_checkable` protocol or registration is rejected with a clear error.

### Fallback Policy & Engine (`providers/fallback.py`)

```python
class FallbackPolicy(BaseModel):
    enabled: bool = True
    auto: bool = False                  # if False, fallback requires explicit opt-in per call OR confirm hook
    prefer: tuple[str, ...] = ()        # ordered backend-name preference, e.g. ("colab", "modal")
    max_attempts: int = 4
    per_backend_retries: int = 1        # transient retries before failing over
    probe_top_k: int = 3
    probe_ttl_s: int = 90
    ban_cooldown_s: int = 6 * 3600      # mark a banned/denylisted backend unhealthy this long
    allow_escape_hatch: bool = False    # opt-in /tun/m/* direct client
    confirm_cross_semantics: bool = True  # require confirm hook before is_colab -> non-Colab fallback
    cost_ceiling_usd: float | None = None
```

**Default stance: fallback is *armed but consent-gated*.** With `auto=False` (default), the engine will retry transient failures on the *same* backend automatically, but a *cross-backend* fall over fires the `on_fallback` confirm hook first when it would change "is-it-Colab" semantics, ToS posture, or cost model. With `auto=True`, fallback proceeds without prompting (the right mode for an autonomous agent that has pre-accepted the routing).

```python
class FallbackEngine:
    def __init__(self, selector, policy, on_fallback=None, on_event=None): ...

    async def submit(self, spec: JobSpec) -> JobResult:
        ledger = AttemptLedger(spec.idempotency_key)
        candidates = await self.selector.rank(spec.requirement)
        if not candidates:
            raise NoEligibleBackendError(self._explain(spec.requirement))

        for cand in candidates[: self.policy.max_attempts]:
            be = cand.backend
            if self._in_cooldown(be):           # recently banned/denylisted
                ledger.skipped(be, "cooldown"); continue
            if self.policy.cost_ceiling_usd is not None:
                est = await be.estimate_cost(spec)
                if est is not None and est > self.policy.cost_ceiling_usd:
                    ledger.skipped(be, f"cost {est} > ceiling"); continue
            if self._is_cross_semantics(cand) and self.policy.confirm_cross_semantics:
                if not await self._confirm(spec, cand, ledger):
                    ledger.skipped(be, "user declined cross-semantics fallback"); continue

            for attempt in range(self.policy.per_backend_retries + 1):
                try:
                    handle = await be.submit(spec)
                    ledger.succeeded(be, handle)
                    return JobResult(handle=handle, ledger=ledger, fell_back=ledger.fell_back)
                except TransientBackendError as e:
                    ledger.transient(be, e)
                    if attempt < self.policy.per_backend_retries:
                        await self._backoff(attempt); continue
                    break  # exhausted retries -> next backend
                except (QuotaExhaustedError, BackendUnavailableError) as e:
                    ledger.skip(be, e); break
                except AccountBannedError as e:
                    ledger.banned(be, e); self._cooldown(be); break
                except AuthExpiredError as e:
                    if await self._try_reauth(be):   # interactive only
                        continue
                    ledger.skip(be, e); break
                except PermanentJobError:
                    raise  # spec/user-code is broken; no backend will succeed
        raise AllBackendsExhaustedError(ledger)
```

**Idempotency across fall over.** `JobSpec.idempotency_key` is threaded into each backend's `submit` (Modal `Function.spawn` tag, HF job name, Vertex `displayName`, Colab assignment label). The `AttemptLedger` records every `(backend, outcome, error_class, ts)` so the CLI/MCP can show *why* it ended up on, say, Modal instead of Colab — and so a re-submit with the same key does not double-allocate paid GPUs.

#### Fallback decision sequence (worked)

```
submit(spec: min_gpu=A100, exec=BATCH, need_live_logs=True, allowed={SANCTIONED})
  rank():
    static filter -> [colab(0), modal(10), vertex(20), hf(30)]   # kaggle dropped (poll-then-fetch != live logs)
    probe top-3:
      colab.probe()  -> QuotaExhaustedError (TooManyAssignmentsError)   => dropped
      modal.probe()  -> ok (A100 available, token valid)
      vertex.probe() -> ok
    confirmed = [modal, vertex, hf]
  engine.submit():
    modal: is_cross_semantics? yes (is_colab False, cost pay-per-second)
           confirm_cross_semantics -> on_fallback("Colab quota-exhausted; run on Modal (~$3.95/hr H-class)?")
           auto=True -> proceed
           modal.submit() -> JobHandle  => SUCCESS, fell_back=True, ledger explains the hop
```

### How the SDK Selects a Backend (top-level flow)

`colabctl/client.py` exposes `ColabClient.run(spec, *, policy=None)`; the CLI (`colabctl run ...`) and the MCP `submit` verb both call into it. Selection order of precedence:

1. **Hard pin** — `spec.requirement.must_be_colab` or `policy.prefer=("modal",)` forces a single backend; if it fails, the engine raises rather than silently substituting (because the user expressed intent).
2. **Capability filter** — `static_match` drops backends that *cannot* satisfy the requirement (no escape-hatch unless `allow_escape_hatch`).
3. **Ranking** — preference list → priority (Colab first) → GPU-fit → cost.
4. **Live probe** of the top-K to confirm the chosen backend is healthy *now*.
5. **Submit with failover** per `FallbackEngine`.

Defaults that make Colab first-class without making it load-bearing: `prefer=("colab",)`, `priority(colab)=0`, `allowed_sanctions={SANCTIONED, SANCTIONED_INTERACTIVE}`, `allow_escape_hatch=False`, `auto=False`. The Colab backend itself internally prefers the **official `google-colab-cli`** transport, falls back to the **`colab-mcp` browser bridge** for interactive-with-browser, and only uses the **`/tun/m/*` escape hatch** when `allow_escape_hatch=True`.

### Worked Example: A Second Backend (`providers/modal_be.py`)

Modal (review score 8, gVisor-isolated, the recommended target for agent-generated code) implementing the full `Backend` contract. This demonstrates exactly what a new backend author must provide.

```python
from datetime import timedelta, datetime, timezone
from typing import AsyncIterator
from .base import BaseBackend
from .models import (Capabilities, JobSpec, JobHandle, JobStatus, JobState, LogChunk,
                     Artifact, GpuType, ExecMode, LogMode, Persistence, Sanction)
from .errors import (BackendUnavailableError, QuotaExhaustedError, TransientBackendError,
                     UnsupportedFeatureError, PermanentJobError)

try:
    import modal
    _HAS_MODAL = True
except ImportError:
    _HAS_MODAL = False

_GPU_MAP = {
    GpuType.T4: "T4", GpuType.L4: "L4", GpuType.A100: "A100",
    GpuType.A100_80: "A100-80GB", GpuType.H100: "H100",
    GpuType.H200: "H200", GpuType.B200: "B200",
}


class ModalBackend(BaseBackend):
    name = "modal"
    priority = 10

    def __init__(self, settings):
        super().__init__(settings)
        self._app = None  # lazy modal.App

    # ---- capability negotiation -------------------------------------------
    def capabilities(self) -> Capabilities:
        return Capabilities(
            backend_name=self.name,
            available=_HAS_MODAL and bool(self.settings.modal_token_id),
            is_colab=False,
            sanction=Sanction.SANCTIONED,
            exec_modes=frozenset({ExecMode.BATCH, ExecMode.INTERACTIVE}),  # Sandbox + Notebooks
            log_mode=LogMode.LIVE,                       # sb.exec() streams stdout/stderr
            persistence=frozenset({Persistence.EPHEMERAL, Persistence.VOLUME}),
            gpu_types=frozenset(_GPU_MAP.keys()),
            max_runtime=timedelta(hours=24),             # sandbox max; default 5min, set per-spec
            requires_external_storage=False,             # modal.Volume is native durable storage
            supports_parameters=True,
            supports_notebook_ipynb=True,                # via Modal Notebooks
            concurrent_session_limit=10,                 # Starter tier: 10 concurrent GPUs
            cost_model="pay-per-second",
            notes=("No free GPU tier (Starter: $30/mo credit). Default 5-min timeout; "
                   "set spec.requirement.max_runtime explicitly for long jobs.",),
            probed_at=None,
        )

    async def probe(self) -> Capabilities:
        if not _HAS_MODAL:
            raise BackendUnavailableError("modal SDK not installed (pip install colabctl[modal])")
        try:
            await modal.config._lookup_token()  # validates MODAL_TOKEN_ID/SECRET
        except Exception as e:
            raise BackendUnavailableError(f"modal auth failed: {e}") from e
        cap = self.capabilities()
        return cap.model_copy(update={"probed_at": datetime.now(timezone.utc)})

    async def estimate_cost(self, spec: JobSpec) -> float | None:
        rate = {GpuType.T4: 0.59, GpuType.L4: 0.80, GpuType.A100: 2.10,
                GpuType.H100: 3.95, GpuType.H200: 4.54, GpuType.B200: 6.25}
        hrs = (spec.requirement.max_runtime or timedelta(hours=1)).total_seconds() / 3600
        return rate.get(spec.requirement.min_gpu, 0.0) * hrs * spec.requirement.gpu_count

    def _gpu(self, req) -> str:
        try:
            spec = _GPU_MAP[req.min_gpu]
        except KeyError:
            raise UnsupportedFeatureError(f"Modal has no GPU type {req.min_gpu}")
        return f"{spec}:{req.gpu_count}" if req.gpu_count > 1 else spec

    # ---- the five core verbs ----------------------------------------------
    async def submit(self, spec: JobSpec) -> JobHandle:
        req = spec.requirement
        image = modal.Image.debian_slim().pip_install(*spec.requirements) if spec.requirements \
            else modal.Image.debian_slim()
        app = modal.App.lookup("colabctl", create_if_missing=True)
        try:
            sb = await modal.Sandbox.create.aio(
                app=app, image=image, gpu=self._gpu(req),
                timeout=int((req.max_runtime or timedelta(hours=1)).total_seconds()),
                volumes=self._mount_volumes(spec),
            )
        except modal.exception.ResourceExhausted as e:   # concurrency / GPU cap
            raise QuotaExhaustedError(f"modal capacity: {e}") from e
        except modal.exception.ConnectionError as e:
            raise TransientBackendError(str(e)) from e

        proc = sb.exec("python", "-c", self._materialize(spec))   # streams later
        return JobHandle(
            backend_name=self.name, job_id=sb.object_id, runtime_id=sb.object_id,
            exec_mode=ExecMode.BATCH, submitted_at=datetime.now(timezone.utc),
            extra={"proc_id": str(id(proc))},
        )

    async def status(self, handle: JobHandle) -> JobStatus:
        sb = await modal.Sandbox.from_id.aio(handle.runtime_id)
        rc = sb.returncode
        if rc is None:
            return JobStatus(state=JobState.RUNNING, granted_gpu=None)
        state = JobState.SUCCEEDED if rc == 0 else JobState.FAILED
        return JobStatus(state=state, exit_code=rc,
                         finished_at=datetime.now(timezone.utc),
                         cost_estimate_usd=None)

    async def logs(self, handle: JobHandle, *, follow: bool = False) -> AsyncIterator[LogChunk]:
        sb = await modal.Sandbox.from_id.aio(handle.runtime_id)
        async for line in sb.stdout:          # LIVE: native streaming iterable
            yield LogChunk(stream="stdout", text=line, ts=datetime.now(timezone.utc))
        async for line in sb.stderr:
            yield LogChunk(stream="stderr", text=line, ts=datetime.now(timezone.utc))

    async def fetch(self, handle: JobHandle, dest_dir: str) -> list[Artifact]:
        # Artifacts written to a modal.Volume; commit + download to dest_dir.
        vol = modal.Volume.from_name(f"colabctl-{handle.job_id}", create_if_missing=True)
        out: list[Artifact] = []
        async for entry in vol.iterdir.aio("/out"):
            local = f"{dest_dir}/{entry.path}"
            await vol.read_file.aio(entry.path, local)
            out.append(Artifact(name=entry.path, uri=f"file://{local}", size_bytes=entry.size))
        return out

    async def cancel(self, handle: JobHandle) -> None:
        sb = await modal.Sandbox.from_id.aio(handle.runtime_id)
        await sb.terminate.aio()   # also stops billing — critical to avoid runaway spend
```

The only Modal-specific knowledge in the file is: the GPU-name map, the SDK calls, and the error-class mapping. Everything routing, ranking, fallback, capability-filtering — is inherited unchanged from `BaseBackend`/the engine. A new backend (Kaggle, RunPod, Vertex) follows the identical template; the table below shows what each declares.

### Backend Capability Matrix (declared `Capabilities`)

| Backend | `is_colab` | sanction | exec_modes | log_mode | persistence | GPUs | max_runtime | cost_model | priority |
|---|---|---|---|---|---|---|---|---|---|
| **Colab** (CLI) | ✅ | sanctioned | interactive+batch | live | ephemeral (+Drive sidecar) | T4/L4/A100/H100/TPU | ~24h Pro keep-alive | flat-subscription | 0 |
| **Colab** (mcp) | ✅ | sanctioned-interactive | interactive | live | ephemeral | same | session-bound | flat-subscription | 0 |
| **Colab** (esc-hatch) | ✅ | escape-hatch (opt-in) | interactive+batch | live | ephemeral | same | ~12h/24h | flat-subscription | 0 |
| **Modal** | ❌ | sanctioned | interactive+batch | live | ephemeral+volume | T4→B200 | 24h (5m default) | pay-per-second | 10 |
| **Vertex/Colab Enterprise** | ❌ | sanctioned | batch | live (Cloud Logging) | GCS | T4/L4/A100/H100/H200/B200/TPU | unbounded (executionTimeout) | gcp-pay-as-you-go | 20 |
| **HF Jobs** | ❌ | sanctioned | batch | live | HF repo/bucket | T4→8×H200 | set via timeout (def 30m!) | pay-per-second | 30 |
| **Kaggle** | ❌ | sanctioned | batch | **poll-then-fetch** | output-only | P100/T4/A100/L4/H100/TPU | ~9h, 30h/wk cap | free-quota | 40 |
| **RunPod / vast** | ❌ | sanctioned | interactive+batch | live | volume | wide marketplace | unbounded | pay-per-second | 50 |

Backends self-report these via `capabilities()`. The matrix is the **negotiation surface**: e.g. a `need_live_logs=True` requirement statically excludes Kaggle; an `exec_mode=INTERACTIVE` requirement statically excludes Vertex/HF/Kaggle (batch-only); `must_be_colab=True` excludes everything but Colab.

### Edge Cases & Failure Handling (specific to this layer)

1. **Colab quota-exhausted (`TooManyAssignmentsError`/412).** Mapped to `QuotaExhaustedError` → engine *skips* Colab (no retry — the cap is per-account and won't clear in seconds) and falls over. The single-account Pro concurrency cap is structural; **never** spin up multiple Colab accounts to scale (the FAQ explicitly bans it) — that is what the *other backends* are for.
2. **Colab denylist / "suspected abusive activity."** Mapped to `AccountBannedError` → engine marks Colab unhealthy for `ban_cooldown_s` (default 6h), falls over, and emits a loud `on_event(BACKEND_BANNED)` so the operator sees it (bans are opaque and unappealable; the abstraction's job is survival, not silent retry).
3. **Cross-semantics fallback (Colab → non-Colab).** Gated by `confirm_cross_semantics`. Falling from flat-rate Colab to pay-per-second Modal/HF, or to a product that *isn't Colab* (Vertex/RunPod), fires `on_fallback` with the cost-model and is-colab delta before spending money. `auto=True` pre-consents.
4. **`must_be_colab` pin + Colab down.** No substitution allowed → raise `NoEligibleBackendError` with the recorded skip reasons rather than quietly using Modal. Honoring user intent beats availability.
5. **GPU downgrade.** A backend may grant a *lesser* GPU than `min_gpu` (Colab/Kaggle silently downgrade TPU+HIGH_RAM; Vertex regional stockout). `status().granted_gpu` reports the actual grant; if `granted_gpu` rank < `min_gpu` rank, the engine raises `UnsupportedFeatureError` post-submit and (if `auto`) re-routes to a backend that can guarantee the GPU.
6. **Live-logs requirement vs poll-then-fetch backend.** Statically excluded at selection — never reaches submit. If the *only* surviving candidate is Kaggle and `need_live_logs=True`, `NoEligibleBackendError` explains the conflict rather than degrading silently.
7. **HF Jobs 30-minute default timeout.** The HF backend MUST inject `timeout` from `req.max_runtime` on every submit; the matrix note flags this so an unset runtime doesn't silently kill jobs at 30m.
8. **Ephemeral state loss on fall over.** Because runtimes are ephemeral, a fall over from a `LOST`/preempted runtime re-runs from the start unless `artifacts_out` checkpoints were already fetched. The engine treats `JobState.LOST` as `TransientBackendError` *only if* an idempotency key + externalized checkpoint exist; otherwise it surfaces `LOST` so the caller decides (re-run vs resume).
9. **Runaway cost on agent loops (Modal/HF/RunPod).** `cost_ceiling_usd` is checked pre-submit via `estimate_cost`; `cancel()` MUST stop billing (e.g. `Sandbox.terminate`, RunPod pod stop). A teardown watchdog in `fallback.py` cancels orphaned handles whose owning process died (reconciliation against the backend's list-jobs endpoint on next client start).
10. **Capability cache staleness.** A backend banned 10 minutes ago must not be re-selected from stale cache; `AccountBannedError`/`AuthExpiredError` force-invalidate the `probe()` cache for that backend and start the cooldown clock.
11. **Unregistered/uninstalled backend referenced in `prefer`.** If `policy.prefer=("modal",)` but the `modal` extra isn't installed, `capabilities().available is False` → `NoEligibleBackendError` with a remediation hint (`pip install colabctl[modal]`), never an `ImportError`.
12. **Escape-hatch never auto-selected.** `Sanction.ESCAPE_HATCH` is excluded from `allowed_sanctions` by default; selecting the `/tun/m/*` Colab transport requires both `allow_escape_hatch=True` *and* the user-acknowledged disclosed-risk flag. It can be a *target* of explicit selection but is never a *fallback destination*.

### Configuration

```toml
# colabctl.toml  (or env vars COLABCTL_*; CLI flags override both)
[providers]
prefer = ["colab", "modal", "vertex"]
allow_escape_hatch = false

[providers.fallback]
enabled = true
auto = false                 # set true for unattended agents that pre-consent to routing
max_attempts = 4
per_backend_retries = 1
probe_top_k = 3
probe_ttl_s = 90
ban_cooldown_s = 21600
confirm_cross_semantics = true
cost_ceiling_usd = 25.0

[providers.colab]
transport = "cli"            # "cli" | "mcp" | "tun"  (tun requires allow_escape_hatch)

[providers.modal]
# token via keyring/env MODAL_TOKEN_ID / MODAL_TOKEN_SECRET
```

The MCP surface exposes this as a `routing` argument on the `submit` verb (`prefer`, `auto`, `must_be_colab`, `allowed_sanctions`), so an AI agent can request, e.g., "Colab only, no fallback" or "anything sanctioned with an A100, auto-route" per call — making the routing policy a first-class, agent-controllable input rather than a hidden global.

### 8.x Key decisions

- The Backend contract is exactly five core verbs (submit/status/logs/fetch/cancel) plus capabilities() for negotiation and estimate_cost() for cost-gating; notebook/file operations are expressed through JobSpec.artifacts_in/out rather than as extra verbs, so one uniform interface covers code, scripts, and .ipynb across every backend.
- Colab is first-class via priority=0 and prefer=['colab'] defaults, but is NOT privileged in the interface — it implements the same Backend protocol as Modal/Kaggle/Vertex, so it can be statically excluded or fallen over from like any other backend. This is the mechanism that contains Colab's churn and ban risk behind a stable surface.
- Capability negotiation is a two-phase filter: a cheap static_match() over cached Capabilities descriptors (GPU rank, exec_mode, log_mode, persistence, sanction, max_runtime, must_be_colab), then a live probe() of only the top-K survivors to confirm auth/quota/health right now (90s TTL cache, force-invalidated on ban/auth errors).
- Fallback routes on a typed error taxonomy, not message strings: QuotaExhausted (skip+failover, no same-backend retry), AccountBanned (skip + 6h cooldown + loud event), Transient (bounded same-backend retry then failover), Unsupported (excluded at selection), Permanent/user-code (do not fall over). Colab's 412/TooManyAssignmentsError, DENYLISTED, and token-expiry signals each map to a specific class.
- Fallback is armed-but-consent-gated by default (auto=False): transient retries on the same backend are automatic, but a cross-semantics fall over (Colab flat-rate -> pay-per-second, or is_colab -> non-Colab) fires an on_fallback confirm hook first. auto=True pre-consents for unattended agents, and the MCP submit verb exposes per-call routing (prefer/must_be_colab/allowed_sanctions).
- The escape-hatch /tun/m/* Colab transport is gated behind Sanction.ESCAPE_HATCH which is excluded from allowed_sanctions by default; it can be an explicitly-pinned target but is NEVER a fallback destination, and requires both allow_escape_hatch=True and a user-acknowledged disclosed-risk flag.
- Idempotency keys are threaded into every backend's submit and recorded in an AttemptLedger so re-submits don't double-allocate paid GPUs across fall over, and the CLI/MCP can explain exactly why a job landed on Modal instead of Colab.
- New backends plug in via the Backend @runtime_checkable protocol + the colabctl.backends entry-point group; uninstalled optional extras register as available=False stubs (filtered at selection) rather than raising ImportError. The worked Modal backend shows that only GPU-mapping, SDK calls, and error-class mapping are backend-specific — all routing/ranking/fallback is inherited.

### 8.y Section risks

- The whole layer's value rests on the Colab backend's transport actually working; the provider abstraction contains Colab fragility but does not fix it — if every sanctioned backend is simultaneously unavailable (e.g. Colab banned + no Modal/Vertex credits), the engine correctly raises AllBackendsExhausted, but the product still cannot run the user's Colab Pro job. The abstraction buys survivability, not a guarantee.
- Cross-backend GPU-rank matching (GPU_RANK) is a coarse cross-product ordering; a job tuned for an A100-80G on Colab may behave differently on a Modal A100-80GB or a Kaggle P100 (driver, CUDA, memory, TPU-vs-GPU semantics). Capability negotiation matches GPU *class*, not behavioral equivalence — silent correctness/perf differences after fall over are possible and must be flagged to the caller, not hidden.
- Cost runaway on pay-per-second fallback targets (Modal/HF/RunPod) is a real operational hazard for autonomous agents in auto=True mode; estimate_cost() is advisory and may be wrong, and the teardown watchdog/cancel-stops-billing contract must be implemented correctly per backend or orphaned GPUs bill indefinitely. cost_ceiling_usd is a pre-submit advisory gate, not a hard spend cap.
- Account-ban detection depends on mapping opaque Colab signals (DENYLISTED outcome, 'suspected abusive activity' body strings) into AccountBannedError; Google can change these strings/shapes without notice, causing a ban to be mis-classified as Transient and triggering retry-into-a-ban. The classifier needs defensive defaults (treat unknown auth/assignment failures conservatively) and must be revisited on every Colab transport version bump.
- probe() live-checks add latency and themselves consume quota/rate limits (e.g. Colab ccu-info, Kaggle kernels list); an aggressive probe_top_k or short TTL under heavy fan-out could itself look like abusive polling to Colab's heuristics. Probe rate-limiting and TTL tuning are load-bearing and under-specified beyond defaults.
- Capability descriptors are hand-maintained per backend (GPU lists, max_runtime, concurrency caps, the HF 30-min default-timeout footgun); these drift as providers change (Kaggle session caps, Modal tiers, Vertex regional stockouts). Stale Capabilities cause either wrongful exclusion (lost availability) or wrongful inclusion (submit-time UnsupportedFeatureError). A periodic capability-refresh/validation job is needed but not yet specified.
- is_colab semantics fallback (must_be_colab pin vs auto-substitution) assumes users understand that Vertex/Modal/RunPod are NOT Colab Pro and have different cost/storage/identity models; if the confirm hook is suppressed or auto=True is set carelessly, a user can be silently moved off the product they actually wanted onto a pricier substitute — a UX/trust risk the consent gate only partially mitigates.

---

## 9. Python SDK & CLI Surface

This section specifies the developer-facing surface of `colabctl`: the Python SDK (`colabctl` package) and the Typer-based CLI (`colabctl` console script). It is the human ergonomics layer that sits **on top of** the provider abstraction (`submit/status/logs/fetch/cancel` + notebook/file verbs from Layer 6) and the transport layer (Layer 3, official `google-colab-cli` primary). The MCP server is a *sibling* consumer of the same abstraction and is specified in its own section; this section only references the shared types it reuses.

Design rules that govern everything below:

1. **One core, two faces.** The SDK and CLI are thin façades over a single async engine (`colabctl.engine`). Sync SDK == `asyncio.run`-wrapped async. CLI == argument parsing + rich rendering over the sync façade. No business logic lives in the SDK/CLI layers.
2. **Backend-agnostic by default, Colab-first in practice.** Every public verb accepts a `backend=` selector; the default is resolved from config/capability detection. The same code runs against Colab, Modal, Vertex, etc. The decorators (`@colab.gpu`) are *Colab-flavored ergonomic sugar* but route through the abstraction.
3. **The transport fragility never leaks into signatures.** Token-only proxy auth, `/tun/m/*` churn, the Python-3.13 subprocess interop for the official CLI — all are hidden behind the engine. The SDK surface is stable even as Layer 3 is swapped.
4. **Capabilities are first-class.** Callers can branch on `backend.capabilities` (live-logs vs poll, interactive vs batch) instead of `try/except`-ing feature gaps.
5. **Ephemerality is explicit.** Anything returned from a runtime is transient unless persisted to Drive/GCS via the file verbs. The SDK makes "where does this survive?" obvious.

---

### Package layout

```
src/colabctl/
├── __init__.py            # re-exports: Client, AsyncClient, colab (decorator ns), exceptions, models
├── _version.py
├── client.py              # Client (sync façade)
├── aio.py                 # AsyncClient (async-native)
├── engine.py              # internal async engine; owns provider abstraction wiring
├── decorators.py          # colab.remote / colab.gpu / colab.cpu / colab.tpu
├── session.py             # RuntimeSession + AsyncRuntimeSession context managers
├── exceptions.py          # exception hierarchy
├── models.py              # pydantic v2 models (re-exported from providers where shared)
├── config.py              # ColabctlConfig, layered config resolution, profiles
├── serialization.py       # cloudpickle-based function shipping for decorators
├── streaming.py           # StreamEvent, async iterators, output multiplexing
├── _sync.py               # internal sync<->async bridge (run_coro, AsyncToSyncIterator)
├── providers/             # (owned by the provider-abstraction section; imported here)
│   ├── base.py            # Provider Protocol, Capability flags
│   ├── colab.py
│   ├── modal.py
│   └── ...
└── cli/
    ├── __init__.py
    ├── main.py            # Typer app root, global callbacks
    ├── render.py          # rich tables/panels/spinners, --json/--quiet handling
    ├── runtime.py         # `colabctl runtime ...`
    ├── run.py             # `colabctl run ...`
    ├── exec.py            # `colabctl exec ...`
    ├── nb.py              # `colabctl nb ...`
    ├── fs.py              # `colabctl fs ...` / `colabctl drive ...`
    ├── auth.py            # `colabctl auth ...`
    ├── backend.py         # `colabctl backend ...`
    └── config_cmd.py      # `colabctl config ...`
```

---

### Core data models (`colabctl.models`)

These pydantic v2 models are the SDK's public vocabulary. Several are re-exported from the provider abstraction so the same object flows from engine → SDK → CLI/MCP unchanged. `model_config = ConfigDict(frozen=True, extra="forbid")` for value objects; mutable status objects use `frozen=False`.

```python
from __future__ import annotations
import datetime as dt
from enum import StrEnum
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, AnyUrl


class Accelerator(StrEnum):
    NONE = "none"
    T4 = "t4"
    L4 = "l4"
    A100 = "a100"
    H100 = "h100"
    V5E = "v5e"      # TPU
    V6E = "v6e"      # TPU
    ANY_GPU = "any-gpu"   # abstract request; engine maps to a concrete grant


class MachineShape(StrEnum):
    STANDARD = "standard"
    HIGH_RAM = "high-ram"


class RuntimeState(StrEnum):
    REQUESTED = "requested"
    PROVISIONING = "provisioning"
    READY = "ready"
    BUSY = "busy"
    IDLE = "idle"
    RECLAIMED = "reclaimed"     # idle/24h reclamation
    TERMINATED = "terminated"
    DENIED = "denied"           # quota/denylist


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class Capability(StrEnum):
    INTERACTIVE = "interactive"           # live kernel, cell-by-cell
    BATCH = "batch"                       # submit-and-poll
    LIVE_LOGS = "live_logs"               # streamed stdout/stderr
    POLL_LOGS = "poll_logs"               # logs only after completion
    FILE_PUSH = "file_push"               # upload into runtime
    FILE_PULL = "file_pull"               # download from runtime
    DRIVE_SYNC = "drive_sync"             # durable Drive/GCS sync
    KEEPALIVE = "keepalive"               # supports long-lived sessions
    NOTEBOOK_NATIVE = "notebook_native"   # runs .ipynb directly


class ResourceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    accelerator: Accelerator = Accelerator.ANY_GPU
    accelerator_count: int = 1
    machine_shape: MachineShape = MachineShape.STANDARD
    min_ram_gb: int | None = None
    region_hint: str | None = None
    # Cost guardrails (Modal/Vertex/RunPod honor; Colab ignores w/ warning)
    max_cost_usd: float | None = None
    max_runtime_seconds: int | None = None


class RuntimeProxyInfo(BaseModel):
    """Header-only proxy credential. NEVER a Bearer token, NEVER a query param.
    Mirrors the corrected auth recipe: distinct from the OAuth identity token."""
    model_config = ConfigDict(frozen=True)
    url: AnyUrl
    proxy_token: str = Field(repr=False)                 # X-Colab-Runtime-Proxy-Token
    tunnel: bool = True                                  # X-Goog-Colab-Tunnel: true
    xsrf_token: str | None = Field(default=None, repr=False)  # X-Goog-Colab-Token
    token_expires_at: dt.datetime                        # from tokenExpiresInSeconds
    client_agent: str = "colabctl"                       # X-Goog-Colab-Client-Agent


class QuotaOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)
    outcome: Literal["success", "denylisted", "quota_exceeded", "too_many_assignments"]
    compute_units_remaining: float | None = None
    message: str | None = None


class Runtime(BaseModel):
    """A live (or once-live) execution context."""
    model_config = ConfigDict(frozen=False, extra="ignore")
    id: str
    backend: str
    state: RuntimeState
    granted_accelerator: Accelerator
    machine_shape: MachineShape
    capabilities: frozenset[Capability]
    created_at: dt.datetime
    expires_at: dt.datetime | None = None       # best-effort idle/lifetime estimate
    proxy: RuntimeProxyInfo | None = Field(default=None, repr=False)
    quota: QuotaOutcome | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class ExecResult(BaseModel):
    """Result of a single code execution over a kernel."""
    model_config = ConfigDict(frozen=True)
    execution_count: int | None
    status: Literal["ok", "error", "abort"]
    stdout: str = ""
    stderr: str = ""
    results: list[dict[str, Any]] = Field(default_factory=list)  # display_data / execute_result mimebundles
    error_name: str | None = None
    error_value: str | None = None
    traceback: list[str] = Field(default_factory=list)
    started_at: dt.datetime
    finished_at: dt.datetime

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def text(self) -> str:
        """Best-effort plain-text result (last text/plain mimebundle, else stdout)."""
        for r in reversed(self.results):
            if "text/plain" in r:
                return r["text/plain"]
        return self.stdout


class Job(BaseModel):
    """A submitted unit of work (notebook run, function call, or batch script)."""
    model_config = ConfigDict(frozen=False)
    id: str
    backend: str
    state: JobState
    runtime_id: str | None = None
    submitted_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    exit_code: int | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    cost_usd: float | None = None


class Artifact(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    uri: AnyUrl                       # drive://, gcs://, file://, or runtime://
    size_bytes: int | None = None
    mime_type: str | None = None
    durable: bool                     # True iff externalized to Drive/GCS


class StreamEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["stdout", "stderr", "status", "result", "error", "heartbeat", "lifecycle"]
    timestamp: dt.datetime
    text: str | None = None
    data: dict[str, Any] | None = None     # mimebundle for result, state for lifecycle
    job_id: str | None = None
    runtime_id: str | None = None
```

---

### Exception hierarchy (`colabctl.exceptions`)

A clean, catchable tree. Every exception carries structured fields, never just a string. The hierarchy separates **transport/fragility** errors (you may retry or route to another backend), **policy/quota** errors (the user must act), and **programmer** errors (bad input). All inherit from `ColabctlError`.

```python
class ColabctlError(Exception):
    """Root. All library errors derive from this. Carries .hint for CLI rendering."""
    hint: str | None = None
    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint

# --- Configuration / programmer errors --------------------------------------
class ConfigError(ColabctlError): ...
class UnknownBackendError(ConfigError): ...
class InvalidRequestError(ColabctlError): ...          # bad ResourceRequest, etc.
class SerializationError(ColabctlError): ...           # cloudpickle failed on @colab.gpu fn

# --- Authentication ----------------------------------------------------------
class AuthError(ColabctlError): ...
class NotAuthenticatedError(AuthError):                # no creds for backend
    def __init__(self, backend: str, account: str | None = None): ...
class TokenExpiredError(AuthError):                    # refresh-token death / 7-day testing
    """Raised when re-consent is required; CLI prompts `colabctl auth login`."""
class ScopeNotGrantedError(AuthError): ...             # colaboratory scope unconfirmed (fallback path)

# --- Runtime allocation / lifecycle -----------------------------------------
class RuntimeError_(ColabctlError):
    """Base for runtime allocation/lifecycle. Underscore avoids shadowing builtin."""
class TooManyAssignmentsError(RuntimeError_):          # 412-style per-account cap
    retry_after_seconds: int | None
class QuotaExceededError(RuntimeError_):
    quota: "QuotaOutcome"
class DenylistedError(RuntimeError_):
    """Suspected-abusive-activity block. NOT retryable; surfaces appeal guidance."""
class RuntimeReclaimedError(RuntimeError_):            # idle/24h reclamation mid-session
class ProxyTokenExpiredError(RuntimeError_):           # internal-ish; engine auto-refreshes, raised only if refresh fails
class AcceleratorUnavailableError(RuntimeError_):      # requested A100 -> degraded/denied
    granted: "Accelerator | None"

# --- Execution ---------------------------------------------------------------
class ExecutionError(ColabctlError):
    """A cell/job ran but produced a Python error. Carries the ExecResult."""
    result: "ExecResult"
class ExecutionTimeoutError(ExecutionError): ...
class KernelDeadError(ExecutionError): ...             # websocket dropped / kernel died

# --- Transport / fragility (retry or route-around) --------------------------
class TransportError(ColabctlError):
    """Undocumented-endpoint drift, websocket hangups, XSSI surprises."""
    retryable: bool = True
class BackendUnavailableError(TransportError): ...     # backend down/degraded -> abstraction may failover
class FileSyncError(ColabctlError): ...
class DriveQuotaError(FileSyncError): ...               # user My Drive full (NOT SA 403)

# --- Job orchestration -------------------------------------------------------
class JobFailedError(ColabctlError):
    job: "Job"
class JobNotFoundError(ColabctlError): ...
```

**Mapping discipline:** the engine is the *only* place that translates transport-layer signals (HTTP 412, `)]}'` XSSI prefix, `DENYLISTED` outcome, socket hangup) into this hierarchy. The provider adapters raise provider-native errors; the engine's `_normalize_error()` maps them. This keeps SDK consumers insulated from "which transport am I on."

---

### `Client` (sync) and `AsyncClient` (async)

The two clients share an identical verb surface. `Client` is `asyncio.run`-wrapping `AsyncClient`. `AsyncClient` owns one `Engine` and an `httpx.AsyncClient`/websocket pool.

#### Construction & lifecycle

```python
class AsyncClient:
    def __init__(
        self,
        *,
        backend: str | None = None,            # None -> resolved from config (default "colab")
        account: str | None = None,            # email; per-account keyring lookup
        config: ColabctlConfig | None = None,  # explicit override; else layered resolution
        timeout: float = 600.0,
        escape_hatch: bool = False,            # opt-in raw /tun/m/* transport for colab
    ) -> None: ...

    async def __aenter__(self) -> "AsyncClient": ...
    async def __aexit__(self, *exc) -> None: ...   # closes pools, releases idle runtimes per policy
    async def aclose(self) -> None: ...

class Client:
    def __init__(self, *, backend=None, account=None, config=None,
                 timeout=600.0, escape_hatch=False) -> None: ...
    def __enter__(self) -> "Client": ...
    def __exit__(self, *exc) -> None: ...
    def close(self) -> None: ...
```

#### Verb surface (identical on both; `async def` on `AsyncClient`)

```python
# --- Backend / capability introspection -------------------------------------
def backends(self) -> list[BackendInfo]: ...
def capabilities(self, backend: str | None = None) -> frozenset[Capability]: ...

# --- Runtime allocation ------------------------------------------------------
def allocate(
    self,
    accelerator: Accelerator | str = Accelerator.ANY_GPU,
    *,
    machine_shape: MachineShape | str = MachineShape.STANDARD,
    accelerator_count: int = 1,
    request: ResourceRequest | None = None,        # full control; overrides scalars
    labels: dict[str, str] | None = None,
    wait: bool = True,                             # block until READY
    keepalive: bool = True,                        # arm the 60s keepalive loop
) -> Runtime: ...

def runtime(self, runtime_id: str) -> Runtime: ...           # refresh status
def runtimes(self) -> list[Runtime]: ...
def release(self, runtime: Runtime | str) -> None: ...       # unassign

# --- Code execution (interactive backends) ----------------------------------
def exec(
    self,
    code: str,
    *,
    runtime: Runtime | str | None = None,   # None -> ephemeral session (allocate+release)
    timeout: float | None = None,
    silent: bool = False,
) -> ExecResult: ...

def exec_stream(                              # AsyncClient -> AsyncIterator; Client -> Iterator
    self, code: str, *, runtime=None, timeout=None
) -> Iterator[StreamEvent]: ...

# --- Notebook & batch (works on batch AND interactive backends) -------------
def run_notebook(
    self,
    path: str | Path,
    *,
    parameters: dict[str, Any] | None = None,    # papermill-style injection
    backend: str | None = None,
    request: ResourceRequest | None = None,
    output: str | Path | None = None,            # local path or drive://... for executed .ipynb
    wait: bool = True,
) -> Job: ...

def submit(self, spec: JobSpec, *, wait: bool = False) -> Job: ...
def status(self, job: Job | str) -> Job: ...
def jobs(self, *, state: JobState | None = None) -> list[Job]: ...
def logs(self, job: Job | str, *, follow: bool = False) -> Iterator[StreamEvent]: ...
def fetch(self, job: Job | str, *, dest: str | Path = ".") -> list[Artifact]: ...
def cancel(self, job: Job | str) -> Job: ...
def wait(self, job: Job | str, *, timeout: float | None = None,
         poll_interval: float = 5.0) -> Job: ...

# --- File / Drive sync (durable; user-OAuth plain-blob to My Drive) ---------
def push(self, local: str | Path, remote: str, *, runtime=None) -> Artifact: ...   # local -> runtime VM
def pull(self, remote: str, local: str | Path, *, runtime=None) -> Path: ...        # runtime VM -> local
def drive_upload(self, local: str | Path, drive_path: str) -> Artifact: ...         # -> My Drive (durable)
def drive_download(self, drive_path: str, local: str | Path) -> Path: ...
def drive_ls(self, drive_path: str = "/") -> list[Artifact]: ...

# --- Sessions ----------------------------------------------------------------
def session(self, accelerator: Accelerator | str = Accelerator.ANY_GPU,
            **kw) -> "RuntimeSession": ...    # context manager; see below
```

`JobSpec` and `BackendInfo`:

```python
class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str | None = None
    kind: Literal["notebook", "function", "script", "code"] = "code"
    notebook_path: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    code: str | None = None
    command: list[str] | None = None
    payload: bytes | None = Field(default=None, repr=False)   # cloudpickle blob for functions
    request: ResourceRequest = Field(default_factory=ResourceRequest)
    inputs: dict[str, str] = Field(default_factory=dict)      # name -> uri to stage in
    output_dir: str | None = None                             # drive:// or gcs:// (durable)
    labels: dict[str, str] = Field(default_factory=dict)
    idempotency_key: str | None = None

class BackendInfo(BaseModel):
    name: str
    available: bool
    sanctioned: bool                      # Colab-CLI/MCP/Modal/Vertex = True
    capabilities: frozenset[Capability]
    default_accelerators: list[Accelerator]
    notes: str | None = None              # e.g. "interactive only; requires open browser tab"
```

---

### Context managers: `RuntimeSession`

Sessions are the recommended interactive pattern. They allocate once, keep a single kernel/websocket warm, arm the keepalive loop, and **guarantee release** on exit — even on exception or `RuntimeReclaimedError`.

```python
class RuntimeSession:
    """Sync session. Wraps an allocated Runtime + warm kernel."""
    runtime: Runtime

    def __enter__(self) -> "RuntimeSession": ...
    def __exit__(self, *exc) -> None: ...   # release(runtime); cancel keepalive

    def exec(self, code: str, *, timeout=None, silent=False) -> ExecResult: ...
    def exec_stream(self, code: str, *, timeout=None) -> Iterator[StreamEvent]: ...
    def run_notebook(self, path, *, parameters=None, output=None) -> Job: ...
    def push(self, local, remote) -> Artifact: ...
    def pull(self, remote, local) -> Path: ...
    def drive_upload(self, local, drive_path) -> Artifact: ...   # delegates to client
    def interrupt(self) -> None: ...        # interrupt the running kernel
    def restart(self) -> None: ...          # restart kernel, keep VM
    @property
    def alive(self) -> bool: ...            # False after reclamation

class AsyncRuntimeSession:   # identical surface, async def + __aenter__/__aexit__
    ...
```

**Reclamation handling inside a session:** if the engine's keepalive loop detects `RuntimeReclaimedError`, the session marks `alive = False`. The next `exec` raises `RuntimeReclaimedError` with `hint="runtime reclaimed; call client.allocate() again or use restore_on_reclaim=True"`. If the session was created with `restore_on_reclaim=True`, the engine transparently re-allocates a fresh runtime, re-pushes staged inputs, and replays `parameters` — but **in-memory kernel state is lost** and a `StreamEvent(kind="lifecycle", data={"event": "reclaimed_reallocated"})` is emitted so callers know state reset.

---

### Decorators: `@colab.gpu` / `@colab.remote`

`colab` is a namespace object exported from `colabctl`. Decorators ship a *local* Python function to a runtime, execute it there, and return the result locally. This is the headline ergonomic feature.

```python
from colabctl import colab

@colab.gpu(accelerator="a100", pip=["torch==2.4.0", "transformers"])
def finetune(dataset_uri: str, epochs: int = 3) -> dict:
    import torch
    ...
    return {"loss": final_loss, "checkpoint": "drive://models/ft.pt"}

result = finetune("drive://data/train.jsonl", epochs=5)   # runs on a remote A100
```

#### Decorator API

```python
class _ColabNamespace:
    def remote(
        self,
        fn: Callable | None = None,
        *,
        backend: str | None = None,
        accelerator: Accelerator | str = Accelerator.NONE,
        accelerator_count: int = 1,
        machine_shape: MachineShape | str = MachineShape.STANDARD,
        pip: list[str] | None = None,            # installed before fn runs
        apt: list[str] | None = None,
        mounts: dict[str, str] | None = None,    # local path -> remote path, staged via push
        secrets: list[str] | None = None,        # keyring keys exposed as env on the runtime
        timeout: float | None = None,
        retries: int = 0,
        reuse: str | bool = False,               # True/"<label>" -> reuse a labeled warm runtime
        on_reclaim: Literal["raise", "retry"] = "retry",
        serializer: Literal["cloudpickle", "source"] = "cloudpickle",
    ) -> Callable: ...

    # Sugar: fixed accelerator presets
    def gpu(self, fn=None, *, accelerator="any-gpu", **kw): ...   # -> remote(accelerator=...)
    def cpu(self, fn=None, **kw): ...                              # -> remote(accelerator="none")
    def tpu(self, fn=None, *, accelerator="v6e", **kw): ...

colab = _ColabNamespace()
```

#### Decorated-function attributes

The decorated callable is enriched so power users can override per-call and introspect:

```python
finetune.options(accelerator="h100", timeout=3600)(...)  # per-call override -> new bound callable
finetune.submit("drive://data/train.jsonl")              # -> Job (async fire-and-forget)
finetune.map(list_of_dataset_uris, max_parallel=4)       # -> list[result]; fans out across runtimes
await finetune.aio("drive://data/train.jsonl")           # async invocation
finetune.spec                                            # -> the JobSpec template (introspection)
```

#### Function-shipping algorithm (`serialization.py`)

```
ship_and_run(fn, args, kwargs, options):
  1. If serializer == "cloudpickle":
       blob = cloudpickle.dumps((fn, args, kwargs))
       guard: if len(blob) > 5 MiB -> SerializationError (use mounts/Drive for big data)
     else "source":
       extract textwrap.dedent(inspect.getsource(fn)); forbid closures over non-literal globals
  2. runtime = reuse_or_allocate(options)        # honors reuse=<label>
  3. for path in options.mounts: client.push(local, remote)
  4. install: exec("import subprocess,sys; subprocess.run([sys.executable,'-m','pip','install',*pip])")
       -> stream pip output as StreamEvent(kind="stdout"); cache by hash(pip) per runtime label
  5. bootstrap exec on runtime:
       import cloudpickle, base64, traceback, json
       fn, a, kw = cloudpickle.loads(base64.b64decode(_BLOB))
       try:    _RET = cloudpickle.dumps(fn(*a, **kw)); _ERR = None
       except Exception as e: _RET=None; _ERR={"type":type(e).__name__,"msg":str(e),"tb":traceback.format_exc()}
       print(_SENTINEL + base64.b64encode(_RET or b"").decode())
  6. parse sentinel-delimited result from ExecResult.stdout
  7. if _ERR -> raise RemoteFunctionError(type, msg, remote_traceback)   # subclass of ExecutionError
  8. if on_reclaim == "retry" and RuntimeReclaimedError -> goto 2 (count against retries)
  9. cloudpickle.loads(result) -> return value
```

**Edge cases the decorator handles explicitly:**
- **Unpicklable return** (e.g. a torch CUDA tensor): caught remotely, re-raised locally as `SerializationError` with hint to return a Drive URI or `.cpu().numpy()`.
- **Library version skew**: cloudpickle by-reference for importable top-level functions; by-value for `__main__`/notebook functions. The `pip=` list is the contract for remote deps. A `RuntimeWarning` is emitted if a `pip` package's local version differs and the function appears to reference it.
- **Closures over large objects**: blob-size guard at 5 MiB; suggests `mounts=`.
- **Non-deterministic `reuse`**: when `reuse=True` (anonymous), the engine labels the runtime `colabctl/remote/<fn_qualname>` so repeated calls hit the same warm VM and skip pip reinstall.

---

### Streaming model (`colabctl.streaming`)

Streaming is uniform across interactive (live kernel) and batch (poll-then-fetch) backends; the engine adapts based on `Capability.LIVE_LOGS` vs `POLL_LOGS`.

```python
# Async-native
async for ev in client.exec_stream("for i in range(5): print(i)", runtime=rt):
    if ev.kind == "stdout":
        sys.stdout.write(ev.text)

# Sync: AsyncToSyncIterator drains the async generator on the engine loop thread
for ev in sync_client.exec_stream("..."):
    ...
```

- **Interactive backends** yield `stdout`/`stderr`/`result`/`error` events in real time off the Jupyter websocket (`iopub` channel), plus `heartbeat` every 30s and `lifecycle` on state changes.
- **Batch backends without live logs** (Kaggle): the iterator yields a single `status` event on submit, periodic `heartbeat` while polling, then replays the captured log as `stdout` events after completion, ending with `lifecycle{state}`. Callers get one code path.
- **Backpressure**: events flow through a bounded `asyncio.Queue(maxsize=1024)`; on overflow the engine coalesces consecutive `stdout` events and emits a `StreamEvent(kind="status", data={"dropped": n})` so high-frequency output (tqdm) never deadlocks.

---

### Configuration (`colabctl.config`)

Layered resolution, highest precedence first:

1. Explicit kwargs / `config=ColabctlConfig(...)`.
2. Environment variables (`COLABCTL_BACKEND`, `COLABCTL_ACCOUNT`, `COLABCTL_PROFILE`, `COLABCTL_ESCAPE_HATCH`, `COLABCTL_LOG_LEVEL`).
3. `--profile`-selected block in `~/.config/colabctl/config.toml` (XDG-respecting; `%APPDATA%` on Windows).
4. `[default]` block in the same file.
5. Built-in defaults.

```python
class BackendConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    accelerator: Accelerator = Accelerator.ANY_GPU
    extra: dict[str, Any] = Field(default_factory=dict)   # backend-specific (gcp_project, modal_env)

class ColabctlConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = "colab"
    account: str | None = None
    escape_hatch: bool = False                  # raw /tun/m/* opt-in (Colab only)
    keepalive_interval: float = 60.0
    default_timeout: float = 600.0
    drive_root: str = "/colabctl"               # My Drive folder for durable artifacts
    keyring_backend: Literal["auto","keychain","secretservice","wincred","age-file"] = "auto"
    failover: list[str] = Field(default_factory=lambda: ["modal", "vertex"])
    log_level: str = "INFO"
    backends: dict[str, BackendConfig] = Field(default_factory=dict)

    @classmethod
    def load(cls, *, profile: str = "default", path: Path | None = None) -> "ColabctlConfig": ...
```

Example `~/.config/colabctl/config.toml`:

```toml
[default]
backend = "colab"
account = "iris@analyticsandsociety.com"
escape_hatch = false
failover = ["modal", "vertex"]
drive_root = "/colabctl"

[default.backends.colab]
accelerator = "t4"

[default.backends.modal]
[default.backends.modal.extra]
modal_env = "main"

[heavy]                       # `colabctl --profile heavy ...`
backend = "modal"
[heavy.backends.modal]
accelerator = "h100"
```

---

### CLI surface (Typer)

Root app with global options applied via a Typer callback. Output respects `--json` (machine-readable, stable schema = the pydantic `model_dump(mode="json")`), `--quiet` (errors only), and rich rendering otherwise (tables, spinners, live log panels).

#### Global options (root callback)

```
colabctl [GLOBAL OPTIONS] COMMAND [ARGS]

  --profile TEXT          Config profile           [env: COLABCTL_PROFILE]
  --backend TEXT          Override backend          [env: COLABCTL_BACKEND]
  --account TEXT          Account email             [env: COLABCTL_ACCOUNT]
  --json / --no-json      Emit JSON instead of tables
  --quiet, -q             Suppress non-error output
  --no-color              Disable rich styling
  --escape-hatch          Enable raw /tun/m/* Colab transport (disclosed-risk)
  --log-level [DEBUG|INFO|WARNING|ERROR]
  --version, -V
  --help, -h
```

#### Command groups & full command list

| Command | Purpose | Key flags |
|---|---|---|
| `colabctl auth login` | OAuth loopback login (official CLI flow); stores per-account creds in keyring | `--account`, `--backend`, `--device` (manual code for headless) |
| `colabctl auth logout` | Remove stored creds | `--account`, `--all` |
| `colabctl auth status` | Show creds, scopes, token expiry per account/backend | `--json` |
| `colabctl auth refresh` | Force refresh-token rotation | `--account` |
| `colabctl backend list` | List backends + availability + capabilities | `--json` |
| `colabctl backend caps BACKEND` | Show capability matrix for one backend | |
| `colabctl runtime new` | Allocate a runtime | `--gpu/--accelerator`, `--high-ram`, `--count`, `--label`, `--no-wait`, `--no-keepalive` |
| `colabctl runtime list` | List active runtimes | `--json` |
| `colabctl runtime show ID` | Refresh + show one runtime | `--json` |
| `colabctl runtime rm ID` | Release/unassign runtime | `--all` |
| `colabctl runtime keepalive ID` | Re-arm keepalive in foreground (blocks) | `--interval` |
| `colabctl exec` | Run a code snippet/file/stdin on a runtime | `--runtime`, `--gpu`, `--file`, `--stdin`, `--timeout`, `--silent` |
| `colabctl shell` | Interactive REPL bound to a runtime | `--runtime`, `--gpu` |
| `colabctl run NOTEBOOK` | Run a `.ipynb` (papermill-style) | `--param k=v` (repeatable), `--params-file`, `--gpu`, `--output`, `--follow`, `--no-wait` |
| `colabctl submit` | Submit a JobSpec (file or flags) | `--spec FILE`, `--kind`, `--command`, `--gpu`, `--output-dir`, `--no-wait` |
| `colabctl jobs` | List jobs | `--state`, `--json`, `--watch` |
| `colabctl status JOB` | Show job status | `--json` |
| `colabctl logs JOB` | Stream/print logs | `--follow/-f`, `--since`, `--tail N` |
| `colabctl fetch JOB` | Download artifacts | `--dest`, `--name` |
| `colabctl cancel JOB` | Cancel a job | |
| `colabctl wait JOB` | Block until terminal state (good for scripts; sets exit code) | `--timeout` |
| `colabctl nb push LOCAL REMOTE` | Upload file into runtime VM | `--runtime` |
| `colabctl nb pull REMOTE LOCAL` | Download file from runtime VM | `--runtime` |
| `colabctl drive upload LOCAL DRIVE_PATH` | Durable upload to My Drive | |
| `colabctl drive download DRIVE_PATH LOCAL` | Durable download from My Drive | |
| `colabctl drive ls [DRIVE_PATH]` | List Drive folder | `--json` |
| `colabctl config show` | Print effective merged config | `--json` |
| `colabctl config init` | Scaffold `config.toml` | `--profile` |
| `colabctl config set KEY VALUE` | Set a config value | `--profile` |
| `colabctl doctor` | Diagnose: CLI interop (Py3.13 env), keyring backend, auth, backend reachability | `--json` |
| `colabctl version` | Versions of colabctl + vendored google-colab-cli + transport probe | `--json` |

#### Exit codes (script-friendly)

| Code | Meaning | Raised by |
|---|---|---|
| 0 | Success | |
| 1 | Generic `ColabctlError` | catch-all |
| 2 | Usage / bad args (Typer) | `InvalidRequestError`, Typer |
| 10 | Auth required / token expired | `NotAuthenticatedError`, `TokenExpiredError` |
| 11 | Quota / too-many-assignments | `QuotaExceededError`, `TooManyAssignmentsError` |
| 12 | Denylisted (abuse block) | `DenylistedError` |
| 13 | Runtime reclaimed / unavailable | `RuntimeReclaimedError`, `BackendUnavailableError` |
| 20 | Execution error in user code | `ExecutionError`, `JobFailedError` (sets via `exit_code`) |
| 21 | Execution timeout | `ExecutionTimeoutError` |
| 30 | Transport/fragility error | `TransportError` |

`colabctl wait JOB` and `colabctl run --wait` propagate the remote job's `exit_code` (offset into the 20-range on failure) so they compose in shell pipelines and CI.

#### Output rendering (`cli/render.py`)

- **Tables** via `rich.table.Table`; one renderer per model (`render_runtime`, `render_job`, `render_backends`). Column sets are fixed and documented; `--json` bypasses rendering entirely and prints `model_dump_json(indent=2)`.
- **Live logs** via `rich.live.Live` panel for `--follow`; falls back to plain line streaming when `--no-color` or non-TTY (CI). Each `StreamEvent` maps: `stdout`→default, `stderr`→dim red, `status`/`lifecycle`→cyan italic header.
- **Spinners** (`rich.status`) during `allocate`/`provisioning`; auto-disabled under `--quiet`/`--json`/non-TTY.
- **Error rendering**: top-level Typer exception handler catches `ColabctlError`, prints `panel(message, title=type, subtitle=hint)`, maps to the exit code table. Under `--json`, errors print `{"error": {"type","message","hint"}}` to stderr.

---

### End-to-end usage examples

#### 1. Sync SDK — allocate, exec, persist to Drive

```python
from colabctl import Client, Accelerator

with Client(account="iris@analyticsandsociety.com") as cc:
    with cc.session(accelerator=Accelerator.T4) as s:
        print("granted:", s.runtime.granted_accelerator)        # may downgrade A100->T4
        r = s.exec("import torch; print(torch.cuda.get_device_name(0))")
        assert r.ok, r.error_value
        print(r.text)

        # train... write checkpoint to the ephemeral VM, then externalize (durable!)
        s.exec("open('/tmp/model.pt','wb').write(b'...weights...')")
        s.pull("/tmp/model.pt", "model.pt")                       # to local disk
        art = s.drive_upload("model.pt", "/colabctl/run-42/model.pt")
        print("durable:", art.durable, art.uri)                  # drive://...
    # session exit -> runtime released, keepalive cancelled
```

#### 2. Async SDK — stream a long cell

```python
import asyncio
from colabctl import AsyncClient

async def main():
    async with AsyncClient(backend="colab") as cc:
        rt = await cc.allocate("a100", machine_shape="high-ram")
        async for ev in cc.exec_stream(
            "for i in range(3):\n  import time; time.sleep(1); print('step', i)",
            runtime=rt,
        ):
            if ev.kind == "stdout":
                print(ev.text, end="")
            elif ev.kind == "lifecycle":
                print("[lifecycle]", ev.data)
        await cc.release(rt)

asyncio.run(main())
```

#### 3. The `@colab.gpu` decorator — run a local function remotely

```python
from colabctl import colab

@colab.gpu(accelerator="a100", pip=["torch==2.4.0"], mounts={"./train.jsonl": "/data/train.jsonl"})
def train(epochs: int = 3) -> dict:
    import torch, json
    # /data/train.jsonl was staged by mounts=
    # ... real training ...
    torch.save({"w": 1}, "/tmp/ckpt.pt")
    return {"epochs": epochs, "device": torch.cuda.get_device_name(0)}

# Blocking call, runs on a remote A100, returns the dict locally:
out = train(epochs=5)
print(out)            # {'epochs': 5, 'device': 'NVIDIA A100-SXM4-40GB'}

# Fan-out across runtimes:
results = train.options(accelerator="t4").map([1, 2, 3], max_parallel=3)

# Fire-and-forget -> Job:
job = train.submit(epochs=10)
print(job.id, job.state)
```

#### 4. Notebook batch run with parameter injection + durable output

```python
from colabctl import Client

with Client() as cc:
    job = cc.run_notebook(
        "experiments/sweep.ipynb",
        parameters={"lr": 3e-4, "batch_size": 64, "dataset": "drive://data/v3"},
        request=None,
        output="drive://colabctl/results/sweep-out.ipynb",   # durable executed notebook
        wait=False,
    )
    for ev in cc.logs(job, follow=True):
        if ev.kind in ("stdout", "stderr"):
            print(ev.text, end="")
    job = cc.wait(job)
    if job.state.value != "succeeded":
        raise SystemExit(job.exit_code or 1)
    arts = cc.fetch(job, dest="./out")
    print("artifacts:", [a.name for a in arts])
```

#### 5. Capability-aware backend routing (no try/except)

```python
from colabctl import Client, Capability

with Client(backend="kaggle") as cc:                 # poll-then-fetch backend
    caps = cc.capabilities()
    job = cc.run_notebook("eval.ipynb", parameters={"n": 1000})
    if Capability.LIVE_LOGS in caps:
        for ev in cc.logs(job, follow=True):
            print(ev.text, end="")
    else:
        cc.wait(job)                                  # batch: just block
        for ev in cc.logs(job):                       # replayed after completion
            print(ev.text, end="")
```

#### 6. CLI — full interactive + batch workflow

```bash
# One-time auth (opens browser loopback; stores per-account creds in keyring)
colabctl auth login --account iris@analyticsandsociety.com

# Diagnose environment (checks Py3.13 interop env for the official CLI, keyring, reachability)
colabctl doctor

# Allocate a labeled A100 runtime, keep it warm
colabctl runtime new --gpu a100 --high-ram --label sweep
colabctl runtime list

# Run a snippet on it, streaming output
colabctl exec --runtime sweep --stdin <<'PY'
import torch; print(torch.cuda.get_device_name(0))
PY

# Run a parameterized notebook, stream logs, write durable output to Drive
colabctl run experiments/sweep.ipynb \
  --param lr=3e-4 --param batch_size=64 \
  --output drive://colabctl/results/sweep.ipynb \
  --follow

# Submit a long batch job to Modal (failover-class backend), don't wait
colabctl --backend modal submit --spec job.toml --output-dir gcs://my-bucket/out --no-wait
colabctl jobs --state running --watch
colabctl logs <JOB_ID> -f
colabctl fetch <JOB_ID> --dest ./artifacts

# Clean up
colabctl runtime rm --all
```

#### 7. CLI — JSON mode for scripting / agent piping

```bash
RT=$(colabctl runtime new --gpu t4 --no-wait --json | jq -r .id)
colabctl runtime show "$RT" --json | jq .state
colabctl exec --runtime "$RT" --file setup.py --json | jq -r '.stdout'
colabctl runtime rm "$RT"
```

---

### Sequence of operations: `Client.exec(code, runtime=None)`

```
1. Resolve config + account; engine ensures auth (else NotAuthenticatedError -> exit 10).
2. If runtime is None:
   a. allocate(ANY_GPU or config default) -> Runtime (may downgrade accelerator; warn)
   b. mark "ephemeral" so __exit__/finally releases it
3. Ensure kernel: if interactive backend, open/reuse Jupyter websocket using RuntimeProxyInfo:
      headers = {
        "X-Colab-Runtime-Proxy-Token": proxy.proxy_token,   # header-only, NOT Bearer
        "X-Goog-Colab-Tunnel": "true",
        "X-Goog-Colab-Token": proxy.xsrf_token,
        "X-Goog-Colab-Client-Agent": proxy.client_agent,
        "Authorization": f"Bearer {oauth_identity_token}",   # SEPARATE identity creds
      }
   - if proxy.token_expires_at within skew -> engine refreshes via runtime-proxy-token first
4. Send execute_request on shell channel; collect iopub (stream/display_data/execute_result/error)
   until status==idle or timeout.
5. On websocket hangup -> KernelDeadError (retryable once: reconnect; else raise).
6. On kernel error message -> build ExecResult(status="error"); exec() raises ExecutionError(result=...).
   exec_stream() instead yields StreamEvent(kind="error") and returns.
7. Assemble ExecResult; if ephemeral runtime -> release in finally.
8. Map any transport signal through engine._normalize_error -> typed exception.
```

### Edge cases & failure handling specific to this surface

- **Accelerator downgrade**: `allocate("a100")` may grant T4 (silent backend downgrade). The SDK never silently swallows this: `Runtime.granted_accelerator` reflects reality and a `RuntimeWarning` fires if `granted != requested`. `allocate(..., strict=True)` raises `AcceleratorUnavailableError(granted=...)` instead.
- **Token expiry mid-stream**: the engine refreshes the proxy token transparently on the keepalive loop; if refresh fails (`ProxyTokenExpiredError`), an in-flight `exec_stream` emits `StreamEvent(kind="lifecycle", data={"event":"proxy_refresh_failed"})` then raises so the caller can re-allocate.
- **`DenylistedError`**: non-retryable; never auto-failover blindly. The SDK raises with `hint` describing the opaque abuse-block and pointing to backend failover (`config.failover`). The CLI prints appeal guidance and exits 12.
- **Headless auth**: `auth login --device` returns a code+URL for machines without a browser; `TokenExpiredError` from a 7-day testing-mode refresh death is caught and the CLI re-prompts login (exit 10) rather than hanging an unattended agent.
- **Sync-from-async context**: calling sync `Client` methods inside a running event loop raises `RuntimeError_` with a hint to use `AsyncClient`; the bridge never nests `asyncio.run`.
- **Non-TTY / CI**: rendering auto-degrades (no spinners/live panels); `--follow` streams plain lines; `--json` always available and the canonical machine contract.
- **Escape-hatch gating**: `escape_hatch=False` by default; if a user calls a Colab-only raw-transport path without enabling it, `ConfigError` fires with the disclosed-risk explanation, never silently using `/tun/m/*`.
- **Large function blobs**: `@colab.gpu` rejects cloudpickle payloads > 5 MiB (`SerializationError`) and steers to `mounts=`/Drive; unpicklable returns are caught remotely and re-raised locally with actionable hints.

### 9.x Key decisions

- Single async engine with sync façade: `Client` is `asyncio.run`-wrapping `AsyncClient`; both expose an identical verb surface (allocate/exec/run_notebook/submit/status/logs/fetch/cancel + push/pull/drive_*). No business logic in the SDK/CLI layers.
- All public verbs accept a `backend=` selector and route through the provider abstraction; decorators (`@colab.gpu/.remote/.cpu/.tpu`) are Colab-flavored sugar over the same abstraction, so identical code runs on Modal/Vertex/Kaggle.
- Clean three-axis exception hierarchy rooted at `ColabctlError` (programmer vs auth vs runtime/quota vs execution vs transport), with the engine as the SOLE translator from transport signals (HTTP 412, XSSI prefix, DENYLISTED, socket hangup) to typed errors — transport fragility never leaks into signatures.
- RuntimeProxyInfo encodes the corrected auth recipe explicitly: proxy token is HEADER-only (X-Colab-Runtime-Proxy-Token), distinct from the OAuth Bearer identity token, plus X-Goog-Colab-Tunnel and X-Goog-Colab-Token XSRF — the SDK type prevents sending it three ways.
- `@colab.gpu` decorator ships local functions via cloudpickle (5 MiB guard), runs remotely, returns results locally; supports `.options()`, `.submit()`, `.map()`, `.aio()`, `reuse=` warm-runtime labeling, `mounts=` for large inputs, and on_reclaim retry.
- Uniform StreamEvent model across interactive (live iopub websocket) and batch (poll-then-replay) backends via Capability detection, with bounded-queue backpressure that coalesces high-frequency output (tqdm).
- RuntimeSession/AsyncRuntimeSession context managers guarantee release, arm the 60s keepalive, surface RuntimeReclaimedError, and optionally re-allocate on reclamation while making in-memory state loss explicit via a lifecycle event.
- Typer CLI mirrors the SDK verbs with `--json` (stable pydantic model_dump schema), rich tables/live-log panels that auto-degrade on non-TTY/CI, and a documented exit-code table that propagates remote job exit codes for shell/CI composition.
- Durability is explicit: push/pull move data to/from the ephemeral VM; drive_upload/download do user-OAuth plain-blob uploads to My Drive; Artifact.durable distinguishes runtime:// (transient) from drive://gcs:// (durable).
- Escape-hatch (raw /tun/m/*) is gated behind `escape_hatch=False` by default at both Client and CLI; using a Colab-only raw path without opting in raises ConfigError with the disclosed-risk explanation.

### 9.y Section risks

- cloudpickle function shipping for `@colab.gpu` is fragile across Python/library version skew between the local 3.11+ core and the remote runtime (official CLI is Python-3.13-only); by-value pickling of __main__/notebook functions and unpicklable returns (CUDA tensors) will surface as SerializationError and frustrate users despite the mounts/Drive escape hatch.
- The sync façade draining async generators (exec_stream/logs --follow) through AsyncToSyncIterator is a classic deadlock/nesting hazard — calling sync Client inside an already-running event loop, or interleaving streaming with other sync calls on the same engine loop thread, needs careful guarding or it hangs.
- Proxy-token auto-refresh on the keepalive loop racing an in-flight exec over the websocket can produce mid-stream 401/expired states; the transparent-refresh design must be exercised hard or users hit ProxyTokenExpiredError unpredictably during long cells.
- Capability-based streaming unification hides real semantic gaps: a caller writing against LIVE_LOGS on Colab and then running on Kaggle gets multi-minute blind latency with logs only replayed post-run (and Kaggle's log download is itself buggy/empty), so 'one code path' can mask sharply different debugging UX.
- The stable `--json` schema is tied to pydantic model_dump output; evolving the models (new RuntimeState/JobState/Capability enum values, new fields) risks breaking agent/CI consumers unless enum additions and field additivity are treated as a versioned contract from day one.
- DenylistedError handling is non-retryable by design, but the opaque, no-appeal nature of Colab abuse blocks means the SDK can do little beyond surfacing failover; sustained `@colab.gpu`/`.map()` fan-out is exactly the high-volume pattern that trips abuse heuristics, so the most ergonomic features carry the highest ban exposure.
- Exit-code mapping that offsets remote job exit codes into the 20-range can collide with or obscure the real process exit code of the user's notebook/script, confusing CI pipelines that key off specific codes.

---

## 10. MCP Server for AI Agents

This section specifies `colabctl`'s Model Context Protocol (MCP) server: the surface through which AI agents (Claude Code, Codex, Gemini CLI, Windsurf, any MCP-compatible client) drive Colab and the other sanctioned backends. The server is a **thin presentation layer over the SDK provider abstraction** (architecture layer 6). It owns *zero* transport, auth, or lifecycle logic — every tool call resolves to a method on `colabctl.sdk.ColabctlClient`, which in turn dispatches to a `Provider` implementation. The MCP server's only added responsibilities are: schema translation (pydantic ⇄ MCP tool I/O), agent-appropriate streaming, safety gating, and idempotency.

> **Non-negotiable design rule:** if a tool handler contains business logic that is not a one-line delegation to the SDK plus input/output marshalling, it is a bug. The CLI (`colabctl.cli`) and the MCP server (`colabctl.mcp`) are siblings that both consume `ColabctlClient`; they must never diverge in behavior.

### Module Layout

```
colabctl/
  sdk/
    client.py            # ColabctlClient — the single entrypoint both CLI and MCP use
    models.py            # shared pydantic v2 models (RuntimeHandle, ExecResult, ...)
    providers/
      base.py            # Provider ABC + Capability flags
      colab_cli.py       # CORE: wraps official google-colab-cli (subprocess/uv env)
      colab_bridge.py    # SECONDARY: colab-mcp browser bridge (human-in-the-loop)
      colab_tun.py       # OPT-IN escape hatch: direct /tun/m/* + jupyter-kernel-client
      modal.py vertex.py kaggle.py hf_jobs.py ...
    exceptions.py        # ColabctlError hierarchy (maps cleanly to MCP errors)
  mcp/
    server.py            # FastMCP app construction + lifespan + tool registration
    tools.py             # one async handler per tool; pure delegation + marshalling
    schemas.py           # MCP-facing pydantic I/O models (subset/reshape of sdk.models)
    streaming.py         # log/exec streaming bridge → MCP progress + resources
    safety.py            # SafetyGate: limits, confirmation, allowlists
    idempotency.py       # IdempotencyStore (request-key dedup for non-idempotent verbs)
    context.py           # ServerContext dataclass injected into every handler
    errors.py            # ColabctlError → mcp.McpError/ToolError translation
    __main__.py          # `python -m colabctl.mcp` / console_script entrypoint
  config.py              # ColabctlConfig (pydantic-settings); env + TOML
```

The transport (`stdio` by default, `streamable-http` optional) is FastMCP-native; we do not hand-roll a websocket relay (the official colab-mcp browser bridge is itself reached *through* the SDK's `colab_bridge` provider, not re-implemented here).

### Server Construction & Lifespan

We use **FastMCP** (the `mcp` Python SDK's high-level API). The server is constructed once; the SDK client and its provider pool live for the process lifetime via the lifespan context manager so we are not re-authenticating per tool call.

```python
# colabctl/mcp/server.py
from contextlib import asynccontextmanager
from dataclasses import dataclass
from mcp.server.fastmcp import FastMCP

from colabctl.sdk.client import ColabctlClient
from colabctl.config import ColabctlConfig
from colabctl.mcp.safety import SafetyGate
from colabctl.mcp.idempotency import IdempotencyStore
from colabctl.mcp.streaming import StreamRegistry

@dataclass
class ServerContext:
    client: ColabctlClient        # the SDK — single source of truth
    config: ColabctlConfig
    safety: SafetyGate
    idem: IdempotencyStore
    streams: StreamRegistry       # tracks in-flight long-running execs/logs

@asynccontextmanager
async def lifespan(server: FastMCP):
    config = ColabctlConfig.load()
    client = await ColabctlClient.create(config)   # opens keyring, builds provider pool
    ctx = ServerContext(
        client=client,
        config=config,
        safety=SafetyGate(config.safety),
        idem=IdempotencyStore(ttl_seconds=config.mcp.idempotency_ttl_s),
        streams=StreamRegistry(),
    )
    try:
        yield ctx                  # FastMCP exposes this via ctx.request_context.lifespan_context
    finally:
        await ctx.streams.close_all()
        await client.aclose()      # flush keep-alives, release proxy tokens politely

def build_server(config: ColabctlConfig | None = None) -> FastMCP:
    mcp = FastMCP(
        name="colabctl",
        instructions=_AGENT_INSTRUCTIONS,   # see "Agent-Facing Instructions" below
        lifespan=lifespan,
    )
    from colabctl.mcp import tools
    tools.register(mcp)            # registers every @mcp.tool()
    return mcp
```

`__main__.py` selects transport from config: `mcp.run(transport="stdio")` (default — what Claude Code/Codex launch over) or `mcp.run(transport="streamable-http", ...)` for a long-lived shared server. **stdio is the only mode that must be flawless**; HTTP is for multi-client/hosted deployments and reuses the identical tool set.

### The Tool Set

All tools are `async def`. Inputs and outputs are pydantic v2 models declared in `colabctl/mcp/schemas.py`; FastMCP derives the JSON Schema the agent sees from the type hints. Every output model carries the discriminant fields an agent needs to decide its next action without a follow-up call (status, ids, truncation flags, and a `next_actions` hint list).

| Tool | Purpose | Idempotent | Safety-gated |
|------|---------|-----------|--------------|
| `list_backends` | Enumerate providers + capability matrix | yes | no |
| `allocate_runtime` | Acquire a GPU/TPU runtime on a backend | no (dedup via key) | yes (cost/quota) |
| `list_runtimes` | List active runtimes the user owns | yes | no |
| `get_runtime_status` | Status + remaining lease for one runtime | yes | no |
| `run_code` | Execute a code snippet on a runtime | no (dedup via key) | yes (destructive-code heuristics) |
| `run_notebook` | Execute a full `.ipynb` (papermill/nbclient adapter) | no (dedup via key) | yes |
| `get_execution` | Poll a previously-submitted async execution | yes | no |
| `stream_logs` | Stream stdout/stderr/log for a runtime or execution | yes | no |
| `cancel_execution` | Interrupt a running cell/notebook | yes | no |
| `upload_file` | Put a file/blob into the runtime or Drive | no (dedup via key) | yes (path/size) |
| `fetch_artifact` | Retrieve a file/output from runtime or Drive | yes | no |
| `list_files` | List files in runtime CWD or a Drive folder | yes | no |
| `stop_runtime` | Release/teardown a runtime | yes (no-op if gone) | confirm if `force` |
| `keepalive_runtime` | Extend lease / reset idle timer | yes | no |

> **Why these and not more:** the verb set mirrors the provider abstraction's `submit/status/logs/fetch/cancel` plus the notebook/file ops the SDK already exposes. We deliberately do **not** expose raw transport verbs (no `assign`, no `runtime-proxy-token`, no `/tun/m/*`); those are SDK-internal and live behind the opt-in escape-hatch provider. Agents must not be able to reach the fragile, ban-exposed surface directly.

#### Shared schema primitives

```python
# colabctl/mcp/schemas.py
from enum import StrEnum
from typing import Annotated, Literal
from pydantic import BaseModel, Field

class Accelerator(StrEnum):
    NONE = "none"; T4 = "t4"; L4 = "l4"; A100 = "a100"; H100 = "h100"; TPU = "tpu"

class RuntimeState(StrEnum):
    ALLOCATING = "allocating"; READY = "ready"; BUSY = "busy"
    IDLE = "idle"; EXPIRING = "expiring"; TERMINATED = "terminated"; ERROR = "error"

class ExecState(StrEnum):
    QUEUED = "queued"; RUNNING = "running"; SUCCEEDED = "succeeded"
    FAILED = "failed"; CANCELLED = "cancelled"; TIMED_OUT = "timed_out"

class RuntimeRef(BaseModel):
    runtime_id: str = Field(description="Opaque handle from allocate_runtime/list_runtimes.")
    backend: str    = Field(description="e.g. 'colab', 'modal', 'vertex'.")

class RuntimeInfo(RuntimeRef):
    state: RuntimeState
    accelerator: Accelerator
    accelerator_count: int = 1
    lease_expires_in_s: int | None = Field(
        default=None, description="Seconds until forced reclamation; None if unknown/unbounded.")
    idle_timeout_s: int | None = None
    region: str | None = None
    note: str | None = Field(default=None, description="Human-readable status, e.g. quota downgrade.")

class OutputChunk(BaseModel):
    stream: Literal["stdout", "stderr", "result", "display", "error"]
    text: str
    truncated: bool = False

class ExecResult(BaseModel):
    execution_id: str
    runtime_id: str
    state: ExecState
    outputs: list[OutputChunk] = Field(default_factory=list)
    result_repr: str | None = Field(default=None, description="Text repr of last expression value.")
    error: "ExecError | None" = None
    duration_ms: int | None = None
    output_truncated: bool = Field(
        default=False, description="True if total output exceeded byte cap; use fetch_artifact/stream_logs for full.")
    log_resource_uri: str | None = Field(
        default=None, description="MCP resource URI to read the complete, untruncated log.")
    next_actions: list[str] = Field(
        default_factory=list,
        description="Suggested follow-ups, e.g. ['fetch_artifact', 'cancel_execution'].")

class ExecError(BaseModel):
    ename: str           # e.g. "RuntimeError"
    evalue: str
    traceback_tail: str  # last N lines, full available via log_resource_uri
```

#### Representative tool signatures (full)

```python
# colabctl/mcp/tools.py  — every handler is pure delegation
from mcp.server.fastmcp import Context
from colabctl.mcp import schemas as S
from colabctl.mcp.errors import translate
from colabctl.mcp.context import server_ctx   # pulls ServerContext off the request

def register(mcp):

    @mcp.tool(
        title="Allocate a Colab/GPU runtime",
        annotations={"destructiveHint": False, "openWorldHint": True},
    )
    async def allocate_runtime(
        ctx: Context,
        backend: str = "colab",
        accelerator: S.Accelerator = S.Accelerator.T4,
        accelerator_count: int = 1,
        high_ram: bool = False,
        region: str | None = None,
        idle_timeout_s: int | None = None,
        request_key: str | None = None,   # idempotency token (agent-supplied)
    ) -> S.RuntimeInfo:
        """Acquire a runtime. May incur cost/compute-unit spend. Subject to safety gate
        (see SafetyGate). On Colab this can raise TooManyAssignments / quota-denied;
        those surface as structured McpErrors, not crashes."""
        sc = server_ctx(ctx)
        async with translate():                       # ColabctlError → McpError
            return await sc.idem.run(request_key, lambda: _allocate(sc, ctx, locals()))

    @mcp.tool(annotations={"destructiveHint": True, "openWorldHint": True})
    async def run_code(
        ctx: Context,
        runtime_id: str,
        code: str,
        timeout_s: int = 300,
        stream: bool = True,
        max_output_bytes: int = 256_000,
        request_key: str | None = None,
    ) -> S.ExecResult:
        """Execute Python on the runtime's kernel. If stream=True, partial output is
        reported via ctx.report_progress + log resource; the return value is the final
        ExecResult. Long-running execs (> timeout) return state=RUNNING with an
        execution_id to poll via get_execution."""
        sc = server_ctx(ctx)
        await sc.safety.check_run_code(code, runtime_id)   # may raise ConfirmationRequired
        async with translate():
            return await sc.idem.run(request_key,
                lambda: _run_code(sc, ctx, runtime_id, code, timeout_s, stream, max_output_bytes))

    @mcp.tool()
    async def run_notebook(
        ctx: Context,
        runtime_id: str,
        notebook_path: str | None = None,     # path on local fs (uploaded first) OR
        notebook_drive_id: str | None = None, # an existing Drive .ipynb
        parameters: dict[str, object] | None = None,  # papermill-style injection
        timeout_per_cell_s: int = 600,
        output_drive_folder: str | None = None,
        request_key: str | None = None,
    ) -> S.ExecResult: ...

    @mcp.tool()
    async def get_execution(ctx: Context, execution_id: str,
                            include_outputs: bool = True) -> S.ExecResult: ...

    @mcp.tool()
    async def stream_logs(ctx: Context, runtime_id: str | None = None,
                          execution_id: str | None = None,
                          since_seq: int = 0, max_chunks: int = 200) -> list[S.OutputChunk]:
        """Capability-aware: live tail where the backend supports it, poll-then-return
        otherwise (e.g. Kaggle). Use since_seq for incremental pulls."""

    @mcp.tool()
    async def upload_file(ctx: Context, runtime_id: str | None,
                          local_path: str | None = None,
                          content_b64: str | None = None,
                          dest_path: str = "/content/",
                          to_drive: bool = False,
                          drive_folder: str | None = None,
                          request_key: str | None = None) -> S.FileRef: ...

    @mcp.tool()
    async def fetch_artifact(ctx: Context, runtime_id: str | None,
                             remote_path: str | None = None,
                             drive_id: str | None = None,
                             max_inline_bytes: int = 1_000_000) -> S.ArtifactPayload:
        """Returns content inline (base64) if under max_inline_bytes, else returns an
        MCP resource URI the agent can read separately."""

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
    async def stop_runtime(ctx: Context, runtime_id: str, force: bool = False) -> S.RuntimeInfo: ...

    @mcp.tool(annotations={"idempotentHint": True})
    async def list_runtimes(ctx: Context, backend: str | None = None) -> list[S.RuntimeInfo]: ...

    @mcp.tool(annotations={"idempotentHint": True})
    async def get_runtime_status(ctx: Context, runtime_id: str) -> S.RuntimeInfo: ...

    @mcp.tool(annotations={"idempotentHint": True})
    async def list_backends(ctx: Context) -> list[S.BackendDescriptor]:
        """Returns the capability matrix so the agent can choose a backend that supports
        what it needs (live logs vs poll, interactive vs batch, gpu types, cost class)."""
```

`_allocate`, `_run_code`, etc. are private helpers in `tools.py` that contain only marshalling — they call `sc.client.allocate(...)`, `sc.client.run_code(...)`, map the SDK result model to the MCP schema, and populate `next_actions`. They contain no transport code.

### Capability Discovery (`list_backends`)

The agent must not guess what a backend can do. `list_backends` projects the SDK's `Capability` flags into a descriptor so the model can route intelligently (e.g. "I need live logs → not Kaggle"; "untrusted generated code → prefer Modal sandbox").

```python
# colabctl/mcp/schemas.py
class BackendDescriptor(BaseModel):
    backend: str
    available: bool                      # creds present & probe passed
    interactive: bool                    # supports run_code on a live kernel
    batch: bool                          # supports run_notebook fire-and-forget
    live_logs: bool                      # stream_logs tails in real time
    accelerators: list[Accelerator]
    headless: bool                       # False for colab_bridge (needs open browser tab)
    cost_class: Literal["subscription", "per_second", "free_quota"]
    tos_risk: Literal["low", "medium", "high"]
    notes: list[str]                     # e.g. "Colab: opaque abuse-ban risk on sustained GPU use"
```

The descriptor is built directly from `Provider.capabilities()` and `Provider.probe()` in the SDK — the MCP layer never hard-codes a backend's traits.

### Streaming & Long-Running Execution

GPU work is long-running and Colab kernels stream output incrementally. MCP gives us two complementary mechanisms; we use **both**:

1. **`ctx.report_progress(...)` for liveness** — coarse progress (cell N of M, elapsed seconds, "still running") so the client UI doesn't appear hung. This is *not* the output channel; it carries no payload bytes beyond a short message.
2. **MCP Resources for the actual log/output** — each streaming execution registers a resource `colab://exec/{execution_id}/log` that the client can read (and re-read incrementally) for the full, untruncated stream. The final `ExecResult.log_resource_uri` points at it.

The decision algorithm in `run_code`:

```
run_code(timeout_s, stream):
  exec = sc.client.submit_code(runtime, code)        # SDK returns an async-iterable handle
  register resource colab://exec/{exec.id}/log
  buf = RingByteBuffer(cap=max_output_bytes)
  deadline = now + timeout_s
  async for chunk in exec.stream():                  # SDK normalizes kernel msgs → OutputChunk
      buf.append(chunk); sc.streams.publish(exec.id, chunk)
      if stream: await ctx.report_progress(progress=exec.elapsed_s, message=chunk.head())
      if now > deadline:
          # DO NOT kill — Colab cells often legitimately exceed the agent's patience.
          return ExecResult(state=RUNNING, execution_id=exec.id,
                            outputs=buf.tail(), output_truncated=buf.overflowed,
                            log_resource_uri=uri,
                            next_actions=["get_execution", "stream_logs", "cancel_execution"])
  return ExecResult(state=exec.terminal_state, outputs=buf.snapshot(), ...)
```

Key rules:
- **Timeout ≠ kill.** Hitting the agent-supplied `timeout_s` returns `state=RUNNING` with an `execution_id`; the kernel keeps running. The agent polls `get_execution` or tails `stream_logs`. We never silently interrupt a GPU job the agent might still want — interruption is an explicit `cancel_execution`.
- **Output is byte-capped** (`max_output_bytes`, default 256 KB) with `output_truncated=True` and a `log_resource_uri` to the full stream. This prevents a `print` loop from blowing the agent's context window. The cap is enforced by a ring buffer that keeps head + tail.
- **Capability fallback:** for poll-only backends (Kaggle, Vertex batch), `stream=True` degrades gracefully — `report_progress` fires on each poll interval and `OutputChunk`s arrive only after completion. The agent learns this from `list_backends().live_logs`.

`StreamRegistry` (`streaming.py`) holds a per-execution `asyncio` pub/sub so concurrent `stream_logs` reads, the resource reader, and the originating `run_code` all observe one buffered stream without re-querying the backend.

### Safety, Confirmations & Limits

The `SafetyGate` (`colabctl/mcp/safety.py`) is the single chokepoint for anything that costs money, allocates compute, or mutates state. It is configured declaratively (`config.safety`) and is the layer that surfaces the architecture's explicit **abuse-detection / cost** risks to the human-in-the-loop instead of hiding them.

```python
# colabctl/mcp/safety.py
class SafetyConfig(BaseModel):
    require_confirm_allocate: bool = True       # ask before spending CU / per-second $
    require_confirm_force_stop: bool = True
    max_concurrent_runtimes: int = 2
    max_runtime_lease_s: int = 6 * 3600
    monthly_cost_ceiling_usd: float | None = 50.0
    blocked_code_patterns: list[str] = [         # heuristic, advisory
        r"rm\s+-rf\s+/", r"os\.system\(.*rm", r":\(\)\{.*\};:",  # fork bomb
    ]
    drive_write_allowlist: list[str] = ["/colabctl-artifacts"]   # Drive folders writable
    upload_max_bytes: int = 200_000_000

class ConfirmationRequired(ColabctlError): ...   # → MCP elicitation, not a hard failure
```

How confirmation works over MCP:
- For clients that support **elicitation** (the MCP `elicitation` capability), `SafetyGate` raises through to `ctx.elicit(...)`, prompting the human ("Allocate an A100 on Colab? Est. cost ~X compute units/hr. ToS note: sustained headless GPU use carries opaque ban risk.") and proceeds only on accept.
- For clients **without** elicitation, the tool returns a structured `ConfirmationRequired` result (not an error) carrying a one-time `confirmation_token`; the agent re-invokes the same tool with `confirm_token=...` to proceed. This keeps the gate honest even on minimal clients.

Enforced limits (all return structured, recoverable errors):
- **Concurrency:** refuse `allocate_runtime` past `max_concurrent_runtimes`; tell the agent to `stop_runtime` first. Mirrors Colab's `TooManyAssignmentsError` at our layer so the agent learns the constraint before hitting the backend.
- **Cost ceiling:** the SDK tracks per-backend spend/compute-units; `allocate_runtime` and `run_code` consult it. Over ceiling → `QuotaExceeded` with the current spend in the payload.
- **Lease cap:** clamp `idle_timeout_s`/lease requests to `max_runtime_lease_s`.
- **Code heuristics:** `blocked_code_patterns` is advisory destructive-command screening (regex). It is *not* a sandbox — the spec explicitly notes that for genuinely untrusted agent-generated code the correct backend is **Modal Sandbox (gVisor)**, which the agent reaches via `backend="modal"`. We document this in the tool description so the model routes risky code correctly.
- **Drive writes:** `upload_file(to_drive=True)` is restricted to `drive_write_allowlist` folders (default a dedicated `/colabctl-artifacts` folder), preventing an agent from scribbling across the human's entire My Drive. Uploads are user-OAuth plain-blob `.ipynb`/file PUTs to My Drive (per the Drive-sync decision); a service account is never used.

### Idempotency

`run_code`, `run_notebook`, `allocate_runtime`, and `upload_file` are not naturally idempotent, and MCP clients retry. Each accepts an optional `request_key`. `IdempotencyStore` (`idempotency.py`) caches `(request_key) → result` for `idempotency_ttl_s` (default 900s). A retried call with the same key returns the cached result (or, if still in flight, awaits the original future) instead of allocating a second A100 or running a training cell twice. Keys are namespaced per session/connection. Read-only tools are inherently idempotent and skip the store.

### Error Mapping

`colabctl/mcp/errors.py` translates the SDK's `ColabctlError` hierarchy into MCP errors with stable machine-readable `code` fields and actionable messages, so the agent can branch instead of giving up:

| SDK exception | MCP surfacing | Agent guidance baked into message |
|---------------|---------------|-----------------------------------|
| `AuthExpiredError` | `McpError(code="auth_expired")` | "Run `colabctl auth login` locally; refresh token died (Testing-status 7-day limit)." |
| `TooManyAssignmentsError` | `McpError(code="too_many_runtimes")` | "Stop an existing runtime (list_runtimes) before allocating." |
| `QuotaExceeded` (compute units / $ ceiling) | structured result, not exception | includes remaining balance / ceiling |
| `RuntimeReclaimedError` | `McpError(code="runtime_gone")` | "Runtime was reclaimed (idle/24h cap). State is lost; re-allocate and restore from Drive." |
| `AbuseBlockedError` | `McpError(code="account_blocked")` | "Account flagged for suspected abusive activity. No automated retry; surface to human. Consider routing to `backend='modal'`/`'vertex'`." |
| `BridgeNotConnectedError` (colab_bridge) | `McpError(code="bridge_needs_browser")` | "Open the Colab tab the bridge printed, or switch to backend='colab' (CLI) / a headless backend." |
| `ConfirmationRequired` | elicitation or `confirmation_token` result | re-invoke with confirm token |
| `CapabilityUnsupported` | `McpError(code="unsupported")` | "Backend X has no live logs; poll get_execution / pick another backend." |

Critically, **`AbuseBlockedError` and `RuntimeReclaimedError` never trigger silent automatic retries** — they are exactly the conditions the architecture flags as opaque and dangerous; the agent (and through it the human) must decide whether to re-allocate or route to a sanctioned non-Colab backend.

### Sequence: Agent runs a training job on Colab end-to-end

```
agent → list_backends()                 # picks "colab" (interactive, T4/A100), notes ban-risk
agent → allocate_runtime(backend="colab", accelerator="a100", request_key="r1")
   SafetyGate → elicit human confirm (cost + ToS note) → accept
   SDK(colab_cli) → official CLI spins runtime → RuntimeInfo{state=ready, lease 24h}
agent → upload_file(runtime_id, local_path="train.py", dest_path="/content/")
agent → run_code(runtime_id, "open('/content/train.py').read()", timeout_s=10)   # sanity
agent → run_code(runtime_id, "%run /content/train.py", timeout_s=300, stream=True)
   → progress events stream; at 300s job still running
   ← ExecResult{state=RUNNING, execution_id="e9", log_resource_uri="colab://exec/e9/log"}
agent → (periodically) get_execution("e9")  OR  reads the log resource incrementally
   ← eventually ExecResult{state=succeeded, ...}
agent → run_code(runtime_id, "model.save('/content/out.pt')")
agent → fetch_artifact(runtime_id, remote_path="/content/out.pt")  # >1MB → resource URI
   (or) upload_file(to_drive=True, drive_folder="/colabctl-artifacts")  # durable
agent → stop_runtime(runtime_id)         # release; avoid idle compute-unit burn
```

### Configuration

MCP-specific knobs live under `[mcp]` and `[safety]` in `colabctl.toml` (and env via `COLABCTL_*`), parsed by `ColabctlConfig` (pydantic-settings):

```toml
[mcp]
transport = "stdio"            # or "streamable-http"
default_backend = "colab"
idempotency_ttl_s = 900
max_output_bytes = 256000
expose_escape_hatch = false    # gate the opt-in /tun direct backend behind explicit config

[safety]
require_confirm_allocate = true
max_concurrent_runtimes = 2
monthly_cost_ceiling_usd = 50.0
drive_write_allowlist = ["/colabctl-artifacts"]
```

`expose_escape_hatch=false` by default means the agent cannot even *select* the fragile direct-`/tun/m/*` provider through MCP unless the human has explicitly opted in — the disclosed-risk path stays disclosed.

### Agent-Facing Instructions (server `instructions` + tool descriptions)

The FastMCP `instructions` string and per-tool docstrings are part of the contract; they teach the model the operating model it cannot infer:

- Runtimes are **ephemeral** — always externalize durable state via `upload_file(to_drive=True)` / `fetch_artifact`; expect `runtime_gone`.
- Always `stop_runtime` when done to avoid compute-unit/$ burn; idle runtimes cost money.
- Prefer `backend="modal"` for untrusted/generated code (sandboxed); `backend="vertex"` for unattended batch; `backend="colab"` for interactive Pro GPU work.
- Long jobs return `state=RUNNING` + `execution_id`; poll, don't assume failure.
- `account_blocked` / `auth_expired` require human action — do not loop-retry.

### Edge Cases & Failure Handling Specific to the MCP Layer

- **Client disconnect mid-stream:** `StreamRegistry` keeps the execution buffer alive for `idempotency_ttl_s` after the originating call's task is cancelled, so a reconnecting agent recovers full output via `get_execution`/the log resource. The kernel job is *not* cancelled on disconnect.
- **Duplicate `allocate_runtime` from retry:** absorbed by `IdempotencyStore`; without a `request_key` we still detect a rapid identical-params duplicate within a short window and warn in `next_actions` rather than spawning a second paid runtime.
- **Oversized inputs:** `run_notebook` with a huge inline notebook, or `upload_file` `content_b64` over `upload_max_bytes`, is rejected pre-flight with a clear size error and a pointer to `local_path` upload streaming.
- **Output flooding:** ring-buffer cap + `output_truncated` flag + resource URI; the agent's context is never blown by runaway prints.
- **Backend unavailable at call time:** if creds/probe fail for the requested backend, tools return `unsupported`/`auth_expired` with the capability matrix attached so the agent can re-route, rather than hanging.
- **Bridge backend selected headlessly:** `colab_bridge` requires an open logged-in browser tab; `bridge_needs_browser` is returned immediately with the tab URL and a suggestion to use `backend="colab"` (CLI) for headless.
- **Concurrent `run_code` on one busy kernel:** serialized by the SDK per-runtime execution lock; the second call gets `state=QUEUED` with the execution_id ahead of it, never a corrupted interleave.
- **Graceful shutdown:** lifespan teardown flushes keep-alives and releases proxy tokens/assignments politely so we don't leave orphaned (billed/leased) runtimes when the agent's session ends.

### 10.x Key decisions

- MCP server is a strictly thin layer over the SDK ColabctlClient: every tool handler is pure delegation + pydantic marshalling, guaranteeing CLI and MCP never diverge and no transport/auth logic is duplicated.
- Built on FastMCP with the SDK client + provider pool held for process lifetime via a lifespan-injected ServerContext; stdio is the primary, must-be-flawless transport, streamable-http is an optional hosted mode reusing the identical tool set.
- Tool set deliberately mirrors the provider abstraction verbs (allocate/run/status/logs/fetch/cancel + notebook/file ops) and explicitly does NOT expose raw transport endpoints (no /tun/m/*, no proxy-token), keeping the fragile, ban-exposed surface unreachable by agents unless the human opts in via expose_escape_hatch=false default.
- Long-running execution model: agent-supplied timeout_s never kills the kernel — on timeout the tool returns state=RUNNING + execution_id, and the agent polls get_execution or tails stream_logs; interruption is only via explicit cancel_execution.
- Dual streaming mechanism: ctx.report_progress for liveness (no payload) plus MCP Resources (colab://exec/{id}/log) for the full untruncated output, with a ring-buffer byte cap (default 256KB) + output_truncated flag so agent context windows are never blown.
- SafetyGate is the single chokepoint for cost/compute allocation and state mutation: confirmation via MCP elicitation (or a confirmation_token fallback for minimal clients), concurrency caps, monthly cost ceiling, lease clamps, advisory destructive-code regex, and Drive write allowlist — surfacing the abuse-ban/cost risks to the human rather than hiding them.
- Idempotency via optional agent-supplied request_key cached per session (default 900s TTL) on the non-idempotent verbs (allocate/run_code/run_notebook/upload_file) so client retries never spawn a second paid A100 or double-run a training cell.
- Structured ColabctlError → McpError mapping with stable codes; account_blocked (abuse) and runtime_gone (reclamation) never auto-retry and are escalated to the human, and errors carry routing guidance toward sanctioned backends (modal/vertex).
- Capability discovery via list_backends projects SDK Provider.capabilities()/probe() so the agent routes correctly (live-logs vs poll, interactive vs batch, headless vs browser-bound, cost class, ToS risk) instead of guessing.
- Durable state is always externalized to the user's My Drive via upload_file(to_drive=True) restricted to an allowlisted folder using user-OAuth plain-blob uploads (never a service account), consistent with the ephemeral-runtime decision.

### 10.y Section risks

- The Colab backend's headlessness depends on the fast-moving official google-colab-cli (v0.5.x, yanked releases, Python 3.13-only, no confirmed stable JSON mode); the MCP layer is insulated by the SDK adapter, but tool reliability inherits the SDK's exposure to CLI interface churn and stdout-parsing fragility.
- Opaque Colab abuse-detection bans can hit even paid Pro accounts on sustained headless GPU use; the MCP surface makes high-volume agent-driven jobs easy, which is exactly the profile that trips heuristics — mitigated by surfacing account_blocked without auto-retry and by routing guidance to Modal/Vertex, but not eliminable.
- Confirmation safety depends on the client supporting MCP elicitation; on clients lacking it the confirmation_token fallback works but an agent could be coded to auto-supply the token, weakening human-in-the-loop intent for costly allocate_runtime calls.
- Idempotency keys are advisory and per-session in-memory; a client that omits request_key, restarts between retries, or runs across reconnects can still double-allocate paid runtimes (partially mitigated by the rapid-duplicate heuristic and concurrency cap).
- Output byte-capping plus resource-URI indirection assumes clients faithfully read MCP resources for full logs; clients that ignore resources will see truncated output and may misjudge job state, so next_actions/log_resource_uri hints must be honored.
- The colab_bridge (browser) backend is structurally non-headless; if an agent or operator misconfigures default_backend to the bridge in an unattended context, every interactive call returns bridge_needs_browser until a human opens a tab — an operational footgun the capability matrix only partially prevents.
- Per-runtime serialization of concurrent run_code prevents kernel corruption but can surprise agents expecting parallelism on one runtime; agents must allocate multiple runtimes (bounded by max_concurrent_runtimes and cost ceiling) for parallel work.
- Graceful-shutdown release of leases/proxy tokens depends on clean lifespan teardown; a hard process kill of the stdio server can leave orphaned, billed/leased runtimes that only a separate reconciliation/list_runtimes sweep will catch.

---

## 11. Reliability & Observability

This section specifies how `colabctl` survives the two dominant failure realities established in the architecture verdicts — **Google interface churn** and **opaque, no-recourse abuse-detection bans** — plus the ordinary distributed-systems failures (network flaps, expired tokens, ephemeral-runtime reclamation, GPU stockouts, billing runaway). Everything here is engineered to make the *capability-detecting provider abstraction* (the highest-rated decision, score 7) actually survivable: every backend reports health and capabilities, every fallible operation has a typed error and a recovery policy, and the abstraction routes around degraded backends rather than crashing.

Module layout for this section:

```
colabctl/
  reliability/
    __init__.py
    retry.py            # backoff policies, @retryable, RetryBudget
    idempotency.py      # idempotency keys, ExecutionLedger, dedup
    ratelimit.py        # token-bucket limiter, per-(backend,account) buckets
    health.py           # HealthCheck protocol, RuntimeHealthMonitor
    quota.py            # GPU detection, ComputeUnit / cost tracking, budgets
    degrade.py          # FallbackRouter, circuit breaker, backend scoring
  observability/
    __init__.py
    logging.py          # structlog config, redaction processors
    tracing.py          # optional OpenTelemetry span helpers
    metrics.py          # in-process counters/histograms + OTel meters
    events.py           # structured event model (pydantic) emitted to logs/MCP
  errors.py             # the complete exception taxonomy (single source of truth)
```

All async; `asyncio` + `httpx.AsyncClient` + `websockets`. All data models are pydantic v2. Logging is `structlog`; tracing/metrics are OpenTelemetry behind a feature flag (`reliability.tracing.enabled`).

---

### 1. Error taxonomy (`colabctl/errors.py`)

The taxonomy is the **single source of truth** that the retry, idempotency, rate-limit, health, and fallback layers all key off. Every exception carries a `RecoveryPolicy` so callers and the `FallbackRouter` never have to pattern-match on message strings.

```python
from __future__ import annotations
import enum
from dataclasses import dataclass

class RecoveryAction(enum.Enum):
    RETRY_SAME          = "retry_same"          # transient; retry same backend w/ backoff
    RETRY_AFTER         = "retry_after"         # honor server Retry-After, then retry same
    REAUTH_THEN_RETRY   = "reauth_then_retry"   # refresh/re-mint creds, then retry
    REASSIGN_RUNTIME    = "reassign_runtime"    # runtime gone; allocate a fresh one, replay
    FALLBACK_BACKEND    = "fallback_backend"    # this backend unusable; route to next
    USER_INTERVENTION   = "user_intervention"   # human must act (consent, captcha, payment)
    ABORT               = "abort"               # non-recoverable; surface to caller

@dataclass(frozen=True)
class RecoveryPolicy:
    action: RecoveryAction
    retryable: bool
    max_attempts: int            # per-operation cap for RETRY_* actions
    respects_retry_after: bool   # read Retry-After / tokenExpiresInSeconds hints
    counts_against_circuit: bool # whether this failure trips the backend circuit breaker
    user_message: str            # safe, actionable text (no secrets)

class ColabctlError(Exception):
    """Root of all colabctl errors. Never raised directly."""
    policy: RecoveryPolicy = RecoveryPolicy(
        RecoveryAction.ABORT, retryable=False, max_attempts=0,
        respects_retry_after=False, counts_against_circuit=True,
        user_message="An unexpected error occurred.",
    )
    def __init__(self, message: str, *, cause: Exception | None = None,
                 backend: str | None = None, context: dict | None = None):
        super().__init__(message)
        self.cause = cause
        self.backend = backend
        self.context = context or {}     # MUST be pre-redacted by caller

    @property
    def fingerprint(self) -> str:
        """Stable id for dedup/metrics: type + backend + normalized cause."""
        return f"{type(self).__name__}:{self.backend or '-'}"
```

#### 1.1 Exception classes mapped to cause and recovery

| Exception | Trigger / Cause (from verdicts) | `RecoveryAction` | retryable | max_attempts | Counts vs circuit |
|---|---|---|---|---|---|
| `TransportError` | TCP reset, DNS, TLS handshake, connect timeout (httpx) | `RETRY_SAME` | yes | 5 | yes |
| `TransientHTTPError` | HTTP 502/503/504 from `/tun/m/*` or sanctioned CLI subprocess | `RETRY_SAME` | yes | 5 | yes |
| `RateLimitedError` | HTTP 429; CLI "60/min" ceiling; Vertex 60 jobs/min | `RETRY_AFTER` | yes | 8 | no |
| `WebSocketDroppedError` | Jupyter kernel WS hangup / `socket hangup on assign` (colab-vscode #604) | `RETRY_SAME` | yes | 4 | yes |
| `AuthExpiredError` | OAuth access token expired; runtime-proxy `tokenExpiresInSeconds` lapsed | `REAUTH_THEN_RETRY` | yes | 2 | no |
| `RefreshTokenDeadError` | 7-day Testing-status refresh-token revocation; 6-month idle; password change | `USER_INTERVENTION` | no | 0 | no |
| `ConsentRequiredError` | First-run loopback OAuth; "unverified app" interstitial | `USER_INTERVENTION` | no | 0 | no |
| `RuntimeGoneError` | Idle (~90 min) / 12h–24h max-lifetime reclamation; VM preempted | `REASSIGN_RUNTIME` | yes | 2 | no |
| `RuntimeProxyTokenError` | Proxy-token refresh via `/tun/m/runtime-proxy-token` failed/revoked | `REAUTH_THEN_RETRY` | yes | 2 | yes |
| `TooManyAssignmentsError` | HTTP 412 per-account concurrent-assignment cap (real in colab-vscode) | `RETRY_AFTER` | yes | 6 | no |
| `QuotaExhaustedError` | `QUOTA_*` outcome; Kaggle 30h/wk; GPU_ALL_REGIONS cap; CCU balance ≤ 0 | `FALLBACK_BACKEND` | no | 0 | yes |
| `AcceleratorUnavailableError` | "No available zone for accelerator"; silent T4/L4/A100 downgrade/stockout | `FALLBACK_BACKEND` | no | 0 | no |
| `BudgetExceededError` | colabctl-enforced spend/compute-unit ceiling hit (Modal/HF/RunPod per-sec billing) | `ABORT` | no | 0 | no |
| `AbuseBlockedError` | "blocked due to suspected abusive activity" (colabtools #4979/#4986); `DENYLISTED` | `FALLBACK_BACKEND` | no | 0 | yes |
| `SchemaContractError` | Undocumented `/tun/m/*` field/enum/header drift; XSSI prefix change; unparseable CLI stdout | `FALLBACK_BACKEND` | no | 0 | yes |
| `BackendUnavailableError` | Capability probe fails; CLI version mismatch/yanked; MCP browser tab not connected | `FALLBACK_BACKEND` | no | 0 | yes |
| `ExecutionError` | User cell raised; non-zero exit; kernel reported error status | `ABORT` | no | 0 | no |
| `CellTimeoutError` | Per-cell wall-clock exceeded (long training cell) | `USER_INTERVENTION` | no | 0 | no |
| `FileSyncError` | Drive plain-blob upload failed (NOT 403 SA case — see below) | `RETRY_SAME` | yes | 4 | no |
| `DriveOwnershipError` | 403 `storageQuotaExceeded` (service-account-owns-native-file footgun) | `ABORT` | no | 0 | no |
| `CancelledError` | User/agent issued `cancel()`; cooperative abort | `ABORT` | no | 0 | no |

Design rules enforced in code review:

- **Never catch `httpx`/`websockets`/SDK exceptions above the adapter boundary.** Each backend adapter (`ColabCliAdapter`, `TunProxyAdapter`, `ModalAdapter`, `VertexAdapter`, …) translates raw exceptions into exactly one taxonomy class in its `_translate_error()` method. This keeps drift contained to one file per backend.
- `ExecutionError` (user code failed) is sharply distinguished from infrastructure errors: a user's `ZeroDivisionError` is `ABORT` (do **not** retry, do **not** fall back — the next backend will fail identically), whereas a dropped websocket is `RETRY_SAME`.
- `DriveOwnershipError` is **non-recoverable by retry** because it is a design mistake (service account writing a Google-native MIME), surfaced loudly so the implementer uses the user-OAuth plain-blob path the verdict mandates.

---

### 2. Retry & backoff (`colabctl/reliability/retry.py`)

#### 2.1 Policy

Full-jitter exponential backoff (AWS-style), driven entirely by the `RecoveryPolicy` on the raised exception. No retry decision is ever made from a string match.

```python
import asyncio, random, time
from collections.abc import Awaitable, Callable
from typing import TypeVar
from pydantic import BaseModel, Field
from colabctl.errors import ColabctlError, RecoveryAction

T = TypeVar("T")

class BackoffConfig(BaseModel):
    base_seconds: float = 0.5
    max_seconds: float = 60.0
    multiplier: float = 2.0
    jitter: str = Field("full", pattern="^(full|equal|none)$")
    overall_deadline_seconds: float = 900.0   # hard wall across all attempts

def compute_delay(attempt: int, cfg: BackoffConfig, retry_after: float | None) -> float:
    if retry_after is not None:               # server hint always wins
        return min(retry_after, cfg.max_seconds)
    raw = min(cfg.max_seconds, cfg.base_seconds * (cfg.multiplier ** (attempt - 1)))
    if cfg.jitter == "full":   return random.uniform(0, raw)
    if cfg.jitter == "equal":  return raw / 2 + random.uniform(0, raw / 2)
    return raw
```

```python
async def run_with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    cfg: BackoffConfig,
    budget: "RetryBudget",
    on_reauth: Callable[[], Awaitable[None]] | None = None,
    on_reassign: Callable[[], Awaitable[None]] | None = None,
    op_name: str = "op",
) -> T:
    started = time.monotonic()
    attempt = 0
    last: ColabctlError | None = None
    while True:
        attempt += 1
        try:
            return await op()
        except ColabctlError as e:
            last = e
            p = e.policy
            log.warning("op_failed", op=op_name, attempt=attempt,
                        error=type(e).__name__, action=p.action.value, backend=e.backend)
            if not p.retryable or attempt >= p.max_attempts:
                raise
            if time.monotonic() - started > cfg.overall_deadline_seconds:
                raise                                   # deadline beats max_attempts
            if not budget.try_consume(e.backend):       # retry-budget gate (sec 2.3)
                raise
            # Side-effect recovery BEFORE the next attempt:
            if p.action is RecoveryAction.REAUTH_THEN_RETRY and on_reauth:
                await on_reauth()
            elif p.action is RecoveryAction.REASSIGN_RUNTIME and on_reassign:
                await on_reassign()
            retry_after = e.context.get("retry_after_seconds") if p.respects_retry_after else None
            await asyncio.sleep(compute_delay(attempt, cfg, retry_after))
    # unreachable; loop either returns or raises
```

`@retryable(cfg=..., budget=...)` is a thin decorator wrapper for adapter methods.

#### 2.2 Where retry applies — and where it must NOT

| Layer / operation | Retry? | Notes |
|---|---|---|
| HTTP calls to `/tun/m/*` (assign, ccu-info, runtime-proxy-token) | yes | `RETRY_SAME`/`RETRY_AFTER`; honor XSSI strip before parse |
| Sanctioned CLI subprocess invocation | yes | Retry only on non-zero exit classified `TransientHTTPError`/`TransportError`; never on `ExecutionError` |
| Kernel WebSocket connect / send-recv | yes | `WebSocketDroppedError`; combine with reconnect (sec 4.3) |
| OAuth access-token mint / proxy-token refresh | bounded (2) | More attempts hide a dead refresh token |
| Drive plain-blob upload (user-OAuth) | yes | `FileSyncError`; idempotent via fixed Drive file name + dedup (sec 3) |
| **Runtime allocation `/assign`** | bounded (2) + reassign | Aggressive retry trips `TooManyAssignmentsError` and abuse heuristics |
| **User cell execution** | **never** | Re-running non-idempotent user code is the worst possible default |
| **Abuse/quota/budget failures** | **never** | Retrying a `DENYLISTED`/`QUOTA_*`/budget hit accelerates the ban; immediately fall back |

#### 2.3 Retry budget (anti-retry-storm, anti-abuse)

A global and per-`(backend, account)` **retry budget** caps the ratio of retries to primary requests. This is the explicit anti-abuse mitigation the verdicts demand — uncontrolled retries against Colab's opaque heuristics are exactly the "sustained programmatic GPU pattern" that triggers bans.

```python
class RetryBudget:
    """Token-bucket of retries. Refilled as a fraction of successful primary calls."""
    def __init__(self, ratio: float = 0.1, min_tokens: float = 10.0):
        self.ratio, self.min_tokens = ratio, min_tokens
        self._tokens: dict[str, float] = {}
    def record_primary(self, backend: str) -> None:
        self._tokens[backend] = min(self._tokens.get(backend, self.min_tokens) + self.ratio,
                                    self.min_tokens * 5)
    def try_consume(self, backend: str | None) -> bool:
        k = backend or "-"
        if self._tokens.get(k, self.min_tokens) >= 1:
            self._tokens[k] = self._tokens.get(k, self.min_tokens) - 1
            return True
        return False     # budget blown -> stop retrying, let fallback take over
```

When the budget is exhausted the operation stops retrying and the failure propagates to the `FallbackRouter`, converting a futile retry storm into a clean backend switch.

---

### 3. Idempotency of execution (`colabctl/reliability/idempotency.py`)

Because runtimes are **ephemeral** (idle/12h/24h reclamation) and retries + reassignments are routine, every submitted job carries a caller-stable **idempotency key**. The contract: *submitting the same logical job twice — across retries, reassignments, or process restarts — must not double-execute non-idempotent side effects, and must return the original result if it already completed.*

```python
import hashlib, json, uuid
from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field

class JobPhase(str, Enum):
    PENDING = "pending"; RUNNING = "running"; SUCCEEDED = "succeeded"
    FAILED = "failed"; CANCELLED = "cancelled"

class LedgerEntry(BaseModel):
    idempotency_key: str
    job_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    backend: str
    account_email: str
    payload_digest: str                     # sha256 of normalized request
    phase: JobPhase = JobPhase.PENDING
    runtime_endpoint: str | None = None     # current assignment, mutable on reassign
    result_ref: str | None = None           # Drive/GCS URI of durable output
    attempts: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

def make_idempotency_key(*, account_email: str, code_or_nb: str, params: dict,
                         caller_key: str | None) -> str:
    if caller_key:
        return f"user:{caller_key}"
    digest = hashlib.sha256(
        json.dumps({"e": account_email, "c": code_or_nb, "p": params},
                   sort_keys=True).encode()).hexdigest()[:24]
    return f"auto:{digest}"
```

`ExecutionLedger` is an `aiosqlite`-backed store at `~/.colabctl/state/ledger.db` (path overridable; in CI use a temp dir). It is the durable, restart-surviving record of what was attempted.

```python
class ExecutionLedger:
    async def begin(self, entry: LedgerEntry) -> tuple[LedgerEntry, bool]:
        """Insert-or-get on idempotency_key (UNIQUE). Returns (entry, is_new).
        If existing entry is terminal (SUCCEEDED), caller returns cached result_ref.
        If existing entry is RUNNING/PENDING, caller ATTACHES rather than resubmits."""
    async def mark(self, key: str, phase: JobPhase, **fields) -> None: ...
    async def bind_runtime(self, key: str, endpoint: str) -> None:
        """Update runtime_endpoint on reassignment without changing job_id."""
```

#### 3.1 Submit algorithm (idempotent)

```
submit(request):
  key   = make_idempotency_key(...)
  entry, is_new = ledger.begin(LedgerEntry(idempotency_key=key, payload_digest=digest(request)))
  if not is_new:
      if entry.payload_digest != digest(request):
          raise IdempotencyConflictError   # same key, different payload -> caller bug
      if entry.phase == SUCCEEDED:  return Result.from_ref(entry.result_ref)   # replay
      if entry.phase in {PENDING, RUNNING}:  return attach(entry)              # no double-run
      # FAILED/CANCELLED -> fall through and re-submit under same key
  # --- guarded execution ---
  for backend in router.candidates(request.requirements):
      try:
          ep = await backend.allocate(...)          ; ledger.bind_runtime(key, ep)
          ledger.mark(key, RUNNING, backend=backend.name, attempts=entry.attempts+1)
          result = await backend.execute(request, ep) # NOT retried at cell level
          ref    = await drive.upload_blob(result.notebook_ipynb)  # durable, user-OAuth
          ledger.mark(key, SUCCEEDED, result_ref=ref)
          return result
      except ColabctlError as e:
          if e.policy.action == FALLBACK_BACKEND:  continue   # try next backend
          if e.policy.action == REASSIGN_RUNTIME:  ... reassign within same backend ...
          ledger.mark(key, FAILED); raise
```

#### 3.2 In-VM idempotency guard

Generated/agent code is wrapped before execution with a **once-guard** keyed by the idempotency key, so even an at-least-once delivery from a reconnect does not double-run side-effectful cells:

```python
_CTL_GUARD = "/content/.colabctl_done_{key}"
# prepended to the user payload on the kernel:
#   import os; _done = os.path.exists("/content/.colabctl_done_<key>")
#   if not _done: <user code>; open(.../.colabctl_done_<key>,'w').close()
```

This is best-effort (the VM is ephemeral) and complements — does not replace — the ledger. Durable correctness lives in the ledger + Drive `result_ref`.

---

### 4. Runtime health checks (`colabctl/reliability/health.py`)

#### 4.1 Two-level health

- **Backend health** (is this provider usable at all right now?) — drives the circuit breaker and `FallbackRouter`.
- **Runtime health** (is *my* assigned VM/kernel alive?) — drives keep-alive, reconnect, and `REASSIGN_RUNTIME`.

```python
from enum import Enum
from pydantic import BaseModel
from typing import Protocol

class HealthState(str, Enum):
    HEALTHY = "healthy"; DEGRADED = "degraded"; UNHEALTHY = "unhealthy"; UNKNOWN = "unknown"

class HealthReport(BaseModel):
    backend: str
    state: HealthState
    latency_ms: float | None = None
    detail: str = ""
    capabilities: "CapabilityDescriptor | None" = None
    checked_at: float

class HealthCheck(Protocol):
    async def probe(self) -> HealthReport: ...
```

Per-backend probes (cheap, side-effect-free, **never** allocate a GPU just to check):

| Backend | Probe |
|---|---|
| Colab via sanctioned CLI | `colab --version` + capability probe of subcommands; classify yanked/3.13-mismatch as `BackendUnavailableError` |
| Colab `/tun/m/*` escape hatch | `GET /tun/m/ccu-info` (read-only) → CCU balance + assignment count; non-200 ⇒ `DEGRADED`/`UNHEALTHY` |
| Colab MCP bridge | Is the local WS connected and is a logged-in browser tab attached? `fe_connected` true within 60s window |
| Vertex / Colab Enterprise | `notebookRuntimeTemplates.list` (1 item, 200) |
| Modal | `modal.App.lookup` / token validity ping |
| Drive | `files.get(fileId=root, fields=id)` cheap metadata call |

#### 4.2 Runtime health monitor & keep-alive

```python
class RuntimeHealthMonitor:
    """Per-active-runtime watchdog. Owns keep-alive + liveness + idle accounting."""
    def __init__(self, endpoint: str, *, keepalive_s: float = 50.0,
                 liveness_s: float = 30.0, idle_warn_s: float = 75 * 60,
                 hard_max_s: float = 23.5 * 3600):
        ...
    async def run(self) -> None:
        # 1) keep-alive ping every keepalive_s (just under Google's ~60s window)
        # 2) liveness: send Jupyter kernel_info_request; expect kernel_info_reply
        # 3) proxy-token refresh when tokenExpiresInSeconds < 2 * keepalive_s
        # 4) approaching idle_warn_s / hard_max_s -> emit RuntimeExpiring event,
        #    trigger pre-emptive checkpoint + REASSIGN_RUNTIME
```

Keep-alive cadence is **conservative on purpose** (~50s, not a busy loop) — aggressive keep-alive is itself an abuse signal. The monitor never fakes "active programming"; it issues the same kernel pings the sanctioned client uses.

#### 4.3 WebSocket reconnect (kernel exec layer)

On `WebSocketDroppedError`: reconnect with backoff (max 4), re-send the **header-only** auth recipe correctly per the verdict — `X-Colab-Runtime-Proxy-Token` (header only, NOT Bearer, NOT query param), `X-Goog-Colab-Tunnel: true`, and the `X-Goog-Colab-Token` XSRF header; the OAuth identity goes in `Authorization: Bearer` separately. After reconnect, reconcile by polling kernel execution state for the in-flight `msg_id` before assuming re-execution is needed (preserves idempotency).

---

### 5. GPU detection, quota & cost awareness (`colabctl/reliability/quota.py`)

```python
from pydantic import BaseModel
from enum import Enum

class Accelerator(str, Enum):
    NONE="none"; T4="T4"; L4="L4"; A100="A100"; H100="H100"
    TPU_V2="v2-8"; TPU_V5E="v5e-1"; TPU_V6E="v6e-1"

class AcceleratorGrant(BaseModel):
    requested: Accelerator
    granted: Accelerator            # may be a SILENT downgrade (colabtools #2425)
    high_ram: bool = False
    was_downgraded: bool            # granted != requested
    source: str                     # "colab-cli" | "tun" | "modal" | "vertex"

class QuotaSnapshot(BaseModel):
    backend: str
    account_email: str
    compute_units_remaining: float | None = None   # Colab CCU balance
    assignments_active: int | None = None          # vs TooManyAssignments cap
    weekly_gpu_hours_used: float | None = None      # Kaggle ~30h/wk
    outcome: str | None = None                      # SUCCESS|DENYLISTED|QUOTA_*
    captured_at: float
```

**GPU detection is verify-after-grant, not trust-the-request.** After any allocation, `detect_accelerator(endpoint)` runs an in-kernel probe (`nvidia-smi --query-gpu=name --format=csv,noheader` / `torch.cuda.get_device_name(0)` / TPU device list) and reconciles against the request. If `granted < requested` (e.g. asked A100, got T4), it raises `AcceleratorUnavailableError` **only if the caller set `require_exact=True`**; otherwise it records `was_downgraded=True`, emits a warning event, and proceeds.

#### 5.1 Cost & compute-unit budget (`BudgetGuard`)

Mandatory for per-second-billed backends (Modal/HF/RunPod) and for Colab CCU burn. A runaway agent loop is, per the verdicts, the real failure mode — not breakage.

```python
class BudgetConfig(BaseModel):
    max_usd_per_job: float | None = None
    max_usd_per_session: float | None = None
    max_compute_units: float | None = None     # Colab CCU
    max_wall_seconds: float = 6 * 3600
    on_breach: str = "cancel"                   # "cancel" | "pause" | "warn"

class BudgetGuard:
    async def watch(self, job_id: str) -> None:
        # polls accrued cost (rate * elapsed for IaaS; ccu-info delta for Colab)
        # at >= 80% -> BudgetWarning event; at >= 100% -> BudgetExceededError -> cancel()
```

Cost estimation uses a static, versioned rate table (`reliability/rates.toml`, e.g. Modal H100 ≈ $3.95/hr, B200 ≈ $6.25/hr) plus measured wall-time. Colab CCU burn is derived from `ccu-info` balance deltas. `BudgetExceededError` is **`ABORT`** (never retried) and triggers cooperative cancel + teardown so stopped IaaS instances don't keep billing.

---

### 6. Rate limiting — staying a good citizen (`colabctl/reliability/ratelimit.py`)

A token-bucket limiter sits in front of every outbound call, keyed by `(backend, account_email, op_class)`. This protects against both server-side 429s and — critically — against *looking like* a resource-farming bot.

```python
class RateLimitRule(BaseModel):
    rate_per_sec: float
    burst: int

DEFAULT_RULES = {
    ("colab-cli", "allocate"):  RateLimitRule(rate_per_sec=0.05, burst=2),   # ~1 alloc / 20s
    ("tun", "assign"):          RateLimitRule(rate_per_sec=0.05, burst=2),
    ("tun", "exec"):            RateLimitRule(rate_per_sec=5.0,  burst=10),
    ("vertex", "submit"):       RateLimitRule(rate_per_sec=0.8,  burst=5),    # < 60/min ceiling
    ("kaggle", "push"):         RateLimitRule(rate_per_sec=0.1,  burst=1),
    ("drive", "upload"):        RateLimitRule(rate_per_sec=2.0,  burst=5),
}
```

```python
class TokenBucket:
    async def acquire(self, n: int = 1) -> None:
        """Async-await until n tokens available; never busy-spins."""
class RateLimiter:
    def __init__(self, rules: dict): ...
    async def limit(self, backend: str, op_class: str): ...   # async context manager
```

Allocation/`assign` ops are throttled hard (≈1 per 20s default) and pass through the same `RetryBudget`. Server `Retry-After` always overrides the bucket. A circuit-level "good-citizen brake": three `AbuseBlockedError` events on one account within 24h force that account's Colab backends to `UNHEALTHY` and stop all proactive allocation for a cooldown window (default 6h), routing strictly to Modal/Vertex — the verdicts' core survivability move.

---

### 7. Graceful degradation & fallback triggering (`colabctl/reliability/degrade.py`)

#### 7.1 Circuit breaker (per backend+account)

```python
class CircuitState(str, Enum):
    CLOSED="closed"; OPEN="open"; HALF_OPEN="half_open"

class CircuitBreaker(BaseModel):
    failure_threshold: int = 5          # consecutive counts_against_circuit failures
    open_seconds: float = 300.0
    half_open_probes: int = 1
    # state machine: CLOSED --threshold--> OPEN --open_seconds--> HALF_OPEN
    #                HALF_OPEN --probe ok--> CLOSED ; --probe fail--> OPEN
```

Only failures whose `RecoveryPolicy.counts_against_circuit` is `True` trip it (so a user's `ExecutionError` or a benign 429 never opens the circuit, but `SchemaContractError`, `AbuseBlockedError`, repeated `TransportError`, and `BackendUnavailableError` do).

#### 7.2 Fallback router

The router is the embodiment of the capability-detecting abstraction. It scores live, healthy backends against the request's requirements and routes in priority order, skipping OPEN circuits.

```python
class BackendScore(BaseModel):
    backend: str
    eligible: bool          # capabilities satisfy request (interactive vs batch, GPU type)
    health: HealthState
    circuit: CircuitState
    priority: int           # config: colab=0, modal=1, vertex=2, hf=3, kaggle=4, runpod=5
    est_cost_usd: float | None

class FallbackRouter:
    def candidates(self, req: "JobRequest") -> list["Backend"]:
        """Ordered, filtered list. Excludes ineligible/unhealthy/OPEN-circuit backends."""
    async def route(self, req, op): ...   # try each candidate; on FALLBACK_BACKEND, advance
```

Fallback **triggers** (any of these advances to the next candidate, with a `BackendFallbackEvent` logged):

| Trigger | From → To example |
|---|---|
| `AbuseBlockedError` / `DENYLISTED` | Colab → Modal (verdict's primary survivability path) |
| `QuotaExhaustedError` (CCU ≤ 0, Kaggle weekly cap, GPU_ALL_REGIONS) | Colab → Vertex |
| `AcceleratorUnavailableError` with `require_exact` | Colab T4-only → Modal/Vertex for A100 |
| `SchemaContractError` (interface drift) | `/tun` escape hatch → sanctioned CLI → Modal |
| `BackendUnavailableError` (CLI yanked/py-mismatch; MCP tab closed) | CLI → MCP → Modal |
| Circuit OPEN for the backend | skip entirely until HALF_OPEN |

**Capability-aware degradation, not blind failover:** if the request is *interactive* (cell-by-cell agent loop) and only *batch* backends remain healthy, the router does **not** silently switch to a batch backend; it raises `BackendUnavailableError` with `user_message` explaining that interactive capability is unavailable and offering the batch (papermill/nbclient over Modal/Vertex) path explicitly. This prevents the "validated on free tier, behaves differently on Enterprise" parity trap the verdicts flag.

Every `submit`, before executing, also **externalizes durable state to Drive/GCS** so a mid-flight fallback or reassignment never loses results — runtimes are ephemeral by contract.

---

### 8. Structured logging & optional tracing (`colabctl/observability/`)

#### 8.1 Logging (`logging.py`)

`structlog` with JSON output by default (machine-readable for the MCP surface and CI), pretty console renderer when `stderr.isatty()`. Every log line carries the correlation context bound at the top of each operation.

```python
import structlog

def bind_op_context(*, job_id: str, idempotency_key: str, backend: str,
                    account_email: str, trace_id: str | None) -> None:
    structlog.contextvars.bind_contextvars(
        job_id=job_id, idem=idempotency_key, backend=backend,
        account=_hash_email(account_email),   # hashed, never raw PII in logs
        trace_id=trace_id)
```

**Mandatory redaction processor** (defense-in-depth, since the keychain is explicitly *not* a security boundary): a structlog processor scrubs any value matching credential patterns before serialization — OAuth access/refresh tokens, `X-Colab-Runtime-Proxy-Token`, `X-Goog-Colab-Token`, SAPISID-family cookies, `Authorization: Bearer …`, Modal/HF/Kaggle keys. Redaction is key-name based **and** value-shape based (`ya29.*`, `1//*`, long base64/JWT). Account emails are hashed. The `ColabctlError.context` dict is asserted pre-redacted at construction (debug-mode runtime check).

```python
REDACT_KEYS = {"authorization", "x-colab-runtime-proxy-token", "x-goog-colab-token",
               "refresh_token", "access_token", "sapisid", "cookie", "token",
               "modal_token_secret", "api_key"}
def redact_processor(logger, method, event: dict) -> dict: ...   # -> "***REDACTED***"
```

Standard event keys: `event`, `level`, `job_id`, `idem`, `backend`, `account` (hashed), `op`, `attempt`, `latency_ms`, `error`, `recovery_action`, `trace_id`.

#### 8.2 Structured events (`events.py`)

A pydantic event model is the typed contract emitted both to logs and to the MCP/CLI streams (so agents get structured reliability signals, not prose):

```python
class ReliabilityEvent(BaseModel):
    kind: str            # "runtime_expiring"|"backend_fallback"|"budget_warning"|
                         # "accelerator_downgraded"|"circuit_opened"|"abuse_blocked"|"reassigned"
    job_id: str
    backend: str
    severity: str        # "info"|"warning"|"error"
    detail: dict         # event-specific, pre-redacted
    ts: float
```

#### 8.3 Optional tracing & metrics (`tracing.py`, `metrics.py`)

OpenTelemetry, **off by default**, enabled via `reliability.tracing.enabled = true` + standard OTLP env (`OTEL_EXPORTER_OTLP_ENDPOINT`). One span per provider-abstraction verb (`submit`, `status`, `logs`, `fetch`, `cancel`) with child spans per backend attempt; `trace_id` is propagated into log context so logs and traces correlate. When OTel is disabled, `tracing.span()` is a zero-overhead null context manager.

Metrics (in-process counters/histograms, optionally exported as OTel meters):

| Metric | Type | Labels |
|---|---|---|
| `colabctl_op_total` | counter | backend, op, outcome |
| `colabctl_retries_total` | counter | backend, error_class |
| `colabctl_fallbacks_total` | counter | from_backend, to_backend, trigger |
| `colabctl_circuit_open_total` | counter | backend |
| `colabctl_runtime_reassign_total` | counter | backend, reason |
| `colabctl_op_latency_seconds` | histogram | backend, op |
| `colabctl_ccu_balance` | gauge | account |
| `colabctl_est_cost_usd` | gauge | backend, job_id |

---

### 9. Configuration (`reliability` block of `colabctl.toml`)

```toml
[reliability.backoff]
base_seconds = 0.5
max_seconds = 60.0
multiplier = 2.0
jitter = "full"
overall_deadline_seconds = 900

[reliability.retry_budget]
ratio = 0.1
min_tokens = 10

[reliability.circuit]
failure_threshold = 5
open_seconds = 300

[reliability.keepalive]
interval_seconds = 50
idle_warn_seconds = 4500       # 75 min
hard_max_seconds = 84600       # 23.5 h

[reliability.budget]
max_usd_per_job = 5.0
max_usd_per_session = 25.0
max_compute_units = 100
on_breach = "cancel"

[reliability.abuse_brake]
ban_events_threshold = 3
window_hours = 24
cooldown_hours = 6

[reliability.fallback]
priority = ["colab-cli", "modal", "vertex", "hf-jobs", "kaggle", "runpod"]
allow_interactive_to_batch_silent_switch = false

[observability]
log_format = "json"          # "json" | "console"
log_level = "info"
redact = true                # MUST stay true outside local debug

[observability.tracing]
enabled = false
```

---

### 10. End-to-end sequence (submit with full reliability path)

```
1.  ratelimiter.limit(backend, "allocate")  -> await token
2.  ledger.begin(key) -> (entry, is_new); if terminal SUCCEEDED -> return cached
3.  router.candidates(req) -> [colab-cli, modal, vertex] (healthy, eligible, circuit CLOSED)
4.  for backend in candidates:
      health = await backend.probe(); if not HEALTHY/DEGRADED -> next
      try:
        ep = run_with_retry(backend.allocate, on_reauth=auth.refresh)   # bounded, budgeted
        detect_accelerator(ep) -> AcceleratorGrant (record downgrade)
        spawn RuntimeHealthMonitor(ep)         # keep-alive + proxy-token refresh
        spawn BudgetGuard.watch(job_id)
        result = await backend.execute(req, ep)  # cell exec NOT retried; WS reconnect only
        ref = await drive.upload_blob(result.ipynb)   # durable, user-OAuth, plain blob
        ledger.mark(key, SUCCEEDED, result_ref=ref); circuit.record_success(backend)
        return result
      except ColabctlError as e:
        circuit.record(e); emit ReliabilityEvent
        if e.policy.action == REASSIGN_RUNTIME: reassign within backend (<=2), replay-safe
        elif e.policy.action == FALLBACK_BACKEND: continue          # next candidate
        else: ledger.mark(key, FAILED); raise
5.  exhausted candidates -> raise BackendUnavailableError(user_message=...)
```

#### 10.1 Edge cases & failure handling specific to this section

- **Reassignment loses VM state:** mitigated by mandatory checkpoint-to-Drive before `idle_warn`/`hard_max` and replay of completed-cell guard files; the ledger `result_ref` is the source of truth.
- **Refresh token dies mid-run (7-day Testing-status):** surfaces as `RefreshTokenDeadError` → `USER_INTERVENTION`; the job is paused (not failed), state checkpointed, and the CLI/MCP emits a re-consent prompt. Unattended agents get a structured `runtime_expiring`/`reauth_required` event rather than a silent hang.
- **Silent GPU downgrade:** never assumed; always probed and reported. `require_exact` callers fall back; others continue with a logged warning.
- **Schema/interface drift on `/tun/m/*` or CLI stdout:** classified `SchemaContractError` (pinned-version capability probe catches most), immediately fails over to the sanctioned CLI or Modal; never retried against the drifted surface.
- **Clock skew vs `tokenExpiresInSeconds`:** proxy-token refresh fires at `< 2×keepalive` margin and on any `RuntimeProxyTokenError`, never relying on a single absolute expiry timestamp.
- **Retry storm against abuse heuristics:** structurally prevented by the `RetryBudget` + hard allocation rate limit + abuse brake; the system prefers to fall back over hammering Colab.
- **Budget runaway on per-second backends:** `BudgetGuard` cancels and tears down; `BudgetExceededError` is non-retryable and triggers IaaS teardown reconciliation so stopped pods stop billing.
- **MCP bridge tab closed mid-session:** detected by runtime health (`fe_connected` false) → `BackendUnavailableError` → fall back to CLI/Modal; never silently stalls returning empty results.

### 11.x Key decisions

- Error taxonomy is the single source of truth: every exception class carries a RecoveryPolicy (action, retryable, max_attempts, respects_retry_after, counts_against_circuit) so the retry, idempotency, rate-limit, circuit-breaker, and fallback layers all key off typed policy instead of pattern-matching message strings.
- Full-jitter exponential backoff gated by a per-(backend,account) RetryBudget token bucket, an overall_deadline that beats max_attempts, and server Retry-After / tokenExpiresInSeconds hints that always override computed delay.
- Hard rule: user cell execution is NEVER retried (re-running non-idempotent code is the worst default), runtime allocation is retried only with a strict ~1-per-20s rate limit + bounded attempts, and abuse/quota/budget failures are never retried — they immediately trigger backend fallback.
- Idempotency via caller-stable keys + an aiosqlite ExecutionLedger that survives process restarts: same key replays cached result_ref if SUCCEEDED, attaches (never resubmits) if RUNNING/PENDING, and an in-VM once-guard file complements (not replaces) durable Drive-externalized state because runtimes are ephemeral.
- Anti-abuse is a first-class reliability feature: RetryBudget + conservative ~50s keep-alive (no fake activity) + hard allocation rate limit + an abuse-brake that forces Colab UNHEALTHY for a cooldown after N AbuseBlockedError events on an account, routing strictly to Modal/Vertex — the verdicts' core survivability move.
- GPU grants are verify-after-allocate via in-kernel nvidia-smi/torch/TPU probes that detect silent downgrades; BudgetGuard enforces per-job/session USD and Colab CCU ceilings and cancels+tears down on breach so per-second IaaS billing cannot run away.
- Capability-aware degradation, not blind failover: circuit breaker only trips on failures flagged counts_against_circuit, and the FallbackRouter refuses to silently switch an interactive request onto a batch-only backend (config allow_interactive_to_batch_silent_switch=false) to avoid the free-tier-vs-Enterprise parity trap.
- Observability: structlog JSON with a mandatory key-name + value-shape redaction processor (keychain is defense-in-depth, not a boundary), hashed account emails, a typed ReliabilityEvent model emitted to logs and the MCP/CLI stream, and optional OFF-by-default OpenTelemetry tracing/metrics correlated via trace_id.
- Kernel WebSocket reconnect re-sends the corrected header-only auth recipe (X-Colab-Runtime-Proxy-Token header only, plus X-Goog-Colab-Tunnel and X-Goog-Colab-Token XSRF, OAuth Bearer kept separate) and reconciles in-flight msg_id state before re-executing, preserving idempotency.

### 11.y Section risks

- Several Colab-specific contracts the recovery logic depends on are unverified/undocumented (HTTP 412 binding for TooManyAssignmentsError, SUCCESS/DENYLISTED/QUOTA_* outcome enum, tokenExpiresInSeconds semantics, XSSI prefix); SchemaContractError handling mitigates drift but the literal classification mappings may be wrong on day one and need a validation spike against the live backend.
- Abuse-detection is opaque and bans are no-recourse: even with conservative rate limits, retry budgets, and the abuse brake, sustained agent-driven GPU usage can still trip Google's heuristics and ban the account; the design contains but cannot eliminate this — residual risk must stay explicitly surfaced to the user.
- Cost awareness relies on a static, manually-maintained rate table (rates.toml) plus measured wall-time; provider price changes or hidden surcharges (e.g. Vertex management fee, RunPod stopped-pod storage billing) can make BudgetGuard estimates under-count real spend until the table is updated.
- Cross-process idempotency depends on a local aiosqlite ledger; concurrent colabctl invocations on different machines sharing one account have no shared ledger, so the same idempotency key could double-submit — multi-host correctness would require an external shared store not specified here.
- Reassignment-with-checkpoint assumes user/agent workloads checkpoint to Drive/GCS, but arbitrary agent-generated code often won't; long in-memory training that exceeds idle/24h limits will still lose state despite the monitor's pre-emptive warnings, capping reliability for long jobs.
- The sanctioned CLI is a fast-moving 0.5.x dependency (yanked releases, Python 3.13-only, no confirmed stable JSON mode); BackendUnavailableError + capability probe + version pin protect the abstraction, but stdout-parsing fragility means classification of CLI failures may misfire and needs continuous integration testing against pinned versions.
- Redaction is best-effort pattern + key-name based; a novel credential shape or a token logged inside an unexpected nested structure could leak into JSON logs — the debug-mode assertion on ColabctlError.context reduces but does not fully close this gap.

---

## 12. Security, Compliance & Account Safety

This section specifies how `colabctl` stores credentials, requests the minimum privilege, behaves as a "good citizen" against Colab's backend, isolates the caller from remotely-run code, detects throttling/challenges and bans before they become account-fatal, and discloses residual legal/operational risk. It is **not** an MVP cut: every safeguard described here is load-bearing because the product's two irreducible risks — Google interface churn and **opaque, no-appeal abuse-detection bans** — land squarely in this section.

The guiding principle, inherited from the architecture decision, is **defense-in-depth, not security theater**: the OS keychain is treated as a hardening layer (no plaintext on disk, no git leaks), *never* as a trust boundary; the proxy token is a header-only credential and is sent exactly one way; durable state always lives outside the ephemeral VM; and every Colab call is rate-shaped and ban-aware. The escape-hatch direct `/tun/m/*` transport is the only path that meaningfully raises ToS/ban exposure, so it is opt-in, version-gated, and forces an explicit user acknowledgement that this module records and enforces.

### Module layout

```
colabctl/
  security/
    __init__.py
    secrets.py            # SecretStore facade + pluggable backends
    backends/
      keyring_backend.py  # OS keychain via `keyring`, with chunking
      secretservice.py    # headless Linux (SecretService / D-Bus)
      wincred.py          # Windows Credential Manager
      age_file.py         # age-encrypted file fallback (CI / no-keychain)
    scopes.py             # least-privilege scope registry & assertions
    redaction.py          # log/exception scrubbing of tokens & cookies
    crypto.py             # constant-time compare, key derivation helpers
  compliance/
    __init__.py
    policy.py             # GoodCitizenPolicy model + defaults per backend
    citizen.py            # rate shaping, idle/keepalive cadence, jitter
    consent.py            # ToS-tier gating + escape-hatch acknowledgement
    disclaimer.py         # canonical disclaimer text + acceptance ledger
  safety/
    __init__.py
    detector.py           # classify backend responses -> SafetySignal
    breaker.py            # per-account circuit breaker + backoff
    throttle.py           # token-bucket / concurrency limiter (asyncio)
    isolation.py          # caller-side guarantees around remote exec
    quotas.py             # CCU/assignment accounting, denylist memory
  models.py               # shared pydantic v2 models (imported below)
```

All clock reads go through `colabctl.util.clock.now()` (monotonic for backoff, wall for audit) so tests can inject time. All randomness for jitter goes through `colabctl.util.rng.jitter_rng` (seedable) so cadence is deterministic in CI.

---

### 1. Secret handling & least-privilege scopes

#### 1.1 Threat model (stated plainly)

The keychain (macOS Keychain, SecretService, Windows Credential Manager) protects against: plaintext-on-disk theft, accidental git commits, and casual filesystem reads. It does **not** protect against: any same-user Python process after "always allow" is granted (this is the documented `keyring` behavior — venvs symlink the same interpreter, so venv isolation does not help), root, or memory inspection. We design as if **any local credential is readable by any code running as the user**, which is why the highest-value mitigation is *scope minimization and short-lived tokens*, not at-rest encryption.

Blast radius is deliberately bounded: we store OAuth refresh/access tokens scoped to Colab + Drive and per-runtime proxy tokens. We **never** store full-account Google session cookies (`SAPISID`/`SID`/`HSID`), because cookie replay is (a) ToS-prohibited, (b) structurally defeated by DBSC in the product's own ship window, and (c) full-Google-identity blast radius. The `SecretStore` actively *rejects* writing any value whose key matches the cookie denylist (see §1.4).

#### 1.2 Secret data models (`colabctl/models.py`)

```python
from __future__ import annotations
import datetime as dt
from enum import StrEnum
from typing import Literal
from pydantic import BaseModel, Field, SecretStr, field_validator

class SecretKind(StrEnum):
    OAUTH_REFRESH = "oauth_refresh"     # long-lived; Drive + colab identity
    OAUTH_ACCESS  = "oauth_access"      # short-lived bearer identity token
    RUNTIME_PROXY = "runtime_proxy"     # header-only X-Colab-Runtime-Proxy-Token
    PROVIDER_KEY  = "provider_key"      # Modal / HF / Kaggle / RunPod creds
    XSRF          = "xsrf"              # X-Goog-Colab-Token value

class StoredSecret(BaseModel):
    account_email: str                  # primary keying dimension
    kind: SecretKind
    value: SecretStr                    # never logged; redacted in repr
    scopes: tuple[str, ...] = ()        # for OAuth kinds; asserted on read
    issued_at: dt.datetime
    expires_at: dt.datetime | None = None
    # provenance: which transport/client minted this, for audit & revocation
    provenance: Literal["official_cli", "colab_mcp", "escape_hatch", "drive_oauth", "provider_sdk"]

    @field_validator("value")
    @classmethod
    def _forbid_cookie_blobs(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        # Cookie blobs are bearer-equivalent full-account creds: never accepted.
        if any(marker in raw for marker in ("SAPISID=", "__Secure-3PSID", "HSID=", "SSID=")):
            raise ValueError("refusing to store Google session cookie material")
        return v

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= dt.datetime.now(dt.UTC)
```

#### 1.3 `SecretStore` facade & backend protocol (`colabctl/security/secrets.py`)

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class SecretBackend(Protocol):
    name: str
    def available(self) -> bool: ...
    def set_blob(self, service: str, key: str, blob: bytes) -> None: ...
    def get_blob(self, service: str, key: str) -> bytes | None: ...
    def delete(self, service: str, key: str) -> None: ...

SERVICE = "com.colabctl.secrets"
CHUNK_BYTES = 3 * 1024            # < macOS Keychain ~4KB soft limit, with headroom

class SecretStore:
    def __init__(self, backend: SecretBackend | None = None) -> None:
        self._backend = backend or select_backend()

    # key = f"{account_email}:{kind}" so secrets are per-account-email keyed
    def put(self, secret: StoredSecret) -> None: ...
    def get(self, account_email: str, kind: SecretKind) -> StoredSecret | None: ...
    def delete(self, account_email: str, kind: SecretKind) -> None: ...
    def purge_account(self, account_email: str) -> int: ...   # used on ban/logout
```

**Backend selection (`select_backend()`)** — deterministic, fail-loud, never silently falls back to plaintext:

```python
def select_backend() -> SecretBackend:
    forced = os.environ.get("COLABCTL_SECRET_BACKEND")   # explicit override for CI
    candidates = {
        "keyring": KeyringBackend, "secretservice": SecretServiceBackend,
        "wincred": WinCredBackend, "age": AgeFileBackend,
    }
    if forced:
        b = candidates[forced]()
        if not b.available():
            raise SecretBackendUnavailable(forced)
        return b
    for cls in (KeyringBackend, SecretServiceBackend, WinCredBackend):
        b = cls()
        if b.available():
            return b
    # Headless server / CI with no keychain: require explicit age key, do NOT
    # degrade to plaintext. AgeFileBackend reads COLABCTL_AGE_KEY (or a file path).
    age = AgeFileBackend()
    if age.available():
        return age
    raise SecretBackendUnavailable(
        "No keychain and no COLABCTL_AGE_KEY; refusing to store secrets in plaintext."
    )
```

**Chunking algorithm** (the verdict's explicit demand — OAuth tokens are tiny, but the >4KB rule is enforced uniformly so a future larger blob can't trigger `securityd` errors):

```python
def set_blob(self, service, key, blob):
    chunks = [blob[i:i+CHUNK_BYTES] for i in range(0, len(blob), CHUNK_BYTES)] or [b""]
    keyring.set_password(service, f"{key}#meta", str(len(chunks)))
    for idx, c in enumerate(chunks):
        keyring.set_password(service, f"{key}#{idx}", base64.b64encode(c).decode())

def get_blob(self, service, key):
    meta = keyring.get_password(service, f"{key}#meta")
    if meta is None:
        return None
    return b"".join(
        base64.b64decode(keyring.get_password(service, f"{key}#{i}"))
        for i in range(int(meta))
    )
```

#### 1.4 Least-privilege scopes (`colabctl/security/scopes.py`)

Scopes are minimized to what each transport actually needs, and **asserted at read time** so a token can never be used outside its grant.

| Transport / backend            | OAuth scopes requested                                  | Storage owner                        |
|--------------------------------|---------------------------------------------------------|--------------------------------------|
| Official CLI (primary)         | mirror the CLI's own grant (`profile`, `email`, `colaboratory`) — we do **not** request more | minted by official CLI, we read it    |
| Drive sync (durable artifacts) | `https://www.googleapis.com/auth/drive.file` (per-file), NOT full `drive` | user-OAuth, files owned by the human |
| Escape hatch (`/tun/m/*`)      | identity Bearer + header-only proxy token               | minted via official flow only        |
| Vertex / Colab Enterprise      | GCP ADC / service-account, `cloud-platform`             | ADC, never persisted by us           |
| Modal / HF / Kaggle / RunPod   | provider-native API keys                                | provider SDK conventions             |

We deliberately request `drive.file` (per-file scope) rather than the broad `auth/drive`. `drive.file` is sufficient for plain-blob `.ipynb` upload/download to files the tool itself creates in My Drive, keeps ownership and quota with the human, and shrinks the blast radius of a leaked refresh token from "entire Drive" to "files this app created."

```python
REQUIRED_SCOPES: dict[str, frozenset[str]] = {
    "drive_oauth": frozenset({"https://www.googleapis.com/auth/drive.file"}),
    "official_cli": frozenset({"profile", "email",
                               "https://www.googleapis.com/auth/colaboratory"}),
}

def assert_scopes(secret: StoredSecret, transport: str) -> None:
    need = REQUIRED_SCOPES.get(transport, frozenset())
    have = frozenset(secret.scopes)
    if not need <= have:
        raise InsufficientScope(transport, missing=tuple(need - have))
```

#### 1.5 Redaction (`colabctl/security/redaction.py`)

A logging filter and exception hook scrub anything that looks like a credential before it can reach logs, tracebacks, or the MCP wire. This runs on *all* log records and is installed by `configure_logging()` at import of the CLI/MCP entrypoints.

```python
import re, logging
_PATTERNS = [
    re.compile(r"(X-Colab-Runtime-Proxy-Token:\s*)\S+", re.I),
    re.compile(r"(Authorization:\s*Bearer\s+)\S+", re.I),
    re.compile(r"(X-Goog-Colab-Token:\s*)\S+", re.I),
    re.compile(r"(ya29\.[\w\-\.]+)"),                 # Google access tokens
    re.compile(r"(1//[\w\-]+)"),                      # Google refresh tokens
    re.compile(r"(SAPISID|HSID|SSID|__Secure-3PSID)=\S+"),
]
def scrub(text: str) -> str:
    for p in _PATTERNS:
        text = p.sub(lambda m: (m.group(1) if m.lastindex else "") + "<redacted>", text)
    return text

class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(str(record.msg))
        if record.args:
            record.args = tuple(scrub(str(a)) for a in record.args)
        return True
```

`SecretStr` (pydantic) guarantees secrets never appear in model `repr()`/`str()`; the redaction filter is the second layer for hand-built log lines and third-party libraries.

---

### 2. ToS-risk mitigations & the "good citizen" policy

#### 2.1 ToS posture per backend

The architecture establishes that for **paid Colab Pro with a positive compute balance**, the FAQ's "no UI bypass / no remote control" prohibitions are explicitly *lifted*, and Google now ships an official agent CLI — so the sanctioned-primary path lands in the **LOW/MEDIUM** band. The residual risk is opaque abuse-detection, not a clause violation. We encode this as a per-backend ToS tier and gate behavior on it.

```python
class TosTier(StrEnum):
    LOW = "low"          # official CLI/MCP, Modal, HF, Vertex, Drive
    MEDIUM = "medium"    # escape-hatch direct /tun/m/* on a PAID account
    HIGH = "high"        # not shipped: cookie replay, UI scraping (rejected)

class BackendCompliance(BaseModel):
    backend: str
    tos_tier: TosTier
    requires_paid_account: bool          # escape hatch requires Pro w/ balance
    requires_ack: bool                   # explicit user acknowledgement gate
    is_default: bool
```

`HIGH`-tier transports are not implemented at all. `MEDIUM` (the escape hatch) is never default and requires the consent gate in §2.4.

#### 2.2 GoodCitizenPolicy (`colabctl/compliance/policy.py`)

```python
class GoodCitizenPolicy(BaseModel):
    # Idle / lifecycle
    keepalive_interval_s: float = 60.0          # match the documented ~60s cadence
    keepalive_jitter_s: float = 8.0             # +/- jitter so calls aren't metronomic
    max_idle_before_release_s: float = 1800.0   # proactively unassign idle runtimes
    respect_idle_timeout: bool = True           # do NOT fight Colab's idle reclamation

    # Concurrency / cadence (per account)
    max_concurrent_assignments: int = 1         # Pro happy-path; never multi-assign
    max_assign_attempts_per_hour: int = 6
    min_seconds_between_assigns: float = 30.0
    request_rate_per_minute: float = 30.0       # token-bucket ceiling on backend calls
    request_burst: int = 5

    # Human-like cadence where it matters (interactive surfaces)
    humanize_interactive: bool = True
    interactive_min_gap_s: float = 0.4          # debounce rapid cell submits

    # Hard prohibitions the tool refuses to do
    forbid_multi_account_pooling: bool = True   # FAQ-banned; never circumvent quotas
    forbid_cookie_auth: bool = True
```

The defaults are deliberately conservative. `max_concurrent_assignments = 1` is the single most important account-safety setting: a headless agent farming many concurrent GPU sessions is exactly the fingerprint that trips abuse heuristics and yields the `TooManyAssignmentsError` (HTTP 412). The tool **refuses** to pool multiple accounts to beat quotas (`forbid_multi_account_pooling`) because the FAQ explicitly bans "using multiple accounts to work around resource usage restrictions" — this is a ToS landmine and the obvious-but-prohibited scaling path. When more throughput is needed, the provider abstraction routes to Modal/Vertex/HF instead of multiplying Colab accounts.

#### 2.3 Rate shaping, keepalive cadence & jitter (`colabctl/compliance/citizen.py`)

```python
class CitizenGovernor:
    """Wraps every outbound Colab backend call: rate limit + jittered cadence."""
    def __init__(self, policy: GoodCitizenPolicy, throttle: TokenBucket) -> None:
        self._policy = policy
        self._throttle = throttle
        self._last_assign_ts: float = 0.0
        self._assign_window: deque[float] = deque()

    async def guard_request(self) -> None:
        await self._throttle.acquire()             # token-bucket; blocks if over rate

    async def guard_assign(self) -> None:
        now = clock.monotonic()
        # enforce min gap
        gap = now - self._last_assign_ts
        if gap < self._policy.min_seconds_between_assigns:
            await asyncio.sleep(self._policy.min_seconds_between_assigns - gap)
        # enforce per-hour ceiling
        self._evict_old(self._assign_window, horizon_s=3600)
        if len(self._assign_window) >= self._policy.max_assign_attempts_per_hour:
            raise AssignRateExceeded(retry_after_s=self._seconds_until_slot())
        self._assign_window.append(clock.monotonic())
        self._last_assign_ts = clock.monotonic()

    def next_keepalive_delay(self) -> float:
        j = self._policy.keepalive_jitter_s
        return self._policy.keepalive_interval_s + jitter_rng.uniform(-j, j)
```

The keepalive jitter matters: metronomic, sub-second-precise keepalives are a bot signature. We send keepalives on the documented ~60s cadence with bounded jitter, and we **stop** keepalives (rather than fighting reclamation) once `max_idle_before_release_s` elapses with no kernel activity — proactively calling `/unassign` to be a good citizen and free GPU capacity. We never simulate fake "active programming" to defeat the idle timeout.

#### 2.4 Consent & escape-hatch acknowledgement (`colabctl/compliance/consent.py`)

The opt-in direct `/tun/m/*` transport raises ToS exposure (rolling your own client is "less authorized" than Google's CLI) and is drift-/ban-exposed. It is disabled unless the user records an acknowledgement that is persisted and re-shown on version bumps.

```python
class EscapeHatchAck(BaseModel):
    account_email: str
    accepted_at: dt.datetime
    package_version: str
    risk_text_hash: str          # sha256 of the disclaimer they accepted
    confirmed_paid_account: bool # escape hatch requires Pro w/ positive balance

def require_escape_hatch_consent(account_email: str, version: str) -> EscapeHatchAck:
    existing = load_ack(account_email)
    if existing and existing.package_version == version \
            and existing.risk_text_hash == current_risk_hash():
        return existing
    # CLI: interactive prompt; MCP: returns a structured "consent_required" error
    raise EscapeHatchConsentRequired(disclaimer=ESCAPE_HATCH_DISCLAIMER)
```

The CLI surfaces this as an explicit `colabctl auth enable-escape-hatch` step; the MCP server returns a structured `consent_required` tool error rather than silently enabling a riskier transport on an agent's behalf.

---

### 3. Isolation of remotely-run code (caller's perspective)

We must be honest about what isolation we can and cannot provide. **Code that runs inside a Colab VM is not sandboxed from the caller's standpoint in the way Modal Sandboxes (gVisor) are** — it runs in Google's shared VM with the user's own Drive mounts and credentials reachable from inside that VM. Our job is to protect the *caller's* host and credentials and to make the trust boundary explicit and routable.

#### 3.1 Caller-side guarantees (`colabctl/safety/isolation.py`)

```python
class ExecutionIsolation(BaseModel):
    backend: str
    # What the caller's machine is exposed to:
    runs_on_callers_host: bool          # False for Colab/Modal/Vertex; True only for local-jupyter escape rig
    network_isolation: Literal["gvisor", "vm", "none"]
    credential_exposure: Literal["none", "vm_scoped_token", "user_drive_oauth"]
    untrusted_code_recommended: bool    # True only for Modal Sandbox

ISOLATION_MATRIX = {
    "colab":   ExecutionIsolation(backend="colab", runs_on_callers_host=False,
                                  network_isolation="vm",
                                  credential_exposure="vm_scoped_token",
                                  untrusted_code_recommended=False),
    "modal":   ExecutionIsolation(backend="modal", runs_on_callers_host=False,
                                  network_isolation="gvisor",
                                  credential_exposure="none",
                                  untrusted_code_recommended=True),
}
```

Concrete caller-side rules the execution layer enforces:

1. **No host execution.** Remote code never runs on the caller's machine. The only path that could (the local-Jupyter test rig) is gated behind the same escape-hatch consent and clearly labeled `runs_on_callers_host=True`.
2. **Credential confinement.** The OAuth identity/refresh tokens stay on the caller's host; only the **VM-scoped, short-lived runtime-proxy token** is used to talk to the kernel. The full-account refresh token is never injected into a runtime. If the user mounts Drive inside the VM, `isolation.py` emits a one-time warning that VM-resident code can read that Drive scope.
3. **Output sanitization.** Kernel stream outputs (stdout/stderr/`display_data`) are treated as untrusted data. When surfaced through the MCP server to an agent, outputs are size-capped, never `eval`'d, and HTML/JS `display_data` mime-types are passed through as inert text (no rendering) to prevent prompt-injection-via-output from steering the agent.
4. **Route untrusted code to a real sandbox.** For agent-*generated* code (the highest-risk input), the provider abstraction's default recommendation is **Modal Sandboxes** (`untrusted_code_recommended=True`, gVisor-isolated), not Colab. `submit(..., trust="untrusted")` will refuse the Colab backend and route to Modal unless the caller explicitly overrides.

```python
def select_backend_for_trust(req: SubmitRequest, registry) -> Backend:
    if req.trust == "untrusted":
        if req.backend == "colab" and not req.force:
            raise UntrustedCodeOnUnsafeBackend(
                advice="route to Modal Sandbox (gVisor) or pass force=True")
        return registry.get("modal")
    return registry.get(req.backend or "colab")
```

#### 3.2 Durable-state externalization

Because runtimes are ephemeral (idle ~90 min, 12–24 h hard cap, re-assignment yields a fresh VM with no disk/memory state), **no durable artifact is trusted to live in the VM**. Inputs/outputs/checkpoints round-trip to the human's My Drive via `drive.file`-scoped user-OAuth plain-blob `.ipynb` uploads (never a service account — an SA cannot own Google-native files and returns 403 `storageQuotaExceeded`). On re-assignment, `safety/quotas.py` triggers a checkpoint-restore from Drive/GCS rather than silently restarting from zero.

---

### 4. Account-safety safeguards (throttling, challenges, ban avoidance)

This is the section that keeps the user's paid account alive. Abuse-detection is **opaque and offers no appeal SLA**, so the strategy is: detect early, back off hard, and *route away* via the provider abstraction rather than retrying into a ban.

#### 4.1 Safety signal classification (`colabctl/safety/detector.py`)

```python
class SignalKind(StrEnum):
    OK = "ok"
    TRANSIENT = "transient"        # 5xx, socket hangup, timeout -> retry w/ backoff
    THROTTLED = "throttled"        # 429 / explicit rate limit -> backoff, slow cadence
    QUOTA = "quota"                # CCU exhausted / QUOTA_DENIED -> stop, surface to user
    TOO_MANY_ASSIGNMENTS = "too_many_assignments"  # HTTP 412 -> never retry-storm
    CHALLENGE = "challenge"        # CAPTCHA / interactive challenge detected
    DENYLISTED = "denylisted"      # abuse-detection block -> HALT, do not retry
    AUTH_EXPIRED = "auth_expired"  # token expiry -> refresh once, then re-auth
    UNKNOWN = "unknown"

class SafetySignal(BaseModel):
    kind: SignalKind
    backend: str
    account_email: str
    http_status: int | None = None
    outcome: str | None = None     # e.g. SUCCESS/DENYLISTED/QUOTA_* if present
    retry_after_s: float | None = None
    raw_excerpt: str               # redacted, for diagnostics

def classify(resp: BackendResponse) -> SafetySignal:
    s = resp.status
    body = scrub(resp.text or "")
    if s == 412 or "TooManyAssignmentsError" in body:
        return SafetySignal(kind=SignalKind.TOO_MANY_ASSIGNMENTS, ...)
    if "DENYLISTED" in body or "suspected abusive activity" in body.lower():
        return SafetySignal(kind=SignalKind.DENYLISTED, ...)     # treat as terminal
    if s == 429:
        return SafetySignal(kind=SignalKind.THROTTLED,
                            retry_after_s=_parse_retry_after(resp), ...)
    if s in (401, 403) and _looks_like_auth(body):
        return SafetySignal(kind=SignalKind.AUTH_EXPIRED, ...)
    if _looks_like_captcha(resp):                                 # HTML challenge page
        return SafetySignal(kind=SignalKind.CHALLENGE, ...)
    if 500 <= s < 600 or resp.is_socket_error:
        return SafetySignal(kind=SignalKind.TRANSIENT, ...)
    if "QUOTA" in (resp.outcome or ""):
        return SafetySignal(kind=SignalKind.QUOTA, ...)
    return SafetySignal(kind=SignalKind.OK if s < 400 else SignalKind.UNKNOWN, ...)
```

Note the schema-uncertainty discipline from the verdicts: the literal `412` binding and the `SUCCESS/DENYLISTED/QUOTA_*` enum are *plausible-but-unverified*, so `classify()` matches on **both** the HTTP status **and** body markers and falls back to `UNKNOWN` (which is treated conservatively, like `TRANSIENT` with a low retry cap) rather than asserting a hard branch that silently breaks on drift.

#### 4.2 Response → action policy

| Signal                  | Retry?            | Backoff                          | Side effects                                                                 |
|-------------------------|-------------------|----------------------------------|------------------------------------------------------------------------------|
| `OK`                    | —                 | reset breaker                    | record success                                                               |
| `TRANSIENT`             | yes, ≤3           | exp 1s,2s,4s + jitter            | —                                                                            |
| `THROTTLED`             | yes, ≤2           | honor `Retry-After`, else 30s    | halve `request_rate_per_minute` for the session                              |
| `AUTH_EXPIRED`          | refresh once      | —                                | refresh token; if refresh fails → require re-auth, do not loop               |
| `TOO_MANY_ASSIGNMENTS`  | **no**            | —                                | release/unassign stale assignments; surface; **never** open a 2nd account    |
| `QUOTA`                 | **no**            | —                                | surface CCU exhaustion; suggest routing to Modal/Vertex                      |
| `CHALLENGE`             | **no**            | —                                | **halt account**, require human-in-the-loop (colab-mcp browser path)         |
| `DENYLISTED`            | **no (terminal)** | open circuit, **24 h** cooldown  | mark account denylisted, halt all Colab traffic, notify, **purge** retries   |

The decisive rule: **`DENYLISTED` and `CHALLENGE` are never retried.** Retrying into an abuse block is how a temporary throttle becomes a permanent, unappealable ban. On `DENYLISTED` the circuit breaker opens for the whole account and the provider abstraction routes new work to a different backend.

#### 4.3 Per-account circuit breaker (`colabctl/safety/breaker.py`)

```python
class BreakerState(StrEnum):
    CLOSED = "closed"; OPEN = "open"; HALF_OPEN = "half_open"

class AccountCircuitBreaker(BaseModel):
    account_email: str
    state: BreakerState = BreakerState.CLOSED
    consecutive_failures: int = 0
    opened_at: dt.datetime | None = None
    cooldown_s: float = 0.0
    reason: SignalKind | None = None

class BreakerManager:
    # thresholds
    FAILURE_THRESHOLD = 3              # consecutive TRANSIENT/THROTTLED -> open briefly
    DENYLIST_COOLDOWN_S = 24 * 3600    # opaque abuse block -> long, human-revisitable
    CHALLENGE_COOLDOWN_S = 3600

    def on_signal(self, sig: SafetySignal) -> None:
        b = self._get(sig.account_email)
        if sig.kind in (SignalKind.DENYLISTED, SignalKind.CHALLENGE):
            self._open(b, reason=sig.kind,
                       cooldown=self.DENYLIST_COOLDOWN_S
                                if sig.kind is SignalKind.DENYLISTED
                                else self.CHALLENGE_COOLDOWN_S)
            emit_alert(b)                       # PushNotification / log / MCP error
            return
        if sig.kind in (SignalKind.TRANSIENT, SignalKind.THROTTLED, SignalKind.UNKNOWN):
            b.consecutive_failures += 1
            if b.consecutive_failures >= self.FAILURE_THRESHOLD:
                self._open(b, reason=sig.kind, cooldown=self._adaptive_cooldown(b))
        elif sig.kind is SignalKind.OK:
            self._close(b)

    def allow(self, account_email: str) -> bool:
        b = self._get(account_email)
        if b.state is BreakerState.OPEN:
            if clock.now() >= b.opened_at + timedelta(seconds=b.cooldown_s):
                b.state = BreakerState.HALF_OPEN     # let exactly one probe through
                return True
            return False
        return True
```

The breaker is **persisted** (in the same store as quotas) so that a `DENYLISTED` cooldown survives process restarts — an agent restarting in a crash-loop must not hammer a denylisted account back into a deeper ban.

#### 4.4 Concurrency limiter & assignment accounting (`colabctl/safety/throttle.py`, `quotas.py`)

```python
class TokenBucket:
    def __init__(self, rate_per_min: float, burst: int): ...
    async def acquire(self) -> None: ...        # asyncio-safe; blocks when empty

class AssignmentLedger(BaseModel):
    account_email: str
    active_assignments: list[str] = []          # endpoint ids
    ccu_balance: float | None = None
    last_seen_outcome: str | None = None
    denylisted_until: dt.datetime | None = None
```

A global `asyncio.Semaphore(policy.max_concurrent_assignments)` enforces the single-assignment happy path. The ledger reconciles against `/tun/m/assignments` on startup and before each new assign, so a crashed process can't leak orphaned runtimes (which both burn CCUs and look like abuse).

#### 4.5 Sequence of operations: a safe `submit → run → fetch`

```
1. consent: verify backend tier; if escape-hatch -> require ack (§2.4)
2. breaker.allow(account)?           no  -> raise AccountTemporarilyHalted(retry_after)
3. governor.guard_assign()           (min-gap + per-hour ceiling)
4. semaphore.acquire()               (max_concurrent_assignments)
5. POST /tun/m/assign                via CitizenGovernor.guard_request()
      -> classify(resp)
         DENYLISTED/CHALLENGE -> breaker.open + route-away + raise   (NO retry)
         TOO_MANY_ASSIGNMENTS -> reconcile ledger, release, raise     (NO retry)
         THROTTLED/TRANSIENT  -> backoff + retry (capped)
6. mint runtime-proxy token; store as RUNTIME_PROXY (header-only)
7. start keepalive task: every next_keepalive_delay() seconds, until idle-release
8. exec over kernel websocket: token sent ONLY as X-Colab-Runtime-Proxy-Token
      + X-Goog-Colab-Tunnel + X-Goog-Colab-Token; Bearer carries SEPARATE identity
9. stream outputs (sanitized, size-capped) -> caller / MCP
10. checkpoint artifacts -> Drive (drive.file, user-owned) at intervals
11. on idle > max_idle_before_release_s -> stop keepalive, POST /unassign, release sem
12. on any DENYLISTED at any step -> halt account, route remaining work via abstraction
```

#### 4.6 Auth-recipe correctness (security-relevant detail)

The runtime-proxy token is a **header-only** credential and is sent **exactly once**, in `X-Colab-Runtime-Proxy-Token`. It is **not** placed in the `Authorization: Bearer` header (which carries the *separate* OAuth identity token) and **not** in a query param. Sending it three ways (the rejected recipe) at best is ignored and at worst collides with the real Bearer header. The kernel handshake also sends `X-Goog-Colab-Tunnel: true` and the `X-Goog-Colab-Token` XSRF value. `assert_scopes` and `redaction.scrub` both run on this path.

---

### 5. Legal/operational risks, mitigations & recommended disclaimer

#### 5.1 Risk register (frank)

| # | Risk | Likelihood | Impact | Mitigation in this package |
|---|------|-----------|--------|----------------------------|
| 1 | **Opaque abuse-detection ban** of the user's paid account, no appeal SLA | Medium (higher under sustained headless GPU) | High (account-wide) | §2.2 conservative concurrency=1, §2.3 jittered cadence, §4 `DENYLISTED`/`CHALLENGE` = terminal-no-retry, route-away via abstraction, persisted 24h cooldown |
| 2 | **ToS clause exposure** ("access other than authorized by Google") on escape hatch | Low–Medium (paid Pro lifts UI-bypass clause) | Medium | Default to sanctioned official CLI/MCP; escape hatch opt-in + acknowledged (§2.4); never on free tier |
| 3 | **Multi-account quota circumvention** (explicitly FAQ-banned) | Low (we forbid it) | High | `forbid_multi_account_pooling=True`; abstraction scales via Modal/Vertex, never via account pooling |
| 4 | **Credential leak** of OAuth refresh token | Low | Medium (scoped: `drive.file` + Colab, not full account) | Keychain at-rest, `drive.file` least-privilege, no cookie storage, redaction filter, `purge_account` on ban |
| 5 | **Full-account cookie liability** | N/A (designed out) | — | Cookie auth structurally rejected (validator + `forbid_cookie_auth`); DBSC makes it pointless anyway |
| 6 | **Interface churn** silently breaking transports | High (undocumented `/tun/m/*`) | Medium | Capability probe + version pinning; sanctioned CLI absorbs churn; escape hatch version-gated |
| 7 | **Prompt-injection via kernel output** steering an agent | Medium | Medium | §3.1 outputs are inert/size-capped, never rendered or eval'd |
| 8 | **Runaway cost** on routed backends (Modal/Vertex per-second) | Medium | Medium | Per-backend spend caps + hard timeouts + guaranteed teardown/reconciliation |
| 9 | **Orphaned runtimes** burning CCUs and tripping abuse heuristics | Medium | Medium | `AssignmentLedger` reconciliation on startup + before assign; idle auto-release |

#### 5.2 Operational guardrails (defaults shipped on)

- Default backend is the **sanctioned official CLI**; escape hatch and any browser-bridge path require explicit enablement.
- `max_concurrent_assignments = 1` per account; raising it requires editing config and surfaces a warning.
- The tool **refuses** to operate on the free tier for any programmatic-control verb where the FAQ prohibition still applies; it checks for a positive compute balance and a Pro entitlement before using the escape hatch.
- On `DENYLISTED`, the tool stops, alerts the user (CLI message / MCP structured error / optional `PushNotification`), and does **not** retry — protecting the account is prioritized over completing the job.

#### 5.3 Recommended disclaimer (`colabctl/compliance/disclaimer.py`)

This text is shown at first run, on `auth enable-escape-hatch`, and is hashed into `EscapeHatchAck`. Verbatim canonical copy:

```
colabctl controls Google Colab on YOUR behalf, using YOUR Google account and YOUR
paid Colab Pro subscription.

1. NOT AFFILIATED WITH GOOGLE. colabctl is an independent, unofficial tool. It is
   not endorsed by, sponsored by, or affiliated with Google LLC.

2. YOU ARE RESPONSIBLE FOR ToS COMPLIANCE. Programmatic and headless control of
   paid Colab Pro (positive compute balance) is, per Google's current FAQ, NOT
   subject to the free-tier "no UI bypass / no remote control" prohibitions.
   colabctl is designed for that paid use case. The optional "escape hatch"
   transport talks to undocumented Google endpoints and may be read as access
   "other than by means authorized by Google"; it is OFF by default and you must
   explicitly enable it.

3. ACCOUNT-BAN RISK IS REAL AND OUTSIDE OUR CONTROL. Google operates opaque
   abuse-detection. Sustained automated GPU usage can trigger account blocks with
   NO published criteria and NO appeal guarantee. colabctl includes safeguards
   (conservative concurrency, backoff, denylist halting) to reduce this risk, but
   CANNOT eliminate it. Use a paid account you can afford to lose access to, and do
   NOT use this on an account whose suspension would also lock you out of Gmail,
   Drive, or other critical Google services you depend on.

4. NO MULTI-ACCOUNT CIRCUMVENTION. colabctl will not pool multiple accounts to beat
   quotas; doing so violates Google's ToS. Scale to sanctioned backends instead.

5. NO WARRANTY. Provided "as is", without warranty of any kind. The authors are not
   liable for account suspension, data loss, compute charges, or other damages.

By enabling the escape hatch you confirm you have a paid Colab Pro account with a
positive compute balance and you accept the above risks.
```

#### 5.4 Edge cases & failure handling specific to this section

- **First-access keychain prompt deadlock (headless):** if a keychain read blocks on an invisible GUI prompt (common after a Python upgrade or signature change), `KeyringBackend.get_blob` runs under a watchdog timeout; on timeout it raises `KeychainPromptTimeout` advising `COLABCTL_SECRET_BACKEND=age` for headless/CI. We never hang an agent indefinitely on a hidden dialog.
- **7-day refresh-token death (Testing-status OAuth):** detected as `AUTH_EXPIRED` with a refresh failure; the tool surfaces a clear "re-consent required" message rather than silently looping. The sanctioned-CLI path is preferred precisely because it avoids the self-registered-client 7-day treadmill.
- **Clock skew vs token expiry:** `StoredSecret.is_expired` uses a 60s skew margin so a slightly-skewed clock doesn't send a just-expired token (and trigger a spurious `AUTH_EXPIRED` storm).
- **Crash-loop after `DENYLISTED`:** the breaker state is persisted with `opened_at` + `cooldown_s`; on restart the tool reads it and refuses Colab traffic until the 24h window elapses, preventing a restart loop from deepening a ban.
- **Schema drift on the quota enum:** because `SUCCESS/DENYLISTED/QUOTA_*` are unverified, any unrecognized outcome string maps to `UNKNOWN` and is handled conservatively (capped retries, then route-away) — never an unguarded hard branch.
- **Cookie material smuggled into config:** the `StoredSecret` validator rejects it at write time; `redaction.scrub` redacts it at log time; together they make accidental full-account-credential handling fail loudly.

### 12.x Key decisions

- Treat the OS keychain strictly as defense-in-depth (no plaintext on disk, no git leaks), NOT a security boundary: design every credential as readable by any same-user process, and minimize blast radius via short-lived tokens and the per-file `drive.file` scope instead of full `auth/drive`.
- Structurally refuse to store or use full-account Google session cookies (pydantic validator + redaction + `forbid_cookie_auth`): cookie replay is ToS-prohibited, full-account blast radius, and defeated by DBSC in the ship window.
- Send the runtime-proxy token exactly once as the header-only `X-Colab-Runtime-Proxy-Token` (plus `X-Goog-Colab-Tunnel` and the `X-Goog-Colab-Token` XSRF), kept distinct from the separate OAuth Bearer identity token; never send it three ways.
- Make abuse-detection signals (`DENYLISTED`, `CHALLENGE`, `TooManyAssignmentsError`/412) terminal-no-retry with a persisted 24h per-account circuit-breaker cooldown, and route remaining work away via the provider abstraction rather than retrying into a permanent ban.
- Default to single-assignment, jittered ~60s keepalive, conservative rate-shaping, and proactive idle release; hard-refuse multi-account quota circumvention (FAQ-banned) and scale via Modal/Vertex instead.
- Gate the riskier direct `/tun/m/*` escape hatch behind an explicit, version-hashed user acknowledgement (`EscapeHatchAck`) that confirms a paid Pro account, and ship a frank no-affiliation/no-warranty/ban-risk disclaimer.
- Route agent-generated ('untrusted') code to gVisor-isolated Modal Sandboxes by default rather than the unsandboxed shared Colab VM, and treat all kernel outputs as inert, size-capped, never-rendered data to prevent prompt-injection-via-output.
- Classify backend responses on BOTH HTTP status and body markers with an `UNKNOWN`-conservative fallback, because the literal 412 binding and the SUCCESS/DENYLISTED/QUOTA_* enum are unverified and drift-prone.

### 12.y Section risks

- Opaque, no-appeal abuse-detection bans cannot be eliminated by any client-side safeguard; sustained headless GPU usage on a paid account remains the dominant residual risk, mitigated (concurrency=1, jitter, terminal-no-retry, route-away) but not removed.
- The header/XSRF auth recipe, 412 `TooManyAssignmentsError` binding, and SUCCESS/DENYLISTED/QUOTA_* enum are reverse-engineered/unverified against primary sources; Google can change them silently, so the escape-hatch detection logic may misclassify and must fail conservative.
- Keychain access can deadlock headless agents on first-access/binary-change GUI prompts and grants broad same-user read access after 'always allow'; the age-file backend mitigates headless deadlock but the at-rest-encryption benefit is modest on a single-user host.
- Self-registered OAuth clients suffer 7-day refresh-token death in Testing status and the colaboratory scope is not publicly grantable; the design leans on the sanctioned official CLI to avoid this, inheriting that CLI's own immaturity (v0.5.x, Python 3.13-only, yanked releases, no confirmed JSON mode).
- The escape-hatch ToS clause ('access other than authorized by Google') is a real, if low-on-paid-Pro, exposure; the consent gate and disclaimer reduce but do not remove legal/operational liability if Google tightens enforcement.
- Caller-side isolation cannot make the shared Colab VM a true sandbox for the user's own credentials/Drive mounts; only routing untrusted code to Modal provides gVisor-grade isolation, so a misconfigured 'force=True' on Colab reintroduces risk.

---

## 13. Testing & Quality Strategy

This section is the definitive specification for how `colabctl` is tested and kept production-grade. The product's defining technical reality dictates the entire strategy: the load-bearing surfaces are **fast-moving, undocumented, and abuse-detection-exposed** (the official `google-colab-cli` is `0.5.x` with yanked releases and Python-3.13-only; the `/tun/m/*` escape hatch is reverse-engineered; Colab can ban accounts opaquely). Therefore the testing pyramid is deliberately **inverted in trust**: the vast majority of confidence must come from **hermetic, deterministic tests that never touch Google** (unit, protocol-replay against a mock Jupyter/WebSocket server, and provider contract tests), with real-Colab integration relegated to an **opt-in, credential-gated, never-blocking** tier. The hostile dependency is contained behind the same stable interfaces the runtime architecture uses — so the test suite, like the product, **survives Google interface churn by routing around it**.

### Guiding Principles

1. **Hermetic by default.** `pytest` run with no environment variables and no network MUST pass the entire unit + protocol + contract tiers offline. Network egress is physically blocked in those tiers (see `pytest-socket`), so a forgotten real call fails loudly instead of silently flaking.
2. **Record once, replay forever.** Every undocumented wire contract (`/tun/m/*` JSON shapes, the XSSI `)]}'` prefix, `RuntimeProxyInfo`, kernel WebSocket frames, CLI stdout/stderr) is captured as a **sanitized, version-stamped fixture** and replayed deterministically. Fixtures are the executable specification of "what Google returned on date X".
3. **Adapters are the seam.** The `Transport` adapter (CLI subprocess), the kernel-exec client, the Drive client, and the keyring backend are all interfaces. Tests inject fakes at those seams. We never monkeypatch deep into `httpx`/`subprocess` internals from a test.
4. **The fragile dependency is fenced.** Tests that exercise the real CLI subprocess or the real `/tun/m/*` client are marked, gated, and excluded from the merge gate. They run in scheduled jobs and are allowed to fail without breaking `main`.
5. **Contract drift is a first-class signal, not a flake.** When a scheduled real-Colab job fails, it is triaged as **possible upstream churn** and feeds the capability-probe + fixture-refresh workflow, not silently retried.

### Test Taxonomy & Markers

All markers are declared in `pyproject.toml` under `[tool.pytest.ini_options].markers` and enforced with `--strict-markers`.

| Marker | Tier | Network? | Real creds? | Runs in PR gate? | Typical target |
|---|---|---|---|---|---|
| _(unmarked)_ | Unit | No (socket-blocked) | No | Yes (required) | Pure logic, pydantic models, header builders, parsers, capability matrix |
| `@pytest.mark.protocol` | Protocol/replay | Loopback only | No | Yes (required) | Kernel client vs mock Jupyter WS; CLI wrapper vs recorded stdout |
| `@pytest.mark.contract` | Provider contract | No | No | Yes (required) | Every backend adapter vs the abstract `Provider` ABC suite |
| `@pytest.mark.integration` | Integration | Yes (loopback + GCP sandbox) | Sanctioned only (Modal/Vertex test project) | No (nightly) | Modal Sandbox, Vertex CustomJob, Drive against a throwaway My Drive |
| `@pytest.mark.colab_live` | E2E real Colab | Yes (Google) | Real Colab Pro | No (manual/weekly, allow-fail) | `colab new --gpu T4`, run a cell, fetch output |
| `@pytest.mark.escape_hatch` | E2E `/tun/m/*` | Yes (Google) | Real Colab Pro | No (manual, allow-fail, opt-in) | Direct backend assign + proxy-token lifecycle |
| `@pytest.mark.slow` | Cross-cutting | — | — | No (nightly) | >5s tests, GPU allocation waits |

Default `addopts` collect only the hermetic tiers: `-m "not integration and not colab_live and not escape_hatch and not slow"`.

### Repository & Test Layout

```text
colabctl/
  src/colabctl/
    auth/                 # OAuth loopback, token store
    secrets/              # keyring + pluggable backends
    transport/
      cli_adapter.py      # PRIMARY: subprocess wrapper of google-colab-cli
      escape_hatch/       # OPT-IN: /tun/m/* client
        tun_client.py
        proxy_token.py
        kernel_exec.py    # jupyter-kernel-client wiring
    drive/                # user-OAuth plain-blob .ipynb sync
    providers/            # Provider ABC + Colab/Modal/Vertex/Kaggle/... impls
    models.py             # pydantic v2 models (shared)
    mcp_server.py
    cli.py                # Typer
tests/
  conftest.py             # global fixtures, socket-block, marker gating
  unit/
    test_models.py
    test_header_builder.py
    test_xssi_parser.py
    test_capability_matrix.py
    test_proxy_token_lifecycle.py
    test_secret_chunking.py
  protocol/
    conftest.py           # mock_jupyter_server, recorded_cli fixtures
    test_kernel_exec_replay.py
    test_cli_adapter_replay.py
    test_tun_client_replay.py
  contract/
    provider_contract.py  # the shared, parametrized ABC test suite
    test_colab_contract.py
    test_modal_contract.py
    test_vertex_contract.py
    test_kaggle_contract.py
  integration/
    test_modal_sandbox.py
    test_vertex_customjob.py
    test_drive_roundtrip.py
  e2e/
    test_colab_live_cli.py
    test_escape_hatch_live.py
  fixtures/
    cli/                  # recorded stdout/stderr + exit codes, JSON sidecars
      colab_new_t4.json
      colab_new_t4.stdout
      colab_status_too_many_assignments.json
    tun/                  # recorded /tun/m/* HTTP exchanges (sanitized)
      assign_success.json
      assign_412_too_many.json
      runtime_proxy_token.json
      assign_quota_denylisted.json
    ws/                   # recorded kernel WebSocket frame sequences
      execute_hello_world.jsonl
      execute_traceback.jsonl
      stream_stdout_chunks.jsonl
    drive/
      upload_create_ipynb.json
      upload_403_storage_quota.json
    schema/               # JSON Schemas the recordings are validated against
      runtime_proxy_info.schema.json
```

### Unit Tests

Unit tests cover pure logic with **zero I/O**. The highest-value targets are exactly the places where the adversarial verdicts caught the architecture being wrong, so they are pinned by tests that encode the corrected behavior.

#### Pydantic model round-trips and validation

Every wire model in `models.py` gets construction, validation-error, and serialization tests. Representative models under test:

```python
# src/colabctl/models.py
from enum import StrEnum
from pydantic import BaseModel, Field, HttpUrl, field_validator

class Accelerator(StrEnum):
    NONE = "NONE"
    T4 = "T4"
    L4 = "L4"
    A100 = "A100"
    H100 = "H100"
    V2_8 = "V2-8"

class AssignmentOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    QUOTA_DENIED = "QUOTA_DENIED"
    DENYLISTED = "DENYLISTED"
    TOO_MANY_ASSIGNMENTS = "TOO_MANY_ASSIGNMENTS"

class RuntimeProxyInfo(BaseModel):
    url: HttpUrl
    proxy_token: str = Field(min_length=1)          # HEADER-only credential
    token_expires_in_seconds: int = Field(gt=0)
    assigned_at_monotonic: float                    # set by client, not wire

    def expiry_deadline(self) -> float:
        return self.assigned_at_monotonic + self.token_expires_in_seconds

class Capability(StrEnum):
    LIVE_LOGS = "live_logs"
    POLL_THEN_FETCH = "poll_then_fetch"
    INTERACTIVE = "interactive"
    BATCH = "batch"
    PARAM_INJECTION = "param_injection"

class JobHandle(BaseModel):
    provider: str
    backend_job_id: str
    capabilities: frozenset[Capability]
```

Tests assert, for example, that `token_expires_in_seconds=0` raises `ValidationError`, that `proxy_token` is never logged (see redaction test below), and that `RuntimeProxyInfo.model_validate(json.loads(...))` succeeds against every recorded `tun/*.json` fixture.

#### Header builder — the corrected proxy-token auth recipe

The single most important unit test class encodes the verdict's correction: **the runtime-proxy token is a header-only credential, sent exactly once, distinct from the OAuth Bearer identity token, and never as a query param or Bearer.**

```python
# src/colabctl/transport/escape_hatch/kernel_exec.py
def build_proxy_headers(
    *, oauth_access_token: str, proxy_token: str, xsrf_token: str
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {oauth_access_token}",   # identity, distinct
        "X-Colab-Runtime-Proxy-Token": proxy_token,         # header-only auth
        "X-Goog-Colab-Tunnel": "true",
        "X-Goog-Colab-Token": xsrf_token,                   # XSRF
        "X-Goog-Colab-Client-Agent": CLIENT_AGENT,
    }
```

```python
# tests/unit/test_header_builder.py
def test_proxy_token_is_header_only_never_bearer():
    h = build_proxy_headers(oauth_access_token="oauth", proxy_token="PTOK", xsrf_token="X")
    # proxy token appears in exactly one header, never in Authorization, never as query
    assert h["X-Colab-Runtime-Proxy-Token"] == "PTOK"
    assert "PTOK" not in h["Authorization"]
    assert sum(1 for v in h.values() if "PTOK" in v) == 1

def test_oauth_and_proxy_token_are_distinct_credentials():
    h = build_proxy_headers(oauth_access_token="oauth", proxy_token="PTOK", xsrf_token="X")
    assert h["Authorization"] == "Bearer oauth"
    assert "oauth" not in h["X-Colab-Runtime-Proxy-Token"]

def test_required_tunnel_and_xsrf_headers_present():
    h = build_proxy_headers(oauth_access_token="o", proxy_token="p", xsrf_token="xsrf")
    assert h["X-Goog-Colab-Tunnel"] == "true"
    assert h["X-Goog-Colab-Token"] == "xsrf"
```

#### XSSI prefix parser

`/tun/m/*` responses are prefixed with `)]}'`. A dedicated parser is unit-tested against every recorded fixture and against malformed input.

```python
# tests/unit/test_xssi_parser.py
@pytest.mark.parametrize("fixture", list(Path("tests/fixtures/tun").glob("*.json")))
def test_xssi_strip_yields_valid_json(fixture):
    raw = fixture.read_bytes()
    obj = strip_xssi_and_parse(raw)
    assert isinstance(obj, dict)

def test_missing_xssi_prefix_is_tolerated():
    assert strip_xssi_and_parse(b'{"ok": true}') == {"ok": True}

def test_truncated_body_raises_transport_error():
    with pytest.raises(TransportProtocolError):
        strip_xssi_and_parse(b")]}'\n{ \"url\":")
```

#### Proxy-token lifecycle (deterministic clock)

The lifecycle manager refreshes the proxy token before `token_expires_in_seconds`, re-assigns on idle/lifetime termination, and surfaces `TooManyAssignmentsError`. Tested with an injected fake monotonic clock — **no `sleep`, no wall time.**

```python
# tests/unit/test_proxy_token_lifecycle.py
def test_refresh_fires_within_safety_margin(fake_clock, fake_tun):
    mgr = ProxyTokenManager(client=fake_tun, clock=fake_clock,
                            refresh_margin_s=30)
    mgr.attach(RuntimeProxyInfo(url=URL, proxy_token="A",
                                token_expires_in_seconds=120,
                                assigned_at_monotonic=fake_clock.now()))
    fake_clock.advance(91)            # within margin (120-30)
    mgr.tick()
    assert fake_tun.refresh_calls == 1
    assert mgr.current_token != "A"

def test_412_maps_to_too_many_assignments(fake_clock, fake_tun):
    fake_tun.queue_assign_response(load_fixture("tun/assign_412_too_many.json"), status=412)
    with pytest.raises(TooManyAssignmentsError):
        ProxyTokenManager(client=fake_tun, clock=fake_clock).assign(accelerator=Accelerator.T4)

def test_denylisted_outcome_is_terminal_not_retried(fake_clock, fake_tun):
    fake_tun.queue_assign_response(load_fixture("tun/assign_quota_denylisted.json"))
    with pytest.raises(AssignmentDenylistedError):
        ProxyTokenManager(client=fake_tun, clock=fake_clock).assign(accelerator=Accelerator.A100)
    assert fake_tun.assign_calls == 1   # no retry on a ban signal
```

> Note: the verdicts flag the literal `412` status binding and the `QUOTA_*` enum strings as **plausible-but-unverified**. The code reads them from a single `WireContract` table, and the lifecycle tests assert against that table, not scattered literals. When a scheduled live test reveals drift, the table changes in one place and these tests are re-recorded.

#### Secret storage: chunking & redaction

```python
# tests/unit/test_secret_chunking.py
def test_blob_over_4kb_is_chunked_across_keychain_items(fake_keyring):
    store = SecretStore(backend=fake_keyring, account_email="iris@analyticsandsociety.com")
    blob = b"x" * 9000
    store.set_blob("oauth_refresh", blob)
    assert len(fake_keyring.items_for("colabctl", prefix="oauth_refresh#")) == 3   # 4k chunks
    assert store.get_blob("oauth_refresh") == blob

def test_secrets_never_appear_in_repr_or_logs(caplog):
    info = RuntimeProxyInfo(url=URL, proxy_token="SUPERSECRET",
                            token_expires_in_seconds=60, assigned_at_monotonic=0.0)
    assert "SUPERSECRET" not in repr(info)
    logging.getLogger("colabctl").info("attached %s", info)
    assert "SUPERSECRET" not in caplog.text
```

A global `tests/conftest.py` autouse fixture scans `caplog.text` after every test and fails if any registered secret token leaks — defense-in-depth against credential logging.

### Protocol-Level Tests (Recorded Fixtures + Mock Servers)

These are the **trust core** of the suite. They prove the kernel client and CLI wrapper behave correctly against the exact bytes Google produced, without ever calling Google.

#### Mock Jupyter / WebSocket server

A real in-process `websockets` server (bound to `127.0.0.1:0`) replays recorded kernel-protocol frame sequences. This exercises the actual `jupyter-kernel-client` codepath (subprotocol negotiation, header injection, frame parsing) rather than a mock of it.

```python
# tests/protocol/conftest.py
import asyncio, json, websockets, pytest

class RecordedKernelServer:
    """Replays a .jsonl of Jupyter wire-protocol frames over a loopback WS."""
    def __init__(self, frames: list[dict]):
        self._frames = frames
        self.received: list[dict] = []
        self.observed_headers: dict[str, str] = {}

    async def _handler(self, ws):
        self.observed_headers = dict(ws.request.headers)
        async for raw in ws:                       # client sends execute_request
            self.received.append(json.loads(raw))
            for frame in self._frames:             # server streams recorded replies
                await ws.send(json.dumps(frame))

    @classmethod
    async def serve(cls, frames):
        srv = cls(frames)
        server = await websockets.serve(srv._handler, "127.0.0.1", 0)
        host, port = server.sockets[0].getsockname()[:2]
        return srv, server, f"ws://{host}:{port}"

@pytest.fixture
def load_ws_frames():
    def _load(name: str) -> list[dict]:
        path = Path(__file__).parent.parent / "fixtures" / "ws" / name
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return _load
```

```python
# tests/protocol/test_kernel_exec_replay.py
@pytest.mark.protocol
async def test_execute_captures_stream_and_result(load_ws_frames):
    frames = load_ws_frames("execute_hello_world.jsonl")
    srv, server, url = await RecordedKernelServer.serve(frames)
    try:
        client = KernelExecClient(ws_url=url, proxy_token="PTOK",
                                  oauth_access_token="oauth", xsrf_token="X")
        result = await client.execute("print('hello')", timeout_s=5)
        assert result.stdout == "hello\n"
        assert result.status == "ok"
        # PROVES the corrected auth recipe on the real wire:
        assert srv.observed_headers["X-Colab-Runtime-Proxy-Token"] == "PTOK"
        assert srv.observed_headers["Authorization"] == "Bearer oauth"
        assert "PTOK" not in srv.observed_headers["Authorization"]
    finally:
        server.close(); await server.wait_closed()

@pytest.mark.protocol
async def test_execute_surfaces_traceback_as_structured_error(load_ws_frames):
    frames = load_ws_frames("execute_traceback.jsonl")
    srv, server, url = await RecordedKernelServer.serve(frames)
    try:
        client = KernelExecClient(ws_url=url, proxy_token="p",
                                  oauth_access_token="o", xsrf_token="x")
        result = await client.execute("1/0", timeout_s=5)
        assert result.status == "error"
        assert result.ename == "ZeroDivisionError"
        assert "division by zero" in result.evalue
    finally:
        server.close(); await server.wait_closed()

@pytest.mark.protocol
async def test_dropped_socket_during_long_cell_raises_not_hangs(load_ws_frames):
    # jupyter/notebook #4757 class: socket dies, UI thinks kernel alive.
    frames = load_ws_frames("stream_stdout_chunks.jsonl")[:1]  # then server closes
    srv, server, url = await RecordedKernelServer.serve(frames)
    client = KernelExecClient(ws_url=url, proxy_token="p",
                              oauth_access_token="o", xsrf_token="x")
    task = asyncio.create_task(client.execute("train()", timeout_s=2))
    server.close(); await server.wait_closed()
    with pytest.raises(KernelConnectionLost):
        await task
```

#### CLI subprocess wrapper replay

The PRIMARY transport shells out to `google-colab-cli`. Because the verdict flags **no confirmed stable JSON mode**, the wrapper must tolerate both a `--json` path and human-readable stdout. We test against recorded `(argv, stdout, stderr, exit_code)` tuples via an injectable `Runner` interface — `subprocess` is never invoked in this tier.

```python
# src/colabctl/transport/cli_adapter.py
class Runner(Protocol):
    async def run(self, argv: list[str], *, timeout_s: float) -> CompletedProc: ...

class ColabCliAdapter:
    def __init__(self, runner: Runner, prefer_json: bool = True): ...
    async def new_runtime(self, accelerator: Accelerator) -> JobHandle: ...
    async def status(self, handle: JobHandle) -> RuntimeStatus: ...
```

```python
# tests/protocol/test_cli_adapter_replay.py
class ReplayRunner:
    def __init__(self, table: dict[tuple[str, ...], CompletedProc]):
        self._table = table; self.calls: list[list[str]] = []
    async def run(self, argv, *, timeout_s):
        self.calls.append(argv)
        return self._table[tuple(argv[1:])]   # key on args after the binary

@pytest.mark.protocol
async def test_new_t4_parses_handle_from_recorded_stdout():
    proc = load_cli_fixture("colab_new_t4")   # stdout + sidecar JSON + exit 0
    runner = ReplayRunner({("new", "--gpu", "T4", "--json"): proc})
    adapter = ColabCliAdapter(runner, prefer_json=True)
    handle = await adapter.new_runtime(Accelerator.T4)
    assert handle.provider == "colab"
    assert handle.backend_job_id

@pytest.mark.protocol
async def test_falls_back_to_human_stdout_when_json_unsupported():
    # CLI rejects --json (unknown flag); wrapper retries without it and scrapes stdout.
    err = CompletedProc(stdout="", stderr="error: unknown option --json", exit_code=2)
    ok = load_cli_fixture("colab_new_t4_human")  # human-readable table
    runner = ReplayRunner({("new", "--gpu", "T4", "--json"): err,
                           ("new", "--gpu", "T4"): ok})
    adapter = ColabCliAdapter(runner, prefer_json=True)
    handle = await adapter.new_runtime(Accelerator.T4)
    assert handle.backend_job_id
    assert runner.calls == [["colab", "new", "--gpu", "T4", "--json"],
                            ["colab", "new", "--gpu", "T4"]]

@pytest.mark.protocol
async def test_too_many_assignments_exit_maps_to_typed_error():
    proc = load_cli_fixture("colab_status_too_many_assignments")  # nonzero exit
    runner = ReplayRunner({("status", "JOB",): proc})
    adapter = ColabCliAdapter(runner)
    with pytest.raises(TooManyAssignmentsError):
        await adapter.status(JobHandle(provider="colab", backend_job_id="JOB",
                                       capabilities=frozenset()))
```

#### HTTP replay for `/tun/m/*` (escape hatch)

`respx` intercepts `httpx` to replay recorded `/tun/m/assign`, `/runtime-proxy-token`, and `/assignments` exchanges including the XSSI prefix and the 412 path. This proves the escape-hatch client without ever risking a real account.

```python
# tests/protocol/test_tun_client_replay.py
@pytest.mark.protocol
async def test_assign_success_parses_runtime_proxy_info(respx_mock):
    body = Path("tests/fixtures/tun/assign_success.json").read_bytes()
    respx_mock.post("https://colab.research.google.com/tun/m/assign").respond(
        200, content=body, headers={"content-type": "application/json"})
    client = TunClient(oauth_access_token="o", xsrf_token="x")
    info = await client.assign(accelerator=Accelerator.T4, machine_shape="HIGH_RAM")
    assert isinstance(info, RuntimeProxyInfo)
    assert info.token_expires_in_seconds > 0

@pytest.mark.protocol
async def test_request_omits_proxy_token_no_query_param(respx_mock):
    route = respx_mock.post("https://colab.research.google.com/tun/m/assign").respond(
        200, content=Path("tests/fixtures/tun/assign_success.json").read_bytes())
    await TunClient(oauth_access_token="o", xsrf_token="x").assign(accelerator=Accelerator.T4)
    req = route.calls.last.request
    assert "proxy" not in str(req.url).lower()          # no token in query string
    assert req.headers["X-Goog-Colab-Tunnel"] == "true"
```

#### Fixture provenance & schema validation

Every recorded fixture carries a sidecar metadata block (`_meta`) with `captured_at`, `cli_version` or `endpoint`, `sanitized: true`, and a `schema` pointer. A meta-test enforces hygiene:

```python
# tests/protocol/test_fixture_hygiene.py
@pytest.mark.protocol
@pytest.mark.parametrize("f", iter_all_json_fixtures())
def test_fixtures_are_sanitized_and_schema_valid(f):
    obj = json.loads(f.read_text())
    assert obj["_meta"]["sanitized"] is True
    assert REDACTED_TOKEN_PATTERN.search(f.read_text()) is None  # no real tokens
    jsonschema.validate(obj["body"], load_schema(obj["_meta"]["schema"]))
    # staleness warning (not failure): fixtures > 90d old emit a warning
    if older_than_days(obj["_meta"]["captured_at"], 90):
        warnings.warn(f"Fixture {f.name} is stale; consider re-recording", StaleFixtureWarning)
```

A recorder utility (`scripts/record_fixtures.py`, run manually with real creds, never in CI) captures fresh exchanges and **auto-redacts** OAuth tokens, proxy tokens, cookies, and email via a deny-list of regexes before writing. The redaction step itself is unit-tested.

### Provider Contract Tests

The provider abstraction is the architecture's highest-rated decision; the contract suite is what keeps every backend honest against it. A single parametrized suite (`tests/contract/provider_contract.py`) is run against **every** registered provider via a fake/sandbox driver, asserting the `submit/status/logs/fetch/cancel` semantics and that declared capabilities match observed behavior.

```python
# src/colabctl/providers/base.py
class Provider(ABC):
    name: str
    @abstractmethod
    def capabilities(self) -> frozenset[Capability]: ...
    @abstractmethod
    async def submit(self, spec: JobSpec) -> JobHandle: ...
    @abstractmethod
    async def status(self, h: JobHandle) -> JobStatus: ...
    @abstractmethod
    async def logs(self, h: JobHandle) -> AsyncIterator[LogLine]: ...
    @abstractmethod
    async def fetch(self, h: JobHandle) -> JobResult: ...
    @abstractmethod
    async def cancel(self, h: JobHandle) -> None: ...
```

```python
# tests/contract/provider_contract.py
class ProviderContractSuite:
    """Subclass per provider; supply make_provider() returning a fake-backed instance."""
    def make_provider(self) -> Provider: raise NotImplementedError

    @pytest.mark.contract
    async def test_submit_returns_handle_with_declared_capabilities(self):
        p = self.make_provider()
        h = await p.submit(JobSpec(code="print(1)", accelerator=Accelerator.T4))
        assert h.provider == p.name
        assert h.capabilities == p.capabilities()

    @pytest.mark.contract
    async def test_status_is_monotonic_to_terminal(self):
        p = self.make_provider()
        h = await p.submit(JobSpec(code="print(1)"))
        seen = []
        for _ in range(20):
            s = await p.status(h); seen.append(s.state)
            if s.state in TERMINAL_STATES: break
        assert seen[-1] in TERMINAL_STATES
        assert no_backwards_transition(seen)   # QUEUED->RUNNING->SUCCEEDED, never back

    @pytest.mark.contract
    async def test_logs_capability_matches_behavior(self):
        p = self.make_provider()
        h = await p.submit(JobSpec(code="print('x')"))
        if Capability.LIVE_LOGS in p.capabilities():
            lines = [l async for l in p.logs(h)]      # must stream before terminal
            assert lines
        else:
            # poll-then-fetch providers must raise a typed, documented error
            with pytest.raises(LiveLogsUnsupported):
                async for _ in p.logs(h): pass

    @pytest.mark.contract
    async def test_cancel_is_idempotent(self):
        p = self.make_provider()
        h = await p.submit(JobSpec(code="while True: pass"))
        await p.cancel(h)
        await p.cancel(h)            # second cancel must not raise
        assert (await p.status(h)).state in {"CANCELLED", "SUCCEEDED", "FAILED"}

    @pytest.mark.contract
    async def test_fetch_after_failure_returns_result_not_exception(self):
        p = self.make_provider()
        h = await p.submit(JobSpec(code="1/0"))
        await drain_to_terminal(p, h)
        res = await p.fetch(h)
        assert res.exit_state == "FAILED"
        assert res.error is not None     # captured, not raised
```

```python
# tests/contract/test_kaggle_contract.py
class TestKaggleContract(ProviderContractSuite):
    def make_provider(self): return KaggleProvider(client=FakeKaggleClient())
    # Inherits all contract tests; capability matrix declares POLL_THEN_FETCH + BATCH,
    # so test_logs_capability_matches_behavior asserts LiveLogsUnsupported is raised.
```

This is where the verdict's hard truth — Kaggle has **no live logs** (issue #653 open), Colab consumer has **no log-streaming contract**, Vertex/Modal **do** stream — is encoded as enforced contract, so the MCP/CLI surfaces can branch on `capabilities()` with confidence.

#### Capability matrix snapshot test

```python
# tests/unit/test_capability_matrix.py
EXPECTED = {
    "colab":   {Capability.INTERACTIVE},                      # no live-log contract
    "modal":   {Capability.LIVE_LOGS, Capability.INTERACTIVE, Capability.BATCH},
    "vertex":  {Capability.LIVE_LOGS, Capability.BATCH},
    "kaggle":  {Capability.POLL_THEN_FETCH, Capability.BATCH},
    "hf_jobs": {Capability.LIVE_LOGS, Capability.BATCH},
}
def test_registered_capabilities_match_snapshot():
    actual = {name: set(p.capabilities()) for name, p in registry().items()}
    assert actual == EXPECTED   # changing a backend's capabilities is a deliberate diff
```

### Integration Tests (Sanctioned Backends)

`@pytest.mark.integration` tests hit **only sanctioned, ToS-clean** backends where automation is the intended use: Modal Sandboxes, Vertex CustomJob, and Google Drive against a disposable My Drive folder. They are gated by credentials, run nightly, and **never** in the PR merge gate. Because they cost money and GPU time, they default to the cheapest tier (Modal CPU/T4, Vertex T4) with hard spend/timeouts.

Gating pattern (a fixture, not scattered `skipif`):

```python
# tests/integration/conftest.py
@pytest.fixture
def modal_creds():
    tok_id = os.environ.get("MODAL_TOKEN_ID")
    tok_secret = os.environ.get("MODAL_TOKEN_SECRET")
    if not (tok_id and tok_secret):
        pytest.skip("Modal creds absent — integration tier skipped (expected in PR CI)")
    return tok_id, tok_secret
```

```python
# tests/integration/test_drive_roundtrip.py
@pytest.mark.integration
async def test_ipynb_uploads_as_plain_blob_to_my_drive(drive_oauth, tmp_drive_folder):
    # Encodes the verdict correction: USER-OAuth, plain-blob, My Drive ownership.
    client = DriveClient(creds=drive_oauth)
    nb = make_minimal_ipynb()
    file_id = await client.upload_ipynb(nb, parent=tmp_drive_folder, name="rt.ipynb")
    meta = await client.get_metadata(file_id)
    assert meta.owners[0].email_address == drive_oauth.user_email   # human owns it
    assert meta.mime_type != "application/vnd.google.colaboratory"  # plain blob, not native
    fetched = await client.download_ipynb(file_id)
    assert fetched == nb
    await client.delete(file_id)   # teardown — no orphaned files

@pytest.mark.integration
async def test_service_account_upload_is_rejected_by_design():
    # Guards against regressing into the 403 storageQuotaExceeded trap.
    with pytest.raises(ServiceAccountOwnershipForbidden):
        DriveClient.from_service_account(SA_INFO)
```

A nightly **billing watchdog** test asserts every integration test tears down its resources (Modal sandboxes terminated, Vertex jobs cancelled, Drive files deleted); a reconciliation pass lists leftover resources tagged `colabctl-ci` and fails the job if any survive.

### E2E Tests Against Real Colab (Gated & Allow-Fail)

The `colab_live` and `escape_hatch` tiers are the only tests that touch a real Colab Pro account. They are **opt-in, credential-gated, manual or weekly-scheduled, and explicitly allowed to fail without breaking `main`** — because their failure is expected churn/abuse-detection signal, not a code defect.

```python
# tests/e2e/conftest.py
@pytest.fixture(scope="session")
def colab_live_enabled():
    if os.environ.get("COLABCTL_E2E_COLAB") != "1":
        pytest.skip("Real-Colab E2E disabled (set COLABCTL_E2E_COLAB=1 + creds)")
    if not Path(os.environ.get("COLABCTL_E2E_TOKEN_FILE", "")).exists():
        pytest.skip("No live OAuth token file present")
    return True

@pytest.fixture(scope="session")
def escape_hatch_enabled(colab_live_enabled):
    # Double opt-in: the disclosed-risk path requires its own explicit flag.
    if os.environ.get("COLABCTL_E2E_ESCAPE_HATCH") != "1":
        pytest.skip("Escape-hatch E2E disabled (disclosed-risk path, set =1 to accept)")
    return True
```

```python
# tests/e2e/test_colab_live_cli.py
@pytest.mark.colab_live
@pytest.mark.slow
async def test_allocate_t4_run_cell_fetch_output(colab_live_enabled):
    async with allocate_runtime(Accelerator.T4) as rt:     # auto-stops in finally
        result = await rt.execute("import torch; print(torch.cuda.is_available())")
        assert result.status == "ok"
        assert result.stdout.strip() in {"True", "False"}  # GPU may be CPU-fallback
    # context manager MUST stop the session (COLAB_SKILL: idle VMs burn units)
```

E2E runs always finalize by stopping/unassigning the runtime, even on assertion failure, to avoid burning compute units and to minimize the abuse-detection footprint. They run **single-account only** — the suite never spins up multiple accounts, since that is explicitly ToS-prohibited.

**Drift detection role:** when the weekly `colab_live` job fails, a triage step diffs the live responses against the committed fixtures. If the wire shape changed (new field, renamed enum, status code drift), it opens a `contract-drift` issue and attaches the captured (sanitized) response as a candidate fixture refresh. This is how the hermetic tiers stay faithful to a moving target.

### Type Checking

Both **mypy** and **pyright** run in strict mode in CI. mypy is the gate; pyright is an advisory second opinion (it catches different narrowing/overload issues and validates the same surface the IDE uses). Strict typing is non-negotiable because the wire models are the spec.

```toml
# pyproject.toml
[tool.mypy]
python_version = "3.11"
strict = true
warn_unreachable = true
warn_redundant_casts = true
disallow_any_explicit = true
plugins = ["pydantic.mypy"]
files = ["src", "tests"]

[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true

[[tool.mypy.overrides]]
module = ["jupyter_kernel_client.*", "kaggle.*", "modal.*", "runpod.*", "vastai.*"]
ignore_missing_imports = true   # third-party SDKs without stubs

[tool.pyright]
typeCheckingMode = "strict"
pythonVersion = "3.11"
include = ["src", "tests"]
reportMissingTypeStubs = false
```

Tests are type-checked too (catching wrong fixture shapes early). The `ColabCliAdapter` subprocess boundary returns a typed `CompletedProc`, never raw `subprocess.CompletedProcess`, so stdout parsing stays inside typed code.

### Linting & Formatting (Ruff)

Ruff is the single tool for both lint and format (replacing black/isort/flake8). Run in CI with `--no-fix --diff` to gate, and locally via pre-commit with autofix.

```toml
# pyproject.toml
[tool.ruff]
target-version = "py311"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "W", "I", "N", "UP", "B", "C4", "SIM", "TID", "PTH",
          "RUF", "ASYNC", "S", "PT", "ANN", "LOG", "G"]
ignore = ["ANN401"]   # allow typed **kwargs Any in narrow adapter shims

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["S101", "ANN201", "S105", "S106"]   # asserts + dummy creds OK in tests

[tool.ruff.lint.flake8-bandit]
# S-rules catch hardcoded secrets; we still hard-fail on real-looking tokens via
# a custom pre-commit secret scanner (detect-secrets) below.
```

Notable enabled rule groups for this codebase: `ASYNC` (no blocking calls in async transport), `S`/bandit (no hardcoded credentials — critical given the token-handling surface), `LOG`/`G` (logging hygiene so secrets are not f-string-interpolated into log records), `PTH` (pathlib over os.path for fixture handling).

### Pre-commit Hooks

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.9.x
    hooks:
      - id: ruff           # lint + autofix
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.x
    hooks:
      - id: mypy
        additional_dependencies: [pydantic>=2, types-requests]
        args: [--strict]
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.x
    hooks:
      - id: detect-secrets
        args: ["--baseline", ".secrets.baseline"]
        exclude: tests/fixtures/   # fixtures are pre-sanitized; baseline tracks them
  - repo: local
    hooks:
      - id: fixture-sanitization
        name: assert recorded fixtures contain no live tokens
        entry: python scripts/check_fixture_sanitization.py
        language: python
        files: ^tests/fixtures/
      - id: no-marker-leak
        name: forbid integration/colab_live/escape_hatch tests without their marker
        entry: python scripts/check_test_markers.py
        language: python
        files: ^tests/(integration|e2e)/
      - id: pytest-hermetic
        name: fast hermetic unit+protocol+contract tests
        entry: pytest -m "not integration and not colab_live and not escape_hatch and not slow" -q
        language: system
        pass_filenames: false
        stages: [pre-push]   # full hermetic suite only on push, not every commit
```

The `fixture-sanitization` local hook is load-bearing: it is the last line of defense preventing a real OAuth/proxy token or cookie from being committed inside a recorded fixture.

### Coverage Targets

Coverage is measured with `coverage.py` (via `pytest-cov`) over the **hermetic tiers only** — integration/e2e are excluded because they are non-deterministic and would inflate or destabilize numbers.

```toml
# pyproject.toml
[tool.coverage.run]
branch = true
source = ["src/colabctl"]
omit = ["src/colabctl/mcp_server.py"]   # thin glue, covered by contract+manual

[tool.coverage.report]
fail_under = 90
show_missing = true
exclude_lines = ["pragma: no cover", "raise NotImplementedError",
                 "if TYPE_CHECKING:", "\\.\\.\\."]
```

| Module class | Target | Rationale |
|---|---|---|
| `models.py`, header/XSSI/parser logic | 100% | Pure logic, the executable spec; no excuse for gaps |
| `transport/escape_hatch/*` (proxy-token lifecycle, tun client) | 95%+ | Highest fragility; every error/branch must be exercised by replay |
| `transport/cli_adapter.py` | 90%+ | Both JSON and human-stdout fallback paths covered |
| `providers/*` | 90%+ | Enforced by the shared contract suite |
| `secrets/*`, `drive/*` | 90%+ | Security/correctness sensitive |
| Project gate (`fail_under`) | **90%** | Hard CI gate; PR fails below it |

Coverage diff (via `diff-cover` on the PR) requires **new/changed lines to be ≥ 95% covered**, which is stricter than the project floor and prevents erosion.

### CI Pipeline (GitHub Actions)

Two key constraints shape the pipeline: (1) the core package targets **Python 3.11+**, but `google-colab-cli` is **Python 3.13-only and invoked as a pinned, isolated `uv tool` subprocess** — so the matrix tests the core on 3.11/3.12/3.13 while the CLI integration installs the CLI into its own ephemeral 3.13 env; (2) the merge gate must be **fully hermetic and free** — no Google, no Modal, no GCP creds.

```yaml
# .github/workflows/ci.yml
name: ci
on:
  pull_request:
  push: { branches: [main] }

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

jobs:
  lint-type:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv python install 3.11
      - run: uv sync --all-extras --dev
      - run: uv run ruff check --no-fix --output-format=github .
      - run: uv run ruff format --check .
      - run: uv run mypy
      - run: uv run pyright          # advisory; continue-on-error: false (kept strict)
      - run: uv run python scripts/check_fixture_sanitization.py

  hermetic-tests:
    needs: lint-type
    runs-on: ${{ matrix.os }}
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv python install ${{ matrix.python }}
      - run: uv sync --all-extras --dev
      - name: Hermetic suite (unit + protocol + contract), network blocked
        run: >
          uv run pytest
          -m "not integration and not colab_live and not escape_hatch and not slow"
          --disable-socket --allow-unix-socket
          --cov --cov-report=xml --cov-fail-under=90 -q
      - uses: codecov/codecov-action@v4
        with: { files: coverage.xml }

  diff-coverage:
    needs: hermetic-tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --all-extras --dev
      - run: uv run pytest -m "not integration and not colab_live and not escape_hatch and not slow" --cov --cov-report=xml -q
      - run: uv run diff-cover coverage.xml --compare-branch=origin/main --fail-under=95

  cli-interop:
    # Verifies the 3.13-only google-colab-cli installs in an isolated tool env and
    # that the wrapper's capability probe runs WITHOUT real auth (probe = --version/--help).
    needs: lint-type
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv python install 3.11 3.13
      - run: uv sync --dev
      - name: Install pinned CLI into isolated 3.13 env
        run: uv tool install --python 3.13 "google-colab-cli==${{ vars.COLAB_CLI_PIN }}"
      - name: Capability probe (no creds; asserts adapter handshakes with real binary)
        run: uv run pytest tests/protocol/test_cli_capability_probe.py -q
```

Scheduled / manual jobs in a **separate workflow** (`.github/workflows/live.yml`) so they never gate PRs:

```yaml
# .github/workflows/live.yml
name: live-and-integration
on:
  schedule: [{ cron: "0 6 * * 1" }]   # weekly, Monday 06:00 UTC
  workflow_dispatch:
    inputs:
      enable_escape_hatch: { type: boolean, default: false }

jobs:
  integration:                      # sanctioned backends, nightly-grade
    runs-on: ubuntu-latest
    environment: integration        # protected env; holds Modal/Vertex test creds
    continue-on-error: true         # never blocks main
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --all-extras --dev
      - env:
          MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
          MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
          GOOGLE_APPLICATION_CREDENTIALS: ${{ runner.temp }}/sa.json
        run: uv run pytest -m "integration" -q
      - run: uv run python scripts/reconcile_orphans.py   # billing watchdog

  colab-live:                       # real Colab Pro; drift detection
    runs-on: ubuntu-latest
    environment: colab-live         # holds the live OAuth token file
    continue-on-error: true
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --all-extras --dev
      - env:
          COLABCTL_E2E_COLAB: "1"
          COLABCTL_E2E_TOKEN_FILE: ${{ runner.temp }}/token.json
          COLABCTL_E2E_ESCAPE_HATCH: ${{ github.event.inputs.enable_escape_hatch && '1' || '0' }}
        run: uv run pytest -m "colab_live or escape_hatch" -q || true
      - if: failure()
        run: uv run python scripts/triage_contract_drift.py   # opens drift issue
```

**Branch protection** requires `lint-type`, `hermetic-tests` (all matrix legs), `diff-coverage`, and `cli-interop` to pass. The `live-and-integration` workflow is intentionally **not** a required check.

### Edge Cases & Failure Handling Specific to Testing

- **No stable JSON mode in the CLI:** covered by `test_falls_back_to_human_stdout_when_json_unsupported`. The wrapper probes `--json`, falls back to stdout scraping, and a protocol test pins both code paths. If a CLI version changes its table layout, the human-stdout fixture diff fails loudly.
- **Yanked / version-drifting CLI releases:** the CLI is pinned via `vars.COLAB_CLI_PIN`; `cli-interop` installs that exact version. A Renovate/Dependabot PR bumping the pin re-runs `cli-interop` so a yanked or breaking release is caught before merge.
- **Python 3.13-only dependency vs 3.11 core floor:** the matrix proves the core imports and the hermetic suite passes on 3.11/3.12/3.13; `cli-interop` proves the isolated-3.13 subprocess invocation works. The core never imports the CLI's Python package — only shells out — so the floor is preserved and unit-tested via the `Runner` seam.
- **Proxy-token expiry mid-execution:** simulated in `test_refresh_fires_within_safety_margin` with a fake clock; the kernel-exec protocol test pairs a long stream with a token refresh to assert no execution interruption.
- **`412 TooManyAssignmentsError` and `DENYLISTED`:** recorded fixtures + lifecycle unit tests assert correct typed exceptions and **no retry** on ban signals (retrying a denylist is exactly what trips deeper abuse detection).
- **Dropped WebSocket / silent kernel stall:** `test_dropped_socket_during_long_cell_raises_not_hangs` proves we raise `KernelConnectionLost` with a timeout rather than hanging — the jupyter/notebook #4757 failure class.
- **Drive 403 storageQuotaExceeded regression guard:** `test_service_account_upload_is_rejected_by_design` makes the verdict's hard rule (no SA owning native files) a failing test if anyone reintroduces a service-account upload path.
- **Secret leakage in logs/reprs:** autouse `caplog` scanner + pydantic `repr` redaction tests + detect-secrets + the fixture-sanitization hook form four independent layers.
- **Real-Colab abuse-detection / non-determinism:** quarantined behind `continue-on-error: true`, single-account only, always-stop teardown; failures become drift-triage tasks, never red `main`.
- **Capability lies:** the contract suite fails any provider whose declared `capabilities()` does not match observed log-streaming/interactive behavior, preventing the MCP/CLI from branching on a false capability.
- **Flaky-network masquerading as logic failure:** impossible in the gate because `--disable-socket` blocks all non-loopback I/O; any accidental real call surfaces as an explicit `SocketBlockedError`, not an intermittent flake.

### 13.x Key decisions

- Hermetic-by-default test gate: unit + protocol-replay + provider-contract tiers run with network physically blocked (pytest-socket --disable-socket) and form the only required PR checks; no test that touches Google, Modal, or GCP can gate a merge.
- All undocumented wire contracts (/tun/m/* JSON with XSSI prefix, RuntimeProxyInfo, kernel WebSocket frames, CLI stdout/stderr+exit codes) are captured as sanitized, version-stamped, schema-validated fixtures and replayed deterministically against a real in-process loopback mock Jupyter/WebSocket server (websockets) and respx HTTP intercepts.
- The corrected proxy-token auth recipe is pinned by dedicated tests: X-Colab-Runtime-Proxy-Token is header-only, sent exactly once, distinct from the OAuth Bearer identity token, never as Bearer or query param, accompanied by X-Goog-Colab-Tunnel and X-Goog-Colab-Token XSRF headers.
- A single parametrized provider contract suite (submit/status/logs/fetch/cancel + capability feature-detection) runs against every backend, enforcing that declared capabilities (Colab=interactive-only, Kaggle=poll-then-fetch, Modal/Vertex/HF=live-logs) match observed behavior.
- Real-Colab E2E (colab_live) and the /tun/m/* escape hatch (escape_hatch) are double-opt-in, credential-gated, single-account, always-teardown, weekly-scheduled, and continue-on-error: their failures feed a contract-drift triage workflow rather than breaking main.
- Strict typing with both mypy (gate) and pyright (advisory) plus the pydantic mypy plugin; the fast-moving google-colab-cli is invoked only through a typed Runner subprocess seam so the 3.11+ core floor is preserved while the CLI runs in an isolated uv tool 3.13 env.
- Ruff for lint+format with security (S/bandit), async-blocking (ASYNC), and logging-hygiene (LOG/G) rule groups enabled; four independent layers (caplog scanner, pydantic repr redaction, detect-secrets, fixture-sanitization hook) prevent credential leakage into logs or committed fixtures.
- Coverage measured over hermetic tiers only with a 90% project gate, 100% on pure-logic/model code, 95%+ on the fragile escape-hatch lifecycle, and a stricter 95% diff-coverage requirement on changed lines.
- CI splits into a required hermetic workflow (lint-type, 3-OS x 3.11/3.12/3.13 matrix hermetic tests, diff-coverage, cli-interop capability probe) and a non-required scheduled live-and-integration workflow holding all real credentials in protected GitHub environments.
- Regression guards encode the adversarial-verdict corrections as failing tests: no service-account Drive uploads of native-MIME files (403 storageQuotaExceeded trap), no retry on DENYLISTED/412 ban signals, and raise-not-hang on dropped kernel WebSockets.

### 13.y Section risks

- Recorded fixtures are point-in-time snapshots of undocumented Google surfaces; they can silently diverge from reality, giving green hermetic tests while production breaks. Mitigated by weekly colab_live drift detection and fixture staleness warnings, but there is an inherent lag between Google changing a contract and the fixture being refreshed.
- The required cli-interop job depends on installing a pinned, 3.13-only, frequently-yanked google-colab-cli from PyPI; a yanked pin or PyPI/network outage can make a required check fail for reasons unrelated to the code under test, potentially blocking unrelated PRs until the pin is bumped.
- Real-Colab and escape-hatch E2E run against a live Colab Pro account and can trigger opaque abuse-detection bans or compute-unit burn on the CI test account; even with single-account, always-teardown, and weekly cadence, the test account itself is an at-risk shared asset with no appeal SLA.
- Strict mypy+pyright over third-party SDKs (modal, kaggle, runpod, vastai, jupyter-kernel-client) without stubs forces ignore_missing_imports overrides, creating untyped seams where wrong-shape data can pass type checking and only be caught by contract/protocol tests.
- The mock Jupyter/WebSocket server and respx replays validate that the client correctly parses recorded responses, but cannot validate that Colab still produces those responses or still accepts the exact header set; auth-recipe correctness is only truly confirmed by the allow-fail live tier.
- Integration tests against Modal/Vertex incur real cost and depend on a billing watchdog/reconciliation script to clean up; a watchdog bug or a job crashing before teardown can leak paid GPU resources, and the cleanup itself is hard to test deterministically.
- Provider contract tests run against fakes/sandboxes, so a backend can pass the contract suite in CI while its real SDK drifts (RunPod REST v2, Kaggle CLI, HF Jobs, Vertex SDK all evolve independently); contract conformance is necessary but not sufficient for real-backend correctness.

---

## 14. Repository Structure, Packaging & Config

This section is the authoritative layout for the package. It is a **src-layout, uv-managed, single-distribution** Python package that ships three console entry points (a Typer CLI, an MCP server, and an internal `colabctl-cli-shim` used only to invoke the official `google-colab-cli` in an isolated interpreter). The package name is provisional (`colabctl`); the import package is `colabctl` and is the single rename point.

> **Architecture invariants this layout enforces** (from the chosen design):
> 1. The official `google-colab-cli` (Python **3.13-only**, v0.5.x, yanked releases) is **never** imported into our process. It runs as a **pinned, version-gated subprocess** behind an adapter. Our core floor stays at **Python 3.11+** (see `transport/official_cli/`).
> 2. The **provider abstraction** (`submit/status/logs/fetch/cancel` + notebook/file ops) is the spine; Colab is one backend among several. Each backend is an isolated subpackage so a Colab breakage cannot break Modal/Vertex.
> 3. The **opt-in escape hatch** (raw `/tun/m/*` client) is physically quarantined in `transport/direct_tun/`, is import-guarded, and is never wired into defaults.
> 4. **Secrets** go through a backend-pluggable `secrets/` layer (keyring + SecretService + Windows + age-file), keyed per account email, with >4KB chunking.
> 5. **All durable state is externalized to Drive/GCS**; nothing in this package assumes a persistent runtime filesystem.

---

### 1. Complete Annotated Directory Tree

```text
colabctl/                                  # repo root (git)
├── pyproject.toml                         # single source of build + tool config (PEP 621 + uv)
├── uv.lock                                # fully resolved, committed lockfile (reproducible installs)
├── README.md                             # quickstart, install matrix, ToS/abuse-risk disclosure banner
├── LICENSE                                # Apache-2.0 (matches upstream google-colab-cli / colab-mcp)
├── CHANGELOG.md                           # Keep-a-Changelog format, maintained per PR
├── SECURITY.md                            # credential blast-radius, keychain-is-not-a-boundary disclosure
├── CODE_OF_CONDUCT.md
├── CONTRIBUTING.md                        # dev setup via uv, how to add a provider backend
├── .python-version                        # "3.11" — uv pins the dev interpreter floor
├── .gitignore                             # excludes .venv, *.age, profiles/*.local.toml, __pycache__, dist/
├── .pre-commit-config.yaml                # ruff (lint+format), pyright, codespell, check-toml
├── .editorconfig
│
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                         # lint+type+test matrix: {3.11,3.12,3.13} x {linux,macos,windows}
│   │   ├── release.yml                    # tag-triggered build + PyPI Trusted Publishing (OIDC)
│   │   ├── docs.yml                        # mkdocs build + gh-pages deploy on main
│   │   └── cli-drift-probe.yml            # SCHEDULED: probes google-colab-cli latest vs pin (see §6.4)
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.yml
│   │   ├── backend_request.yml
│   │   └── cli_drift.yml                  # auto-filed by cli-drift-probe.yml
│   └── dependabot.yml
│
├── src/
│   └── colabctl/
│       ├── __init__.py                    # exports __version__, top-level façade re-exports
│       ├── __about__.py                   # __version__ = "X.Y.Z" — single source of version truth
│       ├── py.typed                       # PEP 561 marker: ship type info to consumers
│       │
│       ├── core/                          # framework-agnostic primitives (no I/O frameworks here)
│       │   ├── __init__.py
│       │   ├── errors.py                  # exception taxonomy (see §1.1)
│       │   ├── logging.py                 # structlog setup, secret-redaction processor
│       │   ├── types.py                   # NewTypes: AccountEmail, ProxyToken, AssignmentId, RuntimeUrl
│       │   ├── result.py                  # Outcome[T] result-envelope (success/quota/denylist/error)
│       │   └── async_utils.py             # retry-with-jitter, timeout helpers, async context mgrs
│       │
│       ├── config/                        # layered configuration system (see §5)
│       │   ├── __init__.py                # load_config(), resolve_profile() public API
│       │   ├── models.py                  # pydantic-settings models: ColabctlConfig, ProfileConfig, ...
│       │   ├── sources.py                 # EnvSource, FileSource, ProfileSource, DefaultSource
│       │   ├── precedence.py              # merge algorithm (CLI > env > profile file > defaults)
│       │   ├── paths.py                   # platformdirs resolution of config/data/cache/log dirs
│       │   └── migrate.py                 # config schema_version upgrades
│       │
│       ├── secrets/                        # pluggable secret storage (defense-in-depth, NOT a boundary)
│       │   ├── __init__.py                # get_secret_store() factory + capability probe
│       │   ├── base.py                    # SecretStore ABC (get/set/delete/list_accounts)
│       │   ├── chunking.py                # >4KB blob chunking across N keychain items
│       │   ├── keyring_store.py           # macOS Keychain / cross-platform keyring backend
│       │   ├── secretservice_store.py     # Linux SecretService (explicit, headless-aware)
│       │   ├── windows_store.py           # Windows Credential Manager
│       │   ├── age_file_store.py          # age-encrypted file backend (headless servers / CI)
│       │   └── models.py                  # StoredCredential, ChunkManifest (pydantic v2)
│       │
│       ├── auth/                          # OAuth + ADC credential acquisition & refresh
│       │   ├── __init__.py
│       │   ├── base.py                    # CredentialProvider ABC
│       │   ├── oauth_loopback.py          # SANCTIONED: loopback (127.0.0.1) PKCE S256 flow
│       │   ├── token_lifecycle.py         # refresh, 7-day-death detection, re-consent prompts
│       │   ├── adc.py                     # GCP ADC / service-account (Vertex/Enterprise ONLY)
│       │   └── models.py                  # OAuthToken, RefreshState, ConsentStatus (pydantic v2)
│       │
│       ├── transport/                     # how we REACH a runtime (one subpkg per mechanism)
│       │   ├── __init__.py
│       │   ├── base.py                    # Transport ABC + TransportCapabilities
│       │   ├── official_cli/              # PRIMARY: vendored/pinned google-colab-cli subprocess
│       │   │   ├── __init__.py
│       │   │   ├── adapter.py             # OfficialCliTransport (hard adapter interface)
│       │   │   ├── subprocess_env.py      # isolated uv-tool / venv interpreter resolution
│       │   │   ├── capability_probe.py    # detect version, JSON-mode availability, flags
│       │   │   ├── output_parser.py       # stdout/JSON parsing with graceful degradation
│       │   │   └── version_pin.py         # PINNED_CLI_VERSION + compat range constants
│       │   ├── browser_bridge/            # SECONDARY: colab-mcp bridge (human-in-the-loop)
│       │   │   ├── __init__.py
│       │   │   └── adapter.py             # BrowserBridgeTransport (requires open logged-in tab)
│       │   └── direct_tun/               # ESCAPE HATCH: opt-in, disclosed-risk /tun/m/* client
│       │       ├── __init__.py            # raises EscapeHatchDisabled unless explicitly enabled
│       │       ├── _guard.py              # import + config gate (see §1.2)
│       │       ├── client.py              # /tun/m/assign|assignments|unassign|ccu-info REST
│       │       ├── proxy_token.py         # RuntimeProxyInfo lifecycle + token refresh
│       │       └── headers.py             # CORRECT header recipe (proxy token = header ONLY)
│       │
│       ├── execution/                     # run code on a kernel over Jupyter WS protocol
│       │   ├── __init__.py
│       │   ├── kernel_client.py           # wraps jupyter-kernel-client; injects correct headers
│       │   ├── stream.py                  # async output streaming (stdout/err/display/error)
│       │   └── models.py                  # KernelOutput, ExecuteRequest, ExecuteResult
│       │
│       ├── sync/                          # durable artifact sync (Drive user-OAuth blob upload)
│       │   ├── __init__.py
│       │   ├── drive.py                   # DriveSync: plain-blob .ipynb upload to My Drive
│       │   ├── kernel_comms.py            # in-VM transient I/O via Google's kernel comms
│       │   └── models.py                  # DriveFile, UploadResult, SyncManifest
│       │
│       ├── providers/                     # THE SPINE: capability-detecting provider abstraction
│       │   ├── __init__.py                # ProviderRegistry, get_provider(name)
│       │   ├── base.py                    # Provider ABC: submit/status/logs/fetch/cancel + caps
│       │   ├── capabilities.py            # ProviderCapabilities feature-detection model
│       │   ├── routing.py                 # fallback routing policy (Colab→Modal→Vertex…)
│       │   ├── models.py                  # Job, JobStatus, JobSpec, GpuRequest, JobArtifact
│       │   ├── colab/                     # first-class node (uses transport/* + execution/*)
│       │   │   ├── __init__.py
│       │   │   └── provider.py            # ColabProvider
│       │   ├── modal/                     # first-tier sanctioned (gVisor sandboxes)
│       │   │   └── provider.py
│       │   ├── vertex/                    # first-tier sanctioned (Colab Enterprise / Vertex)
│       │   │   └── provider.py
│       │   ├── papermill_adapter.py       # optional: batch .ipynb over any kernel backend
│       │   └── fallbacks/                 # lower-priority registered backends
│       │       ├── __init__.py
│       │       ├── hf_jobs.py             # HF Jobs
│       │       ├── kaggle.py              # Kaggle kernels (poll-then-fetch)
│       │       ├── runpod.py              # RunPod / vast.ai marketplace IaaS
│       │       └── hyperscaler.py         # Vertex CustomJob / SageMaker create_training_job
│       │
│       ├── cli/                           # developer-facing Typer CLI
│       │   ├── __init__.py
│       │   ├── app.py                     # Typer root app; wires subcommands
│       │   ├── commands/
│       │   │   ├── auth.py                # colabctl auth login|status|logout
│       │   │   ├── runtime.py             # colabctl runtime new|list|stop
│       │   │   ├── run.py                 # colabctl run <code|file>
│       │   │   ├── notebook.py            # colabctl notebook run|push|pull
│       │   │   ├── config_cmd.py          # colabctl config show|set|profiles
│       │   │   └── providers_cmd.py       # colabctl providers list|capabilities
│       │   ├── render.py                  # rich tables / JSON output (--json global flag)
│       │   └── shim_main.py               # entry point: colabctl-cli-shim (subprocess host)
│       │
│       ├── mcp/                           # AI-agent surface (FastMCP server)
│       │   ├── __init__.py
│       │   ├── server.py                  # FastMCP app exposing provider verbs as tools
│       │   ├── tools.py                   # @mcp.tool wrappers → ProviderRegistry verbs
│       │   └── schemas.py                 # MCP-facing pydantic I/O schemas
│       │
│       └── data/                          # packaged non-code assets (importlib.resources)
│           ├── default_config.toml        # shipped defaults (lowest precedence layer)
│           └── COLAB_SKILL.md             # agent-facing usage guide (mirrors upstream pattern)
│
├── tests/
│   ├── conftest.py                        # fixtures: tmp keychain, fake CLI subprocess, frozen clock
│   ├── unit/                              # per-module, no network (mock transports/providers)
│   ├── contract/                          # provider-abstraction contract tests (run on every backend)
│   ├── integration/                       # marked @pytest.mark.live; opt-in, real creds via env
│   └── fixtures/                          # recorded CLI outputs, golden JSON, sample .ipynb
│
├── docs/                                  # mkdocs-material source (see §7)
│   ├── index.md
│   ├── getting-started/
│   ├── guides/
│   ├── reference/                         # mkdocstrings auto-API from docstrings
│   ├── architecture/                      # this spec, decision records
│   └── risk-and-tos.md                    # explicit abuse-detection / ToS disclosure page
│
├── mkdocs.yml
└── scripts/
    ├── bump_version.py                    # bumps __about__.py, CHANGELOG; called by release flow
    └── probe_cli_version.py               # used by cli-drift-probe.yml (see §6.4)
```

#### 1.1 Exception taxonomy (`core/errors.py`)

All errors derive from one root so callers/agents can catch broadly, and structured subtypes carry machine-readable fields.

```python
class ColabctlError(Exception):
    """Root of all package errors. Carries a stable `code` for agents."""
    code: str = "colabctl_error"

class ConfigError(ColabctlError): code = "config_error"
class SecretStoreError(ColabctlError): code = "secret_store_error"
class SecretTooLargeError(SecretStoreError): code = "secret_too_large"   # chunking trigger
class AuthError(ColabctlError): code = "auth_error"
class RefreshTokenExpiredError(AuthError): code = "refresh_token_expired"  # 7-day death
class TransportError(ColabctlError): code = "transport_error"
class OfficialCliError(TransportError): code = "official_cli_error"
class OfficialCliVersionMismatch(OfficialCliError): code = "cli_version_mismatch"
class EscapeHatchDisabled(TransportError): code = "escape_hatch_disabled"  # direct_tun gate
class ProxyTokenExpiredError(TransportError): code = "proxy_token_expired"
class TooManyAssignmentsError(TransportError): code = "too_many_assignments"  # 412
class AbuseBlockedError(ProviderError): code = "abuse_blocked"           # opaque Google ban
class QuotaExceededError(ProviderError): code = "quota_exceeded"
class ProviderError(ColabctlError): code = "provider_error"
class ProviderUnavailableError(ProviderError): code = "provider_unavailable"  # triggers routing
class CapabilityUnsupportedError(ProviderError): code = "capability_unsupported"
```

#### 1.2 Escape-hatch import guard (`transport/direct_tun/_guard.py`)

The raw `/tun/m/*` client must be impossible to use by accident. Importing the package member without explicit opt-in raises.

```python
# transport/direct_tun/_guard.py
from colabctl.core.errors import EscapeHatchDisabled

def assert_escape_hatch_enabled(cfg: "ColabctlConfig") -> None:
    if not cfg.experimental.enable_direct_tun:
        raise EscapeHatchDisabled(
            "The direct /tun/m/* transport is a disclosed-risk escape hatch. "
            "Enable it explicitly via experimental.enable_direct_tun=true "
            "(or COLABCTL_EXPERIMENTAL__ENABLE_DIRECT_TUN=1) and accept "
            "the fragility/abuse-detection risk documented in docs/risk-and-tos.md."
        )
```

`transport/direct_tun/__init__.py` does **not** import `client.py` at module top level; the client is only constructed via a factory that first calls `assert_escape_hatch_enabled`. This keeps the heavy `httpx`/`websockets` direct-client code out of the default import path and out of the default capability surface.

---

### 2. Module Breakdown & Responsibilities

| Layer / package | Responsibility | Key public surface | Must NOT do |
| --- | --- | --- | --- |
| `core/` | Framework-free primitives: errors, typed IDs, result envelope, async retry, secret-redacting logging | `Outcome`, `ColabctlError` tree, `retry_async()` | No `httpx`, no `typer`, no provider imports (prevents cycles) |
| `config/` | Resolve layered config into a frozen `ColabctlConfig`; named profiles; path resolution | `load_config()`, `resolve_profile()` | No secret values in config objects (secrets live in `secrets/`) |
| `secrets/` | Store/fetch credentials per account email; chunk >4KB; pick backend per platform/headless | `get_secret_store()`, `SecretStore` ABC | Never treated as a security boundary; no auth logic |
| `auth/` | Acquire/refresh OAuth (loopback PKCE) + ADC; detect 7-day refresh death | `CredentialProvider`, `OAuthLoopbackProvider` | No backend-specific transport calls |
| `transport/official_cli/` | PRIMARY runtime allocation via pinned `google-colab-cli` subprocess in isolated interpreter | `OfficialCliTransport`, `probe_capabilities()` | Never `import google_colab_cli` into our process |
| `transport/browser_bridge/` | Human-in-the-loop interactive sessions via colab-mcp bridge | `BrowserBridgeTransport` | Never claim headless capability |
| `transport/direct_tun/` | OPT-IN escape hatch: raw `/tun/m/*` + proxy-token lifecycle | `DirectTunClient` (gated) | Never auto-enabled; never default |
| `execution/` | Run code over Jupyter WS using `jupyter-kernel-client` with correct header recipe | `KernelClient.execute()`, `stream_outputs()` | Never send proxy token as Bearer/query |
| `sync/` | Durable `.ipynb`/artifact sync to My Drive (user-OAuth plain blob); transient kernel-comms I/O | `DriveSync.upload_notebook()` | Never use a service account for Drive-native files |
| `providers/` | The spine: capability-detecting `submit/status/logs/fetch/cancel` + notebook ops across backends; fallback routing | `Provider` ABC, `ProviderRegistry`, `route()` | Never leak backend-specific types past the ABC |
| `cli/` | Developer CLI (Typer); also hosts the `colabctl-cli-shim` subprocess entry | `app` (Typer), `shim_main()` | No business logic — thin over `providers/` |
| `mcp/` | Agent surface (FastMCP) exposing the same verbs | `build_server()`, `@mcp.tool`s | Mirror `providers/`; no duplicated logic |
| `data/` | Shipped defaults + agent skill doc | `default_config.toml`, `COLAB_SKILL.md` | Read-only at runtime |

**Dependency direction (enforced by import-linter, §3.2):** `cli`/`mcp` → `providers` → (`transport`, `execution`, `sync`) → (`auth`, `secrets`) → `config` → `core`. No upward imports; `core` imports nothing internal.

---

### 3. `pyproject.toml` + uv Setup

#### 3.1 `pyproject.toml`

```toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "colabctl"                      # provisional; single rename point
dynamic = ["version"]                  # version read from src/colabctl/__about__.py
description = "Programmatic control of Google Colab Pro and sanctioned GPU backends for developers and AI agents."
readme = "README.md"
requires-python = ">=3.11"             # CORE floor; official CLI's 3.13 need is satisfied via subprocess
license = "Apache-2.0"
license-files = ["LICENSE"]
authors = [{ name = "colabctl maintainers" }]
keywords = ["colab", "gpu", "jupyter", "mcp", "agents", "modal", "vertex-ai"]
classifiers = [
  "Development Status :: 4 - Beta",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "License :: OSI Approved :: Apache Software License",
  "Intended Audience :: Developers",
  "Operating System :: OS Independent",
]

# --- Lean core: only what EVERY install needs ---
dependencies = [
  "pydantic>=2.9,<3",                  # all models; v2 perf + strict validation
  "pydantic-settings>=2.5,<3",         # env/file/profile layered settings
  "httpx>=0.27,<1",                    # async HTTP for Drive + direct_tun + bridge
  "websockets>=13,<16",                # Jupyter WS transport substrate
  "typer>=0.12,<1",                    # developer CLI
  "rich>=13.7",                        # CLI rendering (also pulled by typer)
  "structlog>=24.1",                   # structured, redactable logging
  "keyring>=25.7,<26",                 # OS keychain secret backend (default secrets path)
  "platformdirs>=4.2",                 # cross-platform config/data/cache dirs
  "tomli-w>=1.0",                      # write config/profile TOML (read via stdlib tomllib)
  "jupyter-kernel-client>=0.6,<1",     # Jupyter WS protocol exec (custom-header capable)
]

[project.optional-dependencies]
# Colab path extras (Drive sync via user-OAuth)
colab = [
  "google-api-python-client>=2.130",   # Drive v3 client
  "google-auth>=2.30",                 # OAuth user creds + ADC
  "google-auth-oauthlib>=1.2",         # loopback OAuth flow helper
]
# First-tier sanctioned backends
modal   = ["modal>=0.64"]
vertex  = ["google-cloud-aiplatform>=1.60"]
# Lower-priority fallbacks
hf      = ["huggingface_hub>=1.0"]
kaggle  = ["kaggle>=1.6"]
runpod  = ["runpod>=1.6", "vastai>=0.2"]
# Optional batch notebook adapter
papermill = ["papermill>=2.6", "nbclient>=0.10"]
# MCP agent server
mcp     = ["fastmcp>=2.0"]
# Headless / CI secret backend
age     = ["age-keyring>=0.1; sys_platform == 'linux'"]
secretservice = ["secretstorage>=3.3; sys_platform == 'linux'"]
# Convenience meta-extra: the recommended default install
recommended = ["colabctl[colab,modal,vertex,mcp]"]
all = ["colabctl[colab,modal,vertex,hf,kaggle,runpod,papermill,mcp,age,secretservice]"]

[project.scripts]
colabctl = "colabctl.cli.app:main"
colabctl-mcp = "colabctl.mcp.server:main"
colabctl-cli-shim = "colabctl.cli.shim_main:main"   # internal subprocess host for google-colab-cli

[project.urls]
Homepage = "https://github.com/colabctl/colabctl"
Documentation = "https://colabctl.github.io/colabctl"
Changelog = "https://github.com/colabctl/colabctl/blob/main/CHANGELOG.md"
Issues = "https://github.com/colabctl/colabctl/issues"

# --- Hatch: dynamic version from __about__.py ---
[tool.hatch.version]
path = "src/colabctl/__about__.py"

[tool.hatch.build.targets.wheel]
packages = ["src/colabctl"]

[tool.hatch.build.targets.sdist]
include = ["src/colabctl", "tests", "README.md", "CHANGELOG.md", "LICENSE"]

# --- uv: dev tooling + managed external CLI ---
[tool.uv]
# google-colab-cli is NOT a library dependency. It is installed as an isolated `uv tool`
# (own venv + own interpreter) and invoked as a subprocess. We pin it here for reproducibility
# of the dev environment only; runtime resolution is in transport/official_cli/subprocess_env.py.
dev-dependencies = [
  "pytest>=8.2",
  "pytest-asyncio>=0.23",
  "pytest-cov>=5.0",
  "respx>=0.21",                       # httpx mocking
  "ruff>=0.6",
  "pyright>=1.1.380",
  "import-linter>=2.0",                # enforces layer dependency direction
  "mkdocs-material>=9.5",
  "mkdocstrings[python]>=0.26",
  "pre-commit>=3.8",
]

[tool.uv.sources]
# (No internal path/git sources by default.)

# --- ruff ---
[tool.ruff]
line-length = 100
target-version = "py311"
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "ASYNC", "S", "RUF", "PTH"]
ignore = ["S603", "S607"]              # subprocess calls are intentional + controlled in official_cli
[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101"]                  # asserts allowed in tests

# --- pyright (strict) ---
[tool.pyright]
include = ["src"]
pythonVersion = "3.11"
typeCheckingMode = "strict"
reportMissingTypeStubs = false

# --- pytest ---
[tool.pytest.ini_options]
addopts = "-ra --strict-markers --cov=colabctl --cov-report=term-missing"
asyncio_mode = "auto"
markers = [
  "live: hits real external services; requires creds; opt-in via `-m live`",
  "contract: provider-abstraction contract tests run against each backend",
]
testpaths = ["tests"]
```

#### 3.2 Import-linter contract (`.importlinter`, run in CI)

```ini
[importlinter]
root_package = colabctl

[importlinter:contract:layers]
name = Enforce layered architecture
type = layers
layers =
    colabctl.cli | colabctl.mcp
    colabctl.providers
    colabctl.transport | colabctl.execution | colabctl.sync
    colabctl.auth
    colabctl.secrets
    colabctl.config
    colabctl.core

[importlinter:contract:escape_hatch_isolation]
name = direct_tun is not imported by defaults
type = forbidden
source_modules = colabctl.providers.colab.provider
forbidden_modules = colabctl.transport.direct_tun.client
```

#### 3.3 Developer bootstrap (canonical commands)

```bash
# clone, then:
uv sync --all-extras                       # create .venv, install package + all extras + dev tools
uv run pre-commit install                  # git hooks
uv run pytest -m "not live"                # full offline test suite
uv run ruff check . && uv run pyright      # lint + type
uv run lint-imports                        # import-linter contract gate
uv run mkdocs serve                        # live docs

# Install the EXTERNAL official CLI in its own isolated env (Python 3.13), pinned:
uv tool install "google-colab-cli==<PINNED>" --python 3.13
# our subprocess layer auto-discovers this tool's executable (see §4)
```

---

### 4. Key Dependency Choices (one-line justifications)

| Dependency | Why this one |
| --- | --- |
| `pydantic>=2.9` + `pydantic-settings` | v2 perf + strict validation for all models (tokens, RuntimeProxyInfo, capability descriptors) and native env/file/profile layering. |
| `httpx` | Async-first HTTP with the same client for Drive, browser-bridge, and the direct_tun escape hatch; `respx` gives clean test mocking. |
| `websockets` | Lightweight async WS substrate underneath `jupyter-kernel-client` for kernel exec/streaming. |
| `jupyter-kernel-client>=0.6` | Implements the Jupyter wire protocol and (critically) supports **custom headers**, required for the header-only `X-Colab-Runtime-Proxy-Token` recipe; pinned `<1` because it is small/fast-moving. |
| `typer` | Type-hint-driven CLI with `rich` rendering; minimal boilerplate for the developer surface. |
| `fastmcp` (extra) | Mature FastMCP-style server to expose provider verbs to agents without hand-rolling MCP plumbing. |
| `keyring>=25.7` | Actively-maintained OS keychain access; the default secret backend (defense-in-depth, not a boundary). |
| `platformdirs` | Correct per-OS config/data/cache/log paths so config/profiles/age-files land in standard locations. |
| `google-api-python-client` + `google-auth*` (extra) | Official Drive v3 + user-OAuth/ADC; user-OAuth plain-blob `.ipynb` upload to My Drive (avoids the SA-can't-own-native-files 403). |
| `modal` / `google-cloud-aiplatform` (extras) | First-tier sanctioned backends (gVisor sandboxes; Vertex/Colab Enterprise) the abstraction routes to when Colab degrades. |
| `huggingface_hub` / `kaggle` / `runpod`+`vastai` (extras) | Registered lower-priority fallback backends. |
| `papermill`+`nbclient` (extra) | Optional batch `.ipynb`-over-any-kernel adapter. |
| **`google-colab-cli` — NOT a dependency** | Python-3.13-only, v0.5.x with yanked releases, no stable JSON mode, rejects external PRs → installed as an isolated `uv tool` and called as a **pinned subprocess** so it cannot drag our floor to 3.13 or break our import graph. |
| `structlog` | Structured logs with a redaction processor that scrubs tokens/cookies before any sink. |
| `hatchling` (build) | Simple PEP 621 backend with `dynamic` version pulled from `__about__.py`. |
| `ruff` + `pyright` + `import-linter` | Fast lint/format, strict typing, and machine-enforced layer boundaries (esp. escape-hatch isolation). |

---

### 5. Layered Configuration System

#### 5.1 Precedence (highest wins)

```
1. Explicit CLI flags / MCP tool arguments
2. Environment variables           (prefix COLABCTL_, nested via __)
3. Active profile file             (profiles/<name>.toml selected by --profile / COLABCTL_PROFILE)
4. Base user config file           (config.toml)
5. Packaged defaults               (src/colabctl/data/default_config.toml)
```

Secrets are **never** stored in any of these layers — config holds only the *account email* (a lookup key) and backend selectors; actual tokens/cookies resolve through `secrets/`.

#### 5.2 Path resolution (`config/paths.py`)

Uses `platformdirs` with app name `colabctl`:

| Purpose | macOS | Linux | Windows |
| --- | --- | --- | --- |
| Config / profiles | `~/Library/Application Support/colabctl/` | `~/.config/colabctl/` | `%APPDATA%\colabctl\` |
| Data (age-file store) | `~/Library/Application Support/colabctl/` | `~/.local/share/colabctl/` | `%LOCALAPPDATA%\colabctl\` |
| Cache (CLI capability probe) | `~/Library/Caches/colabctl/` | `~/.cache/colabctl/` | `%LOCALAPPDATA%\colabctl\Cache\` |
| Logs | `~/Library/Logs/colabctl/` | `~/.local/state/colabctl/log/` | `%LOCALAPPDATA%\colabctl\Logs\` |

Override root with `COLABCTL_CONFIG_DIR`. Profiles live in `<config_dir>/profiles/<name>.toml`.

#### 5.3 Config models (`config/models.py`, pydantic-settings v2)

```python
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Literal

SecretBackend = Literal["keyring", "secretservice", "windows", "age-file", "auto"]
TransportName = Literal["official_cli", "browser_bridge", "direct_tun", "auto"]
ProviderName  = Literal["colab", "modal", "vertex", "hf", "kaggle", "runpod", "hyperscaler"]

class SecretsConfig(BaseModel):
    backend: SecretBackend = "auto"
    service_name: str = "colabctl"
    chunk_threshold_bytes: int = 4096          # split blobs >4KB across keychain items
    age_identity_file: str | None = None       # required when backend == "age-file"

class AuthConfig(BaseModel):
    account_email: str | None = None           # lookup key into secrets/ (NOT a secret)
    flow: Literal["loopback", "adc"] = "loopback"
    loopback_port: int = 0                      # 0 = ephemeral
    refresh_skew_seconds: int = 120
    warn_on_7day_death: bool = True

class TransportConfig(BaseModel):
    preferred: TransportName = "official_cli"
    official_cli_executable: str | None = None  # auto-discovered uv-tool path if None
    official_cli_python: str = "3.13"
    capability_probe_ttl_seconds: int = 86400

class ProvidersConfig(BaseModel):
    default: ProviderName = "colab"
    fallback_order: list[ProviderName] = Field(default_factory=lambda: ["colab", "modal", "vertex"])
    auto_route_on_unavailable: bool = True
    auto_route_on_abuse_block: bool = True      # if Colab denylists, route to next backend

class ExperimentalConfig(BaseModel):
    enable_direct_tun: bool = False             # the escape-hatch gate (see §1.2)
    accept_fragility_risk: bool = False

class ProfileConfig(BaseModel):
    """A named, fully-resolvable profile (e.g. 'work', 'ci', 'agent')."""
    secrets: SecretsConfig = SecretsConfig()
    auth: AuthConfig = AuthConfig()
    transport: TransportConfig = TransportConfig()
    providers: ProvidersConfig = ProvidersConfig()
    experimental: ExperimentalConfig = ExperimentalConfig()

class ColabctlConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COLABCTL_",
        env_nested_delimiter="__",              # COLABCTL_TRANSPORT__PREFERRED=official_cli
        extra="forbid",
        frozen=True,                            # immutable after load
    )
    schema_version: int = 1
    active_profile: str = "default"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    json_output: bool = False
    # The resolved, merged profile (populated by load_config, not by env directly):
    profile: ProfileConfig = ProfileConfig()
```

#### 5.4 Load algorithm (`config/__init__.py`)

```python
def load_config(
    *,
    profile_name: str | None = None,
    cli_overrides: dict | None = None,
) -> ColabctlConfig:
    """
    Resolve the effective configuration following the precedence in §5.1.

    1. Start from packaged defaults (data/default_config.toml).
    2. Deep-merge base user config.toml (if present).
    3. Determine profile name: cli_overrides['active_profile'] > COLABCTL_PROFILE
       > base-config active_profile > "default". Deep-merge profiles/<name>.toml.
    4. Overlay environment variables (pydantic-settings, COLABCTL_ prefix, __ nesting).
    5. Overlay explicit CLI/MCP overrides (deep-merge).
    6. Run config/migrate.py if schema_version < current; persist migrated base config.
    7. Validate into a frozen ColabctlConfig. Raise ConfigError with the offending
       key path on validation failure (pydantic error → friendly message).
    """
```

`precedence.py` provides `deep_merge(base, override)` (override wins per-leaf; lists replace, not append) and `select_profile()`.

#### 5.5 Example config files

`config.toml`:
```toml
schema_version = 1
active_profile = "work"
log_level = "INFO"
```

`profiles/work.toml`:
```toml
[auth]
account_email = "iris@analyticsandsociety.com"
flow = "loopback"

[transport]
preferred = "official_cli"

[providers]
default = "colab"
fallback_order = ["colab", "modal", "vertex"]
```

`profiles/ci.toml` (headless server example):
```toml
[secrets]
backend = "age-file"
age_identity_file = "/run/secrets/colabctl.age"

[auth]
account_email = "agent@example.com"
flow = "adc"                 # Vertex/Enterprise path on CI

[providers]
default = "modal"            # avoid Colab's interactive/abuse-detection exposure in CI
fallback_order = ["modal", "vertex"]
```

#### 5.6 Env var examples

```bash
COLABCTL_PROFILE=work
COLABCTL_LOG_LEVEL=DEBUG
COLABCTL_JSON_OUTPUT=1
COLABCTL_TRANSPORT__PREFERRED=official_cli
COLABCTL_PROVIDERS__DEFAULT=modal
COLABCTL_EXPERIMENTAL__ENABLE_DIRECT_TUN=1   # opt into the escape hatch
```

#### 5.7 Config edge cases & failure handling

| Edge case | Handling |
| --- | --- |
| Profile name set but `profiles/<name>.toml` missing | `ConfigError` listing available profiles; never silently fall back. |
| `extra` keys in any TOML | `extra="forbid"` → `ConfigError` with the unknown key path (catches typos like `transprot`). |
| `backend == "age-file"` but `age_identity_file` unset/unreadable | `SecretStoreError` at first secret access, with remediation message. |
| `enable_direct_tun=true` but `accept_fragility_risk` not set | CLI prints a one-time disclosure and requires `--accept-risk` (or config flag) before proceeding. |
| Env var present but malformed (e.g. `COLABCTL_PROVIDERS__FALLBACK_ORDER=foo`) | pydantic validation error → `ConfigError`; lists allowed enum values. |
| `schema_version` newer than code supports | `ConfigError`: "config written by a newer colabctl; upgrade the package." |
| Two profiles disagree with env | Env always wins (§5.1); `colabctl config show --explain` prints which layer set each key. |

---

### 6. Versioning & Release Process

#### 6.1 Version policy

- **SemVer** with a documented pre-1.0 caveat: while `0.y.z`, **minor** bumps may break compat (the package is young and the Colab transport churns).
- **Single source of truth:** `src/colabctl/__about__.py` (`__version__ = "X.Y.Z"`), read by Hatch (`dynamic = ["version"]`) and re-exported as `colabctl.__version__`.
- **External-CLI compat note** documented in CHANGELOG and `transport/official_cli/version_pin.py`: `PINNED_CLI_VERSION` plus a `MIN_CLI_VERSION`/`MAX_CLI_VERSION` compat range; bumping the pin is a **minor** release because behavior can change.

#### 6.2 Branch & PR rules

- Trunk-based on `main`; every PR updates `CHANGELOG.md` under an `## [Unreleased]` heading. CI fails a PR that touches `src/` without a changelog entry (a `check-changelog` job).
- `scripts/bump_version.py` moves `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, bumps `__about__.py`, and commits.

#### 6.3 Release workflow (`.github/workflows/release.yml`)

```text
Trigger:  push tag matching v*.*.*
Steps:
  1. uv sync --all-extras
  2. uv run pytest -m "not live"  (full matrix gate already ran on the merge commit)
  3. Assert tag == __about__.__version__ (fail otherwise)
  4. uv build  → sdist + wheel
  5. Publish to PyPI via Trusted Publishing (OIDC, no stored token)
  6. gh release create with extracted CHANGELOG section as body
  7. docs.yml deploys versioned docs (mike) to gh-pages
```

#### 6.4 Official-CLI drift defense (`cli-drift-probe.yml`)

A scheduled job runs `scripts/probe_cli_version.py`: it queries PyPI for the latest `google-colab-cli`, compares to `PINNED_CLI_VERSION`, checks whether the latest is yanked, and runs `transport/official_cli/capability_probe.py` against it in a throwaway 3.13 `uv tool` env. On any change (new version, yank, capability delta such as JSON-mode appearing/disappearing) it auto-files a `cli_drift` issue. This is how we "treat it as a fast-moving dependency" operationally rather than discovering breakage in production.

---

### 7. Documentation Layout (mkdocs-material)

```text
docs/
├── index.md                              # what it is, install matrix, 60-second quickstart
├── getting-started/
│   ├── install.md                        # uv install, extras matrix, external CLI uv-tool step
│   ├── authenticate.md                   # loopback OAuth; headless token-copy; ADC for Vertex
│   └── first-run.md                       # colabctl runtime new --gpu T4; run code; pull notebook
├── guides/
│   ├── profiles-and-config.md            # the §5 system, with `config show --explain`
│   ├── providers-and-routing.md          # capability matrix + fallback routing
│   ├── drive-sync.md                     # user-OAuth blob upload, why not service accounts
│   ├── mcp-for-agents.md                 # wiring colabctl-mcp into Claude/Gemini/etc.
│   └── escape-hatch-direct-tun.md        # opt-in, disclosed-risk; how to enable + accept risk
├── reference/
│   ├── cli.md                            # auto-generated Typer command reference
│   ├── api/                               # mkdocstrings: ::: colabctl.providers.base etc.
│   └── config-schema.md                  # generated from pydantic JSON Schema
├── architecture/
│   ├── overview.md                       # the layered design + dependency direction diagram
│   ├── this-spec.md                      # THIS section, kept in sync
│   └── decisions/                         # ADRs (e.g. ADR-0001 official-CLI-as-subprocess)
└── risk-and-tos.md                       # ToS band, abuse-detection exposure, blast radius
```

`mkdocs.yml` (essentials):

```yaml
site_name: colabctl
theme:
  name: material
  features: [navigation.sections, content.code.copy, search.suggest]
plugins:
  - search
  - mkdocstrings:
      handlers:
        python:
          options:
            docstring_style: google
            show_signature_annotations: true
  - mike                                   # versioned docs (per release)
markdown_extensions:
  - admonition
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
nav:
  - Home: index.md
  - Getting Started: [getting-started/install.md, getting-started/authenticate.md, getting-started/first-run.md]
  - Guides: [guides/profiles-and-config.md, guides/providers-and-routing.md, guides/drive-sync.md, guides/mcp-for-agents.md, guides/escape-hatch-direct-tun.md]
  - Reference: [reference/cli.md, reference/config-schema.md, reference/api/]
  - Architecture: [architecture/overview.md, architecture/this-spec.md, architecture/decisions/]
  - Risk & ToS: risk-and-tos.md
```

The **`config-schema.md`** and **`reference/cli.md`** pages are generated in CI (`docs.yml`) from `ColabctlConfig.model_json_schema()` and a Typer doc dump, so docs cannot drift from the models/commands.

---

### 8. Packaging-Level Edge Cases & Failure Handling

| Edge case | Where handled | Behavior |
| --- | --- | --- |
| Official CLI requires Python 3.13; user runs colabctl on 3.11 | `transport/official_cli/subprocess_env.py` | Resolve a 3.13 interpreter via the isolated `uv tool` install; if absent, raise `OfficialCliError` with the exact `uv tool install` command. Core process stays 3.11+. |
| Official CLI version on disk ≠ compat range | `version_pin.py` + `capability_probe.py` | Raise `OfficialCliVersionMismatch`; suggest `auto_route_on_unavailable` fallback to Modal/Vertex. |
| Official CLI has no stable JSON mode in installed version | `output_parser.py` | Capability probe records `json_mode=False`; parser uses the human-readable path with defensive regex + golden-fixture tests; surfaces a warning. |
| Optional extra not installed (e.g. `modal`) but provider requested | `providers/__init__.py` registry | Raise `ProviderUnavailableError` naming the missing extra (`pip install 'colabctl[modal]'`); routing skips it. |
| Secret blob >4KB (cookie/large token) on keychain backend | `secrets/chunking.py` | Split into N items with a `ChunkManifest`; reassemble on read; `SecretTooLargeError` only if the backend rejects even chunked writes. |
| Headless server, no GUI keychain | `secrets/__init__.py` `auto` probe | Detect no Secret Service / no GUI; select `age-file` backend; if no identity file configured → actionable `SecretStoreError`. |
| Escape hatch imported without opt-in | `transport/direct_tun/_guard.py` | `EscapeHatchDisabled` with remediation; import-linter also forbids defaults from importing it. |
| `py.typed` missing from wheel | Hatch wheel target includes package dir | CI test installs the built wheel into a fresh venv and runs `pyright` against a sample import to assert type info ships. |
| Two console scripts collide with an existing `colab`/`colabctl` on PATH | `[project.scripts]` uses the package-prefixed names only | We never claim the bare `colab` name (avoids clobbering the official CLI). |
| Reproducibility of installs | `uv.lock` committed; CI runs `uv sync --frozen` | A drifted lockfile fails CI. |

### 14.x Key decisions

- src-layout single distribution (import package `colabctl`) with `__about__.py` as the single version source consumed by Hatch via dynamic version; one rename point for the TBD final name.
- Hard layered package structure (core <- config <- secrets/auth <- transport/execution/sync <- providers <- cli/mcp) machine-enforced by import-linter, preventing dependency cycles and accidental coupling.
- google-colab-cli is explicitly NOT a Python dependency: it is installed as an isolated `uv tool` (its own 3.13 interpreter) and invoked as a pinned subprocess via transport/official_cli, preserving the core's Python 3.11+ floor.
- Lean required dependencies + optional extras per backend (colab, modal, vertex, hf, kaggle, runpod, papermill, mcp, age, secretservice) so a base install stays small and backends are opt-in; `recommended` and `all` meta-extras provided.
- The direct /tun/m/* escape hatch is physically quarantined in transport/direct_tun/, import-guarded (EscapeHatchDisabled), gated behind experimental.enable_direct_tun + accept-risk, and import-linter forbids defaults from importing it.
- Layered config via pydantic-settings with strict 5-tier precedence (CLI > env COLABCTL_ with __ nesting > active profile file > base config.toml > packaged default_config.toml), named profiles, frozen immutable ColabctlConfig, and extra=forbid to catch typos.
- Secrets are never stored in config (config holds only the account-email lookup key); a pluggable SecretStore (keyring default, plus SecretService/Windows/age-file) with >4KB chunking generalizes the macOS Keychain win to headless Linux/CI.
- Release via PyPI Trusted Publishing (OIDC, no stored token) on version tags, SemVer with a documented pre-1.0 minor-may-break caveat, CHANGELOG-per-PR gate, and a scheduled cli-drift-probe workflow that auto-files issues when google-colab-cli changes/yanks/capabilities shift.
- mkdocs-material docs with mkdocstrings + mike versioning; CLI reference and config-schema pages generated in CI from Typer and ColabctlConfig.model_json_schema() so docs cannot drift; a dedicated risk-and-tos.md surfaces abuse-detection exposure.

### 14.y Section risks

- google-colab-cli is Python-3.13-only, v0.5.x with yanked releases and no confirmed stable JSON mode; the subprocess/uv-tool isolation contains but does not eliminate the operational burden of an external interpreter dependency and stdout-parsing fragility (mitigated by capability_probe + golden fixtures + cli-drift-probe).
- Pinning the external CLI version means the package can break whenever Google ships an incompatible CLI; the compat range + scheduled drift probe reduce blast radius but a real break still requires a coordinated minor release.
- The optional-extras matrix increases the support surface: users hitting ProviderUnavailableError for an uninstalled backend, or partial installs in CI, are a recurring friction point; clear error messages naming the exact extra are essential.
- keyring/keychain is defense-in-depth not a security boundary (any same-user process can read after 'always allow'); the age-file backend introduces its own identity-file custody problem on headless servers that must be documented and operationally managed.
- The frozen, strict (extra=forbid) config plus multi-layer precedence is powerful but unforgiving; without the `config show --explain` tooling and good error messages, users will struggle to debug which layer set a value.
- Even fully packaged correctly, the Colab path inherits opaque abuse-detection/account-ban risk that no packaging decision can remove; the provider abstraction's auto-route-on-abuse-block is the only structural mitigation and depends on at least one alternate backend extra being installed and authenticated.
- Pre-1.0 SemVer where minor bumps may break compatibility can frustrate downstream pinning; consumers must pin tightly, and the policy must be prominently documented to avoid surprise breakage.

---

## 15. Consolidated Risk Register

Severity-ranked, each with its mitigation. These are *product-level* risks; section-level risks live with their sections above.

### [HIGH] Opaque, no-recourse Colab abuse-detection bans on sustained headless agent-driven GPU usage (colabtools #4979/#4986), hitting even paying Pro users with positive compute balance; blast radius is the whole Google account.

**Mitigation:** Make the abuse-detection risk a first-class, user-disclosed product fact, not a hidden assumption. Default to the sanctioned CLI/MCP path (lowest signal divergence from first-party clients). Implement the ~60s keep-alive and idle/lifetime handling exactly as the official CLI does, never faking 'active programming.' Enforce single-account, single-session-per-runtime by default; refuse multi-account quota-circumvention (explicitly ToS-banned). Provide instant failover to Modal/Vertex via the provider abstraction so a ban degrades capability rather than killing the product. Surface DENYLISTED/QUOTA_* outcomes and CCU balance to the user.

### [HIGH] Official google-colab-cli is immature and fast-moving (v0.5.x, yanked PyPI releases 0.5.5/0.5.6, Python 3.13-only, no confirmed stable JSON output mode, Google rejects external PRs) — the sanctioned primary path can break between point releases and forces stdout parsing.

**Mitigation:** Wrap it behind a hard adapter interface with strict version pinning and a startup capability probe that detects CLI version, available flags, and output format. Vendor a known-good version and invoke via an isolated uv tool env so the 3.13 requirement does not contaminate the 3.11+ core. Build a tolerant output parser with golden-file tests per pinned version; fail loudly with a clear 'CLI contract changed' error rather than silently. Maintain the opt-in /tun/m/* escape hatch and the Modal/Vertex backends as immediate alternatives if the CLI regresses.

### [HIGH] DBSC (Device Bound Session Credentials) — GA Chrome 146 April 2026, macOS Secure Enclave in Chrome 148 — structurally kills any cookie-extraction/SAPISIDHASH path within the product's ship window.

**Mitigation:** Do NOT build cookie/SAPISIDHASH or browser-cookie extraction at all (verdicts score them 1.5, AVOID). Authenticate exclusively via OAuth user creds (official CLI loopback) and GCP ADC for Enterprise. This risk is fully avoided by design, not mitigated after the fact.

### [MEDIUM] Undocumented Colab internals drift: /tun/m/* paths, RuntimeProxyInfo schema, header/XSRF contract, accelerator/quota enums, idle/lifetime limits all change without notice — affecting the opt-in escape hatch and any direct kernel exec.

**Mitigation:** Confine all reverse-engineered surface to the OPT-IN escape-hatch module, version-gated and disabled by default with disclosed risk. Pin to a known colab-vscode/colab-mcp commit, centralize every undocumented constant (header names, enum values, XSRF flow) behind a single config module, and write contract tests that probe-and-warn on drift. Never make a long-running workload depend on unpublished idle/lifetime numbers; rely on server-reported tokenExpiresInSeconds and checkpoint to Drive/GCS so re-assignment loses no state.

### [MEDIUM] keyring's security benefit is overstated: on macOS any same-user Python process can read secrets without a prompt after 'always allow'; first-access/binary-change prompts can deadlock headless runs; ~4KB Keychain item soft-limit truncates large blobs; Keychain backend does not exist on headless Linux/CI.

**Mitigation:** Treat keyring as defense-in-depth (no plaintext on disk, no git leaks), not a trust boundary. Chunk any blob >4KB across multiple items. Ship a pluggable backend abstraction (SecretService / Windows Credential Manager / age-encrypted file with passphrase from env) so headless Linux/CI works. Document the 'always-allow' exposure honestly. Prefer short-lived OAuth tokens over storing long-lived cookies (never store cookies).

### [MEDIUM] Service-account Drive sync is broken for the actual artifact: an SA cannot own a Google-native .ipynb (vnd.google.colaboratory MIME) → 403 storageQuotaExceeded; the documented escapes (Shared Drives, domain-wide delegation) require paid Workspace, which Colab Pro consumers don't have.

**Mitigation:** Build the Drive layer as USER-OAuth doing plain-blob .ipynb uploads to the human's My Drive (ownership and quota stay with the human). Reserve service-account/ADC Drive only for the Enterprise/Vertex backend where it is appropriate. Treat the undocumented Colab MIME string as a config constant with a fallback to plain .ipynb blob handling.

### [MEDIUM] Colab Pro's compute-unit economics and opaque, dynamic GPU availability cap reliability and throughput; long jobs hit 12h/24h lifetime and ~90min idle reclamation, losing in-VM state.

**Mitigation:** Mandatory checkpoint/resume to Drive/GCS for any long workload; the runtime-lifecycle manager re-assigns and resumes from checkpoint on termination. Surface CCU balance and quota outcomes. For deterministic, scalable, deadline-bound production runs, route to Colab Enterprise/Vertex or Modal via the abstraction. Implement spend/timeout guards on all paid alt-backends (Modal 5-min default timeout, RunPod orphaned-instance billing) with a billing watchdog and guaranteed teardown.

### [MEDIUM] Paid Colab ToS clause 'access the Paid Service other than by means authorized by Google' creates residual exposure even though UI-bypass/remote-control bans are lifted on paid tiers; rolling our own client is arguably less 'authorized' than the official CLI.

**Mitigation:** Default to the official Google CLI/MCP (the most defensibly 'authorized' path). Keep the reverse-engineered escape hatch opt-in and clearly labeled as higher-risk. Never resell/share access to third parties (ToS-banned). Document the ToS posture per backend so developers and downstream agents make informed choices.

---

## 16. Delivery Roadmap

### Phase 0 — Validation spikes (de-risk before committing)

*Empirically confirm the load-bearing unknowns the verdicts flagged so the architecture rests on facts, not hope.*

- Spike A: install official google-colab-cli (pinned), authenticate a real Colab Pro account via its OAuth loopback, allocate a T4 + an A100 session, run code, capture output, sync a file — confirm headless feasibility and whether a stable JSON/machine-readable output mode exists.
- Spike B: confirm the runtime-proxy-token is header-only (X-Colab-Runtime-Proxy-Token) and identify the exact XSRF/X-Goog-Colab-Tunnel contract by observing the official CLI/colab-vscode traffic.
- Spike C: confirm user-OAuth plain-blob .ipynb upload to My Drive round-trips and opens correctly in Colab; reproduce (and thereby avoid) the SA 403 storageQuotaExceeded failure.
- Spike D: confirm whether self-registered colaboratory scope is grantable (default to 'no'); document Python 3.13 interop strategy for invoking the CLI from a 3.11+ core.
- Go/no-go memo updating any architecture assumptions.

### Phase 1 — Core foundation

*Stand up the secret store, auth, provider abstraction contract, CLI, and MCP skeleton.*

- keyring-based secret store with per-account-email keying, >4KB chunking, and a pluggable non-Mac backend.
- OAuth2 user-credential auth via the official CLI loopback, persisted refresh tokens, refresh lifecycle.
- pydantic v2 models for auth, RuntimeProxyInfo, assignment/quota outcomes, provider-capability descriptors, structured kernel outputs.
- Provider-abstraction interface (submit/status/logs/fetch/cancel + notebook/file ops) with capability feature-detection.
- Typer CLI skeleton + FastMCP MCP server skeleton wired to the abstraction (stubbed backend).

### Phase 2 — Colab first-class backend (sanctioned primary)

*Make Colab fully controllable via the official tooling behind the abstraction.*

- google-colab-cli adapter: version-pinned, capability-probed, tolerant output parser with golden-file tests, isolated interpreter env.
- Runtime lifecycle: allocate (variant/accelerator selection), keep-alive, idle/lifetime handling, re-assign, proxy-token refresh; surface DENYLISTED/QUOTA_*/CCU.
- Code execution via jupyter-kernel-client with the corrected header-only auth recipe; structured streaming outputs.
- Drive file sync (user-OAuth, plain-blob .ipynb to My Drive) + checkpoint/resume scaffolding to externalize ephemeral state.
- colab-mcp browser-bridge SECONDARY path for human-in-the-loop interactive sessions.

### Phase 3 — Sanctioned alt-backends + escape hatch

*Deliver survivability and production-grade headless options; contain the reverse-engineered path.*

- Modal Sandbox/Function backend (gVisor isolation, GPU, streaming logs, volumes) with spend/timeout guards.
- Colab Enterprise/Vertex notebookExecutionJobs backend (ADC/service-account auth) for deterministic headless production runs.
- Opt-in, version-gated, disclosed-risk direct /tun/m/* escape hatch + runtime-proxy-token lifecycle, disabled by default, behind contract tests.
- Capability-aware routing/failover so a Colab ban or churn degrades to an alt-backend automatically.

### Phase 4 — Hardening, breadth, and release

*Production polish, broader fallback coverage, and documented ToS posture.*

- Lower-priority fallback backends behind the abstraction (HF Jobs, Kaggle free-GPU, RunPod/vast IaaS with billing watchdog, hyperscaler jobs).
- Optional papermill/nbclient batch .ipynb adapter for code-only backends.
- Contract/drift tests, golden-file CLI parser tests, integration test rig (optionally jupyter_http_over_ws local server as an isolated kernel test target).
- Per-backend ToS/cost/capability documentation; abuse-detection risk disclosure; secret-handling and headless deployment guides; uv/pyproject packaging and release.

---

## 17. Open Decisions for the Owner

These are genuine product forks the spec leaves to you. The spec's *default* answer (baked into the design above) is the sanctioned-primary, Colab-first, provider-abstracted system; the questions below are where you can steer it.

> **✅ Resolved 2026-05-31 — see [`DIRECTIVES.md`](./DIRECTIVES.md) for the binding answers.** In short: **sanctioned default** (official CLI enabled, `/tun/m/*` opt-in) · **Colab Pro is the literal target** · **v1 = Colab + Modal + Vertex** (rest deferred) · **deploy to both Mac and headless Linux/CI** (full secret-backend abstraction up front). Plus a governing directive: the immature `google-colab-cli` must **not** be load-bearing — the native `/tun/m/*` transport is built to first-class quality as a co-primary (just disabled-by-default), so there is no CLI lock-in.

- ToS risk tolerance: The sanctioned default (official google-colab-cli/MCP) on paid Colab Pro is MEDIUM/LOW risk, but opaque abuse-detection bans can still hit a paying account with no appeal and the blast radius is the whole Google account. Are you comfortable defaulting to the sanctioned path only, or do you want the reverse-engineered direct /tun/m/* escape hatch enabled by default despite the higher fragility/abuse-detection exposure? My recommendation: sanctioned default, escape hatch opt-in.
- Headless vs human-in-the-loop priority: Truly autonomous, no-browser operation currently depends on the immature google-colab-cli (v0.5.x); the most stable Colab path (colab-mcp) requires a human-opened logged-in browser tab. If a clean headless server with zero human involvement is a hard requirement TODAY, we may need to weight Modal/Vertex more heavily as the production default and treat consumer Colab as the interactive/dev backend. Which matters more right now — Colab specifically, or reliable headless GPU?
- Single-account vs multi-account: Colab's concurrency caps (412 TooManyAssignmentsError) and explicit ToS ban on multi-account quota circumvention mean we cannot legitimately scale many concurrent agents on one Colab account. Do you need multi-tenant/parallel scale (push that to Modal/Vertex), or is single-account interactive control sufficient?
- Fallback scope to build now: The provider abstraction is the durable win, but each backend is real maintenance surface. Do you want all sanctioned alt-backends (Modal, Vertex, HF Jobs, Kaggle, RunPod/vast, hyperscaler) in v1, or just Colab + Modal + Vertex now with the rest registered-but-deferred? My recommendation: Colab + Modal + Vertex in v1.
- Budget posture for paid backends: Modal/HF/RunPod/Vertex are pay-per-GPU-second with no free tier and real runaway-spend risk for an autonomous agent loop. What hard spend caps / kill-switch behavior do you want enforced by default?
- Is 'Colab Pro' literal or shorthand? Several otherwise-excellent backends (Modal, Vertex, Kaggle) are NOT Colab. If the requirement is literally controlling your existing Colab Pro subscription, those are fallbacks only; if 'Colab Pro' is shorthand for 'a Google-managed/affordable GPU notebook,' the weighting shifts toward the more durable sanctioned alternatives.
- Deployment target for secrets: Will this run primarily on your Mac (Keychain works natively) or on headless Linux servers/CI (needs the SecretService/encrypted-file backend and a passphrase-from-env strategy)? This affects how much of the secret-backend abstraction we build in Phase 1.
