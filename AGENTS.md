# AGENTS.md — working on colabctl

Guidance for AI agents (and humans) contributing to the colabctl codebase. This is for working
*on* colabctl; to teach an agent how to *use* the installed CLI, install the Agent Skill with
`colabctl skill install` (ships in the wheel under `src/colabctl/_skills/`).

## What this is

colabctl is a Python package, CLI (`colabctl`), and MCP server (`colabctl-mcp`) for interactive
and batch GPU work across Google Colab, Modal, Vertex AI, Hugging Face Jobs, Kaggle, RunPod, and
Vast.ai. Detached Colab processes survive client disconnects on the same runtime. For jobs marked
resumable, a later poll can relaunch the stored workload after runtime loss; the application must
restore its own external checkpoint. The cost engine provides catalog-price routing and local
spend estimates.

## Setup & the verification gate

Use `uv`. Before every commit, all four must be clean:

```bash
uv run --all-extras pytest -q        # ~864 tests, hermetic (no network/Colab needed)
uv run --all-extras mypy src         # strict; must say "no issues"
uv run ruff check src tests          # lint
uv run ruff format src tests         # format
```

Requires Python 3.12+ (the bundled google-colab-cli's floor).

## Architecture (where things live)

- `transport/` — **interactive** runtimes (allocate a warm GPU, run cells). `cli` (drives
  Google's `colab` binary, the default), `native` (custom `/tun/m/*`, opt-in via
  `COLABCTL_ENABLE_NATIVE=1`), `browser`. A `TransportAdapter`.
- `backends/` — **batch jobs** (submit → poll → result) across providers. `Backend` +
  `BackendRouter` (capability routing, infra-error failover, cheapest-first cost routing).
- `jobs/` — the durable **detached** Colab backend: supervised on-VM process, cross-process
  attach, auto-resume on reclamation (bounded by `AllocationGate` so it can't bill forever).
- `cost/` — the price model (`GpuPrice`, `PriceSource`, `PriceCatalog`, static table), live
  feeds (`feeds.py` ComputePrices), and spot risk (`risk.py` AWS Spot Advisor).
- `state/` — atomic, lock-guarded JSON store (jobs, sessions, spend ledger).
- `sdk/` — the `ColabClient` + `@remote` decorator. `mcp_server.py` — the FastMCP server.

## Conventions

- **Tests are hermetic** — no live network/Colab/money. External APIs are behind injectable
  `fetch`/`request`/`graphql` callables; tests pass canned data. Spot backends are mock-tested.
- **Single-source version**: `__version__` in `src/colabctl/__init__.py` (hatchling reads it).
- Keep core deps light (pydantic + httpx); everything else is a lazy-imported extra.
- New backend → register in `backends/factory.py` (`BACKEND_NAMES` + `build_backend`) and add
  static prices in `cost/price.py`; update the backend-list test expectations.

## Release

Bump `__version__` + CHANGELOG, commit, push `main`, then tag `vX.Y.Z` and push the tag — the
`Publish to PyPI` GitHub Action publishes via Trusted Publishing. Create a GitHub release with
the built `dist/` artifacts. Run the pre-push audit for secrets and personal paths.
