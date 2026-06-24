# Status & Roadmap

colabctl is **alpha**. This is the honest, detailed status behind the README's one-liner.
The current execution plan and per-phase status is [`docs/plan.md`](./docs/plan.md);
binding decisions are in [`DIRECTIVES.md`](./DIRECTIVES.md);
live findings are in [`spikes/PHASE0-FINDINGS.md`](./spikes/PHASE0-FINDINGS.md) and
[`spikes/PHASE-A-FINDINGS.md`](./spikes/PHASE-A-FINDINGS.md).

## Live-validation matrix

| Component | Status |
|---|---|
| Colab **CLI** transport (sanctioned default) | ✅ live-validated on real Colab Pro |
| Colab **native** `/tun/m/*` transport (opt-in) | ✅ live-validated (allocate + kernel exec + transfer + teardown) |
| **Durable sessions** — state store, cross-process attach, `gc` | ✅ offline + live (attach via GET-only refresh verified) |
| **Detached jobs** — kernel-as-control-plane, follow, auto-resume | ✅ offline (real-subprocess lifecycle); substrate live via canary |
| **File transfer** — contents API, chunked upload + ranged download | ✅ live-validated (multi-chunk round-trip, byte-perfect) |
| **Runtime-direct Drive checkpoints** | ✅ live-validated (5 MiB runtime→Drive→runtime, SHA-256 match) |
| Colab **browser** transport — ColabMCP, sanctioned, keep-alive | ✅ protocol live-captured + built; `-t browser` wired |
| **Modal** backend | ✅ live-validated (CPU + T4) |
| **Vertex AI** backend | ⏳ implemented + unit-tested; live-validation pending a GCP project/bucket |
| **Hugging Face Jobs** backend | ⏳ implemented + unit-tested; live-validation pending an HF token |

Tests are offline and need no credentials; live checks live in [`spikes/`](./spikes) and
are run by hand (plus a weekly drift/health **canary**, `spikes/canary.py`).

## Phases (vs. SPEC §16)

- **Phase 0 — Validation** ✅ — spikes confirmed the sanctioned path works; surfaced the
  keep-alive limitation (no token-auth keep-alive RPC → kernel-activity + checkpoint/re-assign).
- **Phase 1 — Core foundation** ✅ — secret store, auth, domain models, provider-abstraction
  contract, CLI, MCP.
- **Phase 2 — Colab first-class** ✅ — CLI adapter (golden-tested, version-probed), native
  transport (streaming execution, proxy-token expiry handling), Drive sync, lifecycle
  manager, browser-bridge.
- **Phase 3 — Alt-backends + escape hatch** ✅ — Modal, Vertex, opt-in-gated native escape
  hatch with contract tests, capability routing + failover.
- **Phase 4 — Hardening, breadth, release** — observability, spend guards, CI, packaging,
  docs ✅; **HF Jobs** ✅; **remaining:** Kaggle, RunPod/vast, a papermill adapter, a
  `jupyter_http_over_ws` integration rig, and per-backend ToS/deploy guides.
- **Phase 5 — Durable Colab fabric (the 1x→10x increment)** ✅ — persistent state store +
  native attach/`gc`; **detached jobs** (the kernel as a control plane) with `--follow` +
  auto-resume on reclamation; **real-size transfer** (contents API) + **runtime-direct Drive
  checkpoints**; non-disruptive proxy-token refresh; auth UX (`colabctl auth login/status`),
  `quota` + spend guard, allocation ladder, `gc`-on-412; a scheduled drift **canary**; and
  the sanctioned **browser transport** on Colab's ColabMCP protocol. Both the native
  (tunnel-ping) and browser (cell-activity) transports now have a working keep-alive. Full
  detail and per-phase status: [`docs/plan.md`](./docs/plan.md).
- **Remaining:** Track B keep-alive (cookie/SAPISIDHASH — now optional; the browser transport
  covers keep-alive), a live ≥90-min idle measurement, Vertex/HF live validation,
  Kaggle/RunPod, and chunked client-side `DriveSync`.

## Known limitations

- **Keep-alive:** the native transport now has a working **headless token-auth keep-alive** —
  the tunnel ping (`/tun/m/<endpoint>/keep-alive/?authuser=0` + `X-Colab-Tunnel: Google`),
  live-validated to hold a runtime 100+ min past idle with zero activity. The legacy
  RuntimeService RPC stays unusable under token auth. Colab's hard 12/24h cap still applies,
  so durable long jobs rely on checkpoint/re-assign + **auto-resume** regardless.
- **Drive checkpoints:** ADC user credentials need a quota project with the Drive API enabled
  (`colabctl auth status` flags it; auto-detected once `set-quota-project` is run).
- **Browser transport** is non-headless (needs a logged-in tab) and cannot terminate the VM
  (close the tab to release it).
- **Vertex** stdout goes to Cloud Logging (not captured); `result` returns state + a log link.

## Develop

```bash
uv sync --all-extras
uv run pytest            # offline, no credentials
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src          # strict
```

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for conventions.
