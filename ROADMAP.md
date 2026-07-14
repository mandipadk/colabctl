# Roadmap

colabctl is in active development. This roadmap records the public support level of each major
surface, the limitations users should plan around, and the maintenance work currently under
consideration.

## Available now

- Interactive Colab allocation and execution through Google's official CLI.
- An opt-in custom Colab transport with streaming Jupyter execution, cross-process attach,
  keep-alive, interrupt, file transfer, and runtime reconciliation.
- A browser transport that works through a logged-in Colab tab.
- Python SDK, CLI, MCP server, notebook execution, and `@remote`.
- A common batch-job interface for Colab, Modal, Vertex AI, Hugging Face Jobs, Kaggle, RunPod,
  and Vast.ai.
- Capability filtering, opt-in backend fallback, catalog-price ordering, spot selection, local
  spend estimates, and an audit ledger.
- Detached Colab processes, job history, log following, cancellation, garbage collection, and
  bounded poll-triggered relaunch for resumable jobs.
- ADC authentication helpers, encrypted or keychain-backed secrets, health checks, and package
  self-update.

## Colab transports

| Transport | Default | Evidence | Current limitations |
|---|---:|---|---|
| Official CLI (`cli`) | Yes | Checked against Colab Pro | Depends on the installed official CLI output contract; some custom features are unavailable |
| Custom (`native`) | No | Allocation, execution, transfer, keep-alive, attach, and teardown checked live | Requires `COLABCTL_ENABLE_NATIVE=1`; provider protocol changes can require an update |
| Browser (`browser`) | No | Colab MCP protocol captured and covered by tests | Needs a logged-in browser tab; allocation and teardown support are limited |

## Batch backends

| Backend | Implementation status | Live evidence | Main limitation |
|---|---|---|---|
| Colab | Implemented and tested | Official and custom transports checked live | Dynamic capacity and usage limits |
| Modal | Implemented and tested | CPU and T4 runs checked live | Provider account required; state is process-local in the current adapter |
| Vertex AI | Implemented and tested | Pending | Logs remain in Cloud Logging; project and staging bucket required |
| Hugging Face Jobs | Implemented and tested | Pending | Provider token required |
| Kaggle | Implemented and tested | Pending | T4 only, no cancel API, best-effort final logs |
| RunPod | Implemented and tested | Pending | Adapter does not retain stdout; outputs need durable storage |
| Vast.ai | Implemented and tested | Pending | Host offers, reliability, and prices vary |

“Tested” means the hermetic suite exercises the adapter with fake or captured provider
responses. “Checked live” means a bounded run succeeded on a real account. Live checks are not
part of CI because CI never uses credentials, network access, or paid compute.

## Known limitations

- Detached Colab jobs survive the submitting process and connection loss while their runtime
  remains alive. Runtime-loss recovery starts when a later `status` or `result` call observes
  the loss.
- `--resumable` relaunches the stored workload. The workload must save a checkpoint outside the
  runtime and load that checkpoint when it starts again.
- Logs stored only on a reclaimed runtime may be incomplete after relaunch.
- A local state file provides cross-process attach and audit history. It does not provide a
  shared or highly available controller.
- Price ordering, `--max-price`, and `--budget` use catalog data and the local estimated-spend
  ledger. Provider invoices, storage, egress, delayed billing, and concurrent external usage can
  differ.
- Backend semantics vary. Some providers do not expose streaming logs, cancellation, exit codes,
  or durable artifacts through their current APIs.
- The browser transport needs an authenticated tab and cannot provide the same headless
  lifecycle controls as the CLI or custom transports.

## Work under consideration

The next public changes will be selected from these maintenance areas after their behavior and
compatibility are reviewed:

- stronger detached-job checkpoint, log, artifact, and cleanup integrity;
- recovery that does not depend on a later client poll;
- one conformance suite for backend failure, cancellation, timeout, and result semantics;
- machine-readable health, status, and error output;
- stricter official CLI compatibility checks;
- repeated live validation with dated evidence for every advertised backend.

The list does not set a release schedule. Each item needs an issue or pull request with
acceptance criteria before implementation begins.

## Development

```bash
uv sync --all-extras
uv run --all-extras pytest -q
uv run --all-extras mypy src
uv run ruff check src tests
uv run ruff format --check src tests
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the contribution workflow.
