# Status & Roadmap

colabctl is **alpha**. This is the honest, detailed status behind the README's one-liner.
The canonical plan is [`SPEC.md`](./SPEC.md) §16; decisions are in [`DIRECTIVES.md`](./DIRECTIVES.md);
validation findings are in [`spikes/PHASE0-FINDINGS.md`](./spikes/PHASE0-FINDINGS.md).

## Live-validation matrix

| Component | Status |
|---|---|
| Colab **CLI** transport (sanctioned default) | ✅ live-validated on real Colab Pro |
| Colab **native** `/tun/m/*` transport (opt-in) | ✅ live-validated (allocate + kernel exec + teardown) |
| **Modal** backend | ✅ live-validated (CPU + T4) |
| **Vertex AI** backend | ⏳ implemented + unit-tested; live-validation pending a GCP project/bucket |
| **Hugging Face Jobs** backend | ⏳ implemented + unit-tested; live-validation pending an HF token |
| **Browser-bridge** transport (colab-mcp model) | ⏳ implemented + unit-tested; needs live validation against Google's frontend |

Tests are offline and need no credentials; live checks live in [`spikes/`](./spikes) and
are run by hand.

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

## Known limitations

- **Keep-alive:** the Colab RuntimeService keep-alive RPC is unusable under token auth
  (live-confirmed); long jobs rely on kernel activity + checkpoint/re-assign.
- **Vertex** stdout goes to Cloud Logging (not captured); `result` returns state + a log link.
- **Browser-bridge** is non-headless (needs a logged-in tab) and depends on Google's
  colab-mcp frontend protocol.

## Develop

```bash
uv sync --all-extras
uv run pytest            # offline, no credentials
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy src          # strict
```

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for conventions.
