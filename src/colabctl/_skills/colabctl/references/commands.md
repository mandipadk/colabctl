# colabctl command catalog

Full reference for the `colabctl` CLI. Always verify exact flags with `colabctl <cmd> --help`
(this file is a map; the CLI is authoritative). Global option: `-t/--transport {cli|native|browser}`.

## Contents
- Interactive runtimes
- Durable batch jobs
- Notebooks
- Cost & spend
- Quota, auth, secrets, maintenance
- Backends & accelerators

## Interactive runtimes

| Command | What it does |
|---|---|
| `colabctl run FILE --gpu T4 [--keep]` | Allocate a runtime, run a local `.py` file, release it (unless `--keep`). |
| `colabctl exec -s NAME [--code C]` | Run code (from `--code` or stdin) on an existing session. |
| `colabctl new -s NAME --gpu T4` | Allocate a runtime and leave it running; attach later with `exec -s NAME`. |
| `colabctl sessions` | List active sessions (real status, recovered names). |
| `colabctl status -s NAME` | Show one session's status. |
| `colabctl stop -s NAME` | Stop a session and release its runtime. |
| `colabctl attach NAME` | Reconnect to a session created by another process (native transport). |
| `colabctl upload -s NAME LOCAL REMOTE` | Upload a file to the runtime. |
| `colabctl download -s NAME REMOTE LOCAL` | Download a file from the runtime. |
| `colabctl keepalive -s NAME` | Send a keep-alive (native transport). |
| `colabctl interrupt NAME` | Interrupt the running cell without killing the runtime (native). |
| `colabctl gc [--release-orphans]` | Reconcile local state vs live runtimes; reclaim orphans, prune dead records. |

## Durable batch jobs (the durability moat)

`colabctl job ...` runs batch jobs across backends. A **detached** job runs as a supervised
process on the runtime; with `--resumable` it **auto-resumes from its own checkpoint** if the
runtime is reclaimed (bounded by an incarnation cap to avoid cost runaway).

| Command | What it does |
|---|---|
| `colabctl job run FILE --backend colab --gpu A100 [--detach] [--resumable]` | Run a job; `--detach` returns an id immediately. |
| `colabctl job run ... --allow colab,modal,vertex` | Fail over across backends on infra errors (idempotent jobs only). |
| `colabctl job run ... --cheapest --max-price 3 --budget 10 --spot` | Cost-routed: cheapest qualifying backend, fail-closed caps, spot tier. |
| `colabctl job status ID` | Current state (cross-process). |
| `colabctl job logs ID [--follow]` | Print/stream logs (stitched across auto-resume incarnations). |
| `colabctl job result ID` | Wait for completion, print the result. |
| `colabctl job history ID` | State-transition timeline (when/why each change, which incarnation). |
| `colabctl job cancel ID` | Cancel a running detached job. |
| `colabctl job list` | List detached jobs in the local state store. |
| `colabctl job gc [--ttl-hours N] [--no-reconcile]` | Reconcile dead jobs → FAILED; prune stale terminal records. |
| `colabctl job rm ID` | Delete one job record (does not touch the runtime). |
| `colabctl job backends` | List backends and their capabilities. |

## Notebooks

| Command | What it does |
|---|---|
| `colabctl notebook run nb.ipynb --param k=v --gpu T4 [--detach] [--out executed.ipynb]` | Papermill-style parameterized notebook execution on a remote GPU; emits an executed `.ipynb`. |

## Cost & spend (Phase 2 cost engine)

| Command | What it does |
|---|---|
| `colabctl cost --gpu A100 [--spot] [--allow ...] [--live]` | Per-backend `$/hr`, cheapest first. `--live` overlays the cached market feed; default is the offline static table. |
| `colabctl spend [--days N]` | Cross-backend estimated USD spend ledger. |
| `colabctl spot-risk [--gpu A100]` | Per-accelerator spot interruption-rate + savings (AWS reference, directional). |

`job run` cost flags: `--cheapest` (route by price), `--spot` (prefer interruptible tier),
`--max-price N` (per-job $/hr ceiling, fail-closed), `--budget N` (cumulative USD cap, fail-closed),
`--allow a,b,c` (candidate/failover set).

## Quota, auth, secrets, maintenance

| Command | What it does |
|---|---|
| `colabctl quota` | Colab compute-unit balance, burn rate, runway, entitled accelerators. |
| `colabctl auth login` / `auth status` / `auth scopes` | Set up / inspect Colab/Drive ADC credentials. |
| `colabctl secret ...` | Manage secrets (OS keychain / encrypted file). Run `colabctl secret --help`. |
| `colabctl update [--check]` | Upgrade colabctl to the latest PyPI release (auto-detects uv-tool vs pip). |
| `colabctl version` | Print the version. |

## Backends & accelerators

- **Backends:** `colab` (default), `modal`, `vertex`, `hf`, `kaggle`, `runpod`, `vast`.
  RunPod and Vast offer a **spot/interruptible** tier (`--spot`, needs a `--max-price` bid).
- **Accelerators (`--gpu`):** `T4`, `L4`, `A100`, `H100` (and `none` for CPU); some backends
  also TPU. Not every backend has every accelerator — `colabctl cost --gpu X` shows coverage.
- **Transports (`-t`):** `cli` (default, drives the bundled `colab` binary), `native`
  (direct API, opt-in `COLABCTL_ENABLE_NATIVE=1`, no external binary, adds detached jobs +
  cross-process attach + auto-resume), `browser` (logged-in Colab tab).
