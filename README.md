# colabctl

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![CI](https://github.com/mandipadk/colabctl/actions/workflows/ci.yml/badge.svg)](https://github.com/mandipadk/colabctl/actions/workflows/ci.yml)

**Drive Google Colab from code, the terminal, or an AI agent** — allocate GPU/TPU
runtimes, run code and notebooks, stream outputs, and move files, **without ever touching
the Colab website.** Submit a long job, close your laptop, and collect the result later —
sessions and jobs are **durable across processes, disconnects, and runtime reclamation.**
And when Colab isn't the right fit, run the same job on Modal, Vertex AI, or Hugging Face
through one interface.

```python
import asyncio
from colabctl import ColabClient

async def main():
    async with ColabClient() as colab:
        async with await colab.allocate(gpu="A100,L4,T4") as gpu:   # tries each in turn
            r = await gpu.run("import torch; print(torch.cuda.get_device_name(0))")
            print(r.text)          # → Tesla T4

asyncio.run(main())
```

> **Status:** alpha. The Colab paths (official-CLI transport + a from-scratch `/tun/m/*`
> transport), durable sessions/jobs, the contents-API file transfer, runtime-direct Drive
> checkpoints, and the Modal backend are **validated against real Colab Pro / accounts**.
> The **browser** transport runs Colab's own (live-captured) ColabMCP tools and is built +
> unit-tested; Vertex / Hugging Face are implemented and unit-tested but **not yet
> live-validated**. See [`docs/plan.md`](./docs/plan.md) and [`ROADMAP.md`](./ROADMAP.md)
> for the honest, detailed status.

## Install

```bash
pip install "colabctl[cli,sdk,native,secrets]"
# or as a CLI tool (exposes `colabctl` and `colabctl-mcp`):
uv tool install "colabctl[cli,sdk]"
```

Bleeding edge from source: `pip install "colabctl[all] @ git+https://github.com/mandipadk/colabctl.git"`.

Extras: `cli`, `sdk`, `native`, `secrets`, `mcp`, `drive`, `modal`, `vertex`, `hf`,
`browser` (or `all`).

## Authenticate (Colab)

The Colab paths use Google Application Default Credentials (ADC) — **one-time per
machine** (the refresh token persists). colabctl wraps the setup for you:

```bash
colabctl auth login     # runs the gcloud ADC login with the exact scopes colabctl needs
colabctl auth status    # account · scopes · Drive quota project · what to fix
```

`auth status` tells you at a glance whether `colaboratory`/`drive.file` are granted and
whether a Drive **quota project** is set. (Doing it by hand instead? `colabctl auth scopes`
prints the `gcloud auth application-default login --scopes=…` command.)

For **runtime-direct Drive checkpoints**, ADC user credentials also need a quota project
with the Drive API enabled (or Drive returns 403):

```bash
gcloud services enable drive.googleapis.com --project=YOUR_PROJECT
gcloud auth application-default set-quota-project YOUR_PROJECT   # colabctl auto-detects it
```

(Other backends use their own credentials — `MODAL_TOKEN_*`, `HF_TOKEN`, GCP for Vertex.)

## Use it

**Python SDK** — allocate a GPU, run code, get typed results, move real-size files:

```python
async with ColabClient() as colab:
    async with await colab.allocate(gpu="A100") as gpu:
        await gpu.upload("train.py", "content/train.py")     # chunked contents-API transfer
        result = await gpu.run("exec(open('content/train.py').read())")
        await gpu.download("content/model.pt", "model.pt")   # ranged streaming download
        await gpu.interrupt()                                # stop a runaway cell, keep the VM
```

**`@remote`** — ship a local function to a GPU and get its return value back:

```python
from colabctl import remote

@remote(gpu="A100")
def train():
    import torch
    return torch.cuda.get_device_name(0)

print(train())          # blocks, runs on an A100, returns the device name
```

**CLI:**

```bash
colabctl run train.py --gpu A100,L4,T4       # one-shot with a fallback ladder
colabctl new --gpu A100 --name myjob         # keep a runtime; attach later (any process)
colabctl exec -s myjob -c "print(2**10)"
colabctl attach myjob                        # reconnect to a session from a fresh shell
colabctl quota                               # compute-unit balance + burn rate
colabctl sessions                            # live runtimes (real status, recovered names)
colabctl gc --release-orphans                # reclaim runtimes nothing is tracking
colabctl job run train.py --backend modal --gpu A100 --req torch   # any backend
colabctl job run train.py --allow colab,modal,runpod --cheapest --budget 5   # cost-routed
colabctl cost --gpu A100 --live              # per-backend $/hr, cheapest first (live feed)
colabctl spend                               # cross-backend USD spend ledger
colabctl notebook run nb.ipynb --param epochs=10 --gpu T4 --out out.ipynb   # papermill-style
colabctl update                              # self-upgrade to the latest PyPI release
```

### Durable, long-running work

Submit a detached job, walk away, and collect it from any process — it survives your
client exiting, the websocket dropping, and (with `--resumable`) the runtime being
reclaimed (it re-allocates and relaunches, your code resumes from its own checkpoint):

```bash
id=$(colabctl -t native job run train.py --detach --resumable --gpu A100,L4,T4)
colabctl -t native job logs -f "$id"     # stream logs; resumes exactly after a disconnect
colabctl -t native job result "$id"      # wait for the exit code + output
```

Checkpoint real model weights straight from the runtime to **your** Google Drive — no
client memory or bandwidth in the path (resumable upload, ranged restore), wired into the
lifecycle manager so a re-assigned runtime is restored automatically.

**From an AI agent (MCP)** — let Claude / Codex drive Colab *and* run durable jobs:

```json
{ "mcpServers": { "colabctl": { "command": "colabctl-mcp" } } }
```

Tools include `allocate_runtime`, `run_code`, `interrupt_runtime`, and the submit→poll
job set (`submit_job`, `job_status`, `job_logs`, `job_result`, `cancel_job`) so an agent
launches long work and does other things while it runs.

## Backends

One job API (`submit / status / logs / result / cancel`) with capability-based routing
and opt-in failover: `colabctl job run --backend colab --allow colab,modal,vertex` tries
each backend in turn, so a Colab outage or quota block degrades to the next instead of
failing. (Failover re-runs the job on the next backend, so use `--allow` for idempotent
work; a job that *ran* but whose code failed is never retried elsewhere.)

| Backend | What it's for | ToS posture | Live-validated |
|---|---|---|---|
| **Colab** (CLI + native) | Your Colab Pro GPUs, interactive or durable batch | sanctioned (native is opt-in) | ✅ |
| **Modal** | gVisor-isolated GPU sandboxes; great for agent code | sanctioned | ✅ |
| **Vertex AI** | Headless, deadline-bound production jobs | sanctioned | ⏳ impl + tests |
| **Hugging Face Jobs** | Durable, cheap GPU jobs | sanctioned | ⏳ impl + tests |

## How it works

colabctl wraps Google's **official** `google-colab-cli`/`colab-mcp` as the sanctioned
default, keeps a **from-scratch `/tun/m/*` transport** as a co-equal opt-in path (so
you're never hostage to an immature dependency), and puts the durable engineering into:

- a **persistent state store** so sessions/jobs outlive the process (attach, truthful
  `stop`, `gc`);
- **detached jobs** that run as supervised processes on the VM — the kernel is a control
  plane, not the data plane — so a dropped connection costs a reconnect, not the job;
- **runtime-direct file transfer** (Jupyter contents/files REST API) and **Drive
  checkpoints**, so real ML state actually moves;
- a **capability-detecting provider abstraction** so the product survives Colab churn and
  abuse-detection bans by routing elsewhere; and a scheduled **canary** that catches
  Google's protocol drift before users do.

- **The 1x→10x plan:** [`docs/plan.md`](./docs/plan.md) · architecture:
  [`docs/architecture.md`](./docs/architecture.md) · binding decisions: [`DIRECTIVES.md`](./DIRECTIVES.md)
- **Docs:** [`docs/`](./docs) (`uvx mkdocs serve`)
- **Contributing:** [`CONTRIBUTING.md`](./CONTRIBUTING.md) · **Roadmap & status:** [`ROADMAP.md`](./ROADMAP.md)

## A note on Terms of Service

colabctl defaults to Google's sanctioned tooling on **paid** Colab Pro, where automated
use is permitted with a positive compute-unit balance. The reverse-engineered native
transport is **disabled by default** (`COLABCTL_ENABLE_NATIVE=1` to opt in). Opaque
abuse-detection bans can still affect any account; colabctl treats that as a disclosed,
first-class fact and lets you fail over to other backends. Don't share/resell access,
and respect each backend's terms.

## License

[Apache-2.0](./LICENSE).
