# colabctl

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org)
[![CI](https://github.com/mandipadk/colabctl/actions/workflows/ci.yml/badge.svg)](https://github.com/mandipadk/colabctl/actions/workflows/ci.yml)

**Drive Google Colab from code, the terminal, or an AI agent** — allocate GPU/TPU
runtimes, run code and notebooks, stream outputs, and sync files, **without ever
touching the Colab website.** And when Colab isn't the right fit, run the same job on
Modal, Vertex AI, or Hugging Face through one interface.

```python
import asyncio
from colabctl import ColabClient

async def main():
    async with ColabClient() as colab:
        async with await colab.allocate(gpu="T4") as gpu:
            r = await gpu.run("import torch; print(torch.cuda.get_device_name(0))")
            print(r.text)          # → Tesla T4

asyncio.run(main())
```

> **Status:** alpha. The Colab paths (official-CLI transport + a from-scratch
> `/tun/m/*` transport) and the Modal backend are **validated against real accounts**;
> Vertex / Hugging Face / the browser-bridge are implemented and unit-tested but **not
> yet live-validated**. See [`ROADMAP.md`](./ROADMAP.md) for the honest, detailed status.

## Install

> Not on PyPI yet — install from GitHub (PyPI publishing is wired up and goes live on
> the first tagged release; then `pip install colabctl` will work).

```bash
pip install "colabctl[cli,sdk,native,secrets] @ git+https://github.com/mandipadk/colabctl.git"
# or as a CLI tool (exposes `colabctl` and `colabctl-mcp`):
uv tool install "colabctl[cli,sdk] @ git+https://github.com/mandipadk/colabctl.git"
```

Extras: `cli`, `sdk`, `native`, `secrets`, `mcp`, `drive`, `modal`, `vertex`, `hf`,
`browser` (or `all`).

## Authenticate (Colab)

The sanctioned Colab path uses Google Application Default Credentials:

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory,\
https://www.googleapis.com/auth/drive.file
```

(Other backends use their own credentials — `MODAL_TOKEN_*`, `HF_TOKEN`, GCP for Vertex.)

## Use it

**Python SDK** — allocate a GPU, run code, get typed results, auto-release:

```python
async with ColabClient() as colab:
    async with await colab.allocate(gpu="A100") as gpu:
        await gpu.upload("train.py", "content/train.py")
        result = await gpu.run("exec(open('content/train.py').read())")
        await gpu.download("content/model.pt", "model.pt")
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
colabctl run train.py --gpu T4               # one-shot: allocate → run → release
colabctl new --gpu A100 --name myjob         # keep a runtime; attach later
colabctl exec -s myjob -c "print(2**10)"
colabctl job run train.py --backend modal --gpu A100 --req torch   # any backend
colabctl job backends                        # list backends + capabilities
```

**From an AI agent (MCP)** — let Claude / Codex drive Colab *and* run jobs on
Modal/Vertex/HF:

```json
{ "mcpServers": { "colabctl": { "command": "colabctl-mcp" } } }
```

## Backends

One job API (`submit / status / logs / result / cancel`) with capability-based routing
and automatic failover — a Colab outage or quota block degrades to another backend
instead of failing.

| Backend | What it's for | ToS posture | Live-validated |
|---|---|---|---|
| **Colab** (CLI + native) | Your Colab Pro GPUs, interactive or batch | sanctioned (native is opt-in) | ✅ |
| **Modal** | gVisor-isolated GPU sandboxes; great for agent code | sanctioned | ✅ |
| **Vertex AI** | Headless, deadline-bound production jobs | sanctioned | ⏳ impl + tests |
| **Hugging Face Jobs** | Durable, cheap GPU jobs | sanctioned | ⏳ impl + tests |

## How it works

colabctl wraps Google's **official** `google-colab-cli`/`colab-mcp` as the sanctioned
default, keeps a **from-scratch `/tun/m/*` transport** as a co-equal opt-in path (so
you're never hostage to an immature dependency), and puts the durable engineering into
a **capability-detecting provider abstraction** so the product survives Colab churn and
abuse-detection bans by routing elsewhere.

- **Architecture:** [`SPEC.md`](./SPEC.md) · design decisions: [`DECISIONS.md`](./DECISIONS.md) · research: [`RESEARCH.md`](./RESEARCH.md)
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
