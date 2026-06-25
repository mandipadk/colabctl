---
name: colabctl
description: >-
  Durable, cost-aware GPU orchestration over Google Colab and Modal/Vertex/Hugging
  Face/Kaggle/RunPod/Vast via the colabctl CLI and MCP server. Use when the user wants to run
  Python code or notebooks on a remote GPU, allocate or attach a Colab runtime, submit durable
  detached or auto-resuming jobs, check Colab compute-unit quota, compare GPU $/hr cost across
  backends, track spend, route to the cheapest or a spot backend, or manage remote sessions,
  secrets, and file transfers. Covers run/exec, job run/status/logs/result, notebook run,
  attach/sessions, quota, cost/spend/spot-risk, secret, and auth.
allowed-tools: Bash(colabctl:*), Bash(colabctl-mcp:*)
metadata:
  project: colabctl
---

# colabctl — remote GPU orchestration

colabctl runs code, notebooks, and durable batch jobs on remote GPUs — primarily Google Colab,
plus Modal, Vertex AI, Hugging Face Jobs, Kaggle, RunPod, and Vast.ai — from the terminal, the
Python SDK, or an MCP server. Its differentiator is **durability**: a detached job runs as a
supervised process on the runtime and **auto-resumes from its checkpoint** if the runtime is
reclaimed, so long training survives Colab's idle/lifetime limits.

## How to drive it (pick the right surface)

1. **If the `colabctl` MCP server is connected, prefer its tools** for allocate/run/job
   operations — they return structured JSON you can chain. See `references/mcp-tools.md` for the
   tool↔command map.
2. **Otherwise use the `colabctl` CLI** via Bash (pre-approved here).
3. **Always confirm exact flags** with `colabctl <command> --help` before running — the CLI is
   the source of truth and evolves; do not assume flags.

## Command map

- **Interactive runtimes:** `run` (allocate → run a file → release), `exec` (run code on an
  existing session), `new` (allocate and leave running), `sessions`, `status`, `stop`,
  `attach` (reconnect from another process, native transport), `upload`, `download`,
  `keepalive`, `interrupt`, `gc`.
- **Durable batch jobs (the moat):** `job run [--detach] [--resumable]`, `job status`,
  `job logs [--follow]`, `job result`, `job history`, `job cancel`, `job list`, `job gc`,
  `job rm`, `job backends`.
- **Notebooks:** `notebook run nb.ipynb --param k=v --gpu T4 [--out executed.ipynb]`
  (papermill-style parameterized execution).
- **Cost & spend:** `cost --gpu A100 [--spot] [--live]` (per-backend $/hr, cheapest first),
  `spend` (USD ledger), `spot-risk` (interruption-rate reference). Cost flags on `job run`:
  `--cheapest`, `--max-price`, `--budget`, `--spot`, `--allow a,b,c`.
- **Quota / auth / secrets / self-update:** `quota`, `auth login|status|scopes`, `secret`,
  `update`, `version`.

## Key concepts (load `references/commands.md` for full detail)

- **Transports** (`-t`): `cli` (default; drives Google's `colab` binary, bundled in
  `colabctl[cli]`), `native` (direct API, opt-in via `COLABCTL_ENABLE_NATIVE=1`, no external
  binary), `browser` (logged-in Colab tab). Native/browser need no external binary.
- **Backends** (`--backend` / `--allow`): colab, modal, vertex, hf, kaggle, runpod, vast. The
  router fails over across `--allow` backends on infra errors and (with `--cheapest`/`--spot`)
  routes by cost; spot preemption fails over too.
- **Durable jobs:** add `--detach --resumable` so a reclaimed runtime auto-resumes from the
  workload's own checkpoint; poll later from any shell with `job status/logs/result`.
- **Cost safety:** `--max-price` (per-job $/hr ceiling) and `--budget` (cumulative USD cap) are
  **fail-closed** — they refuse to launch rather than overspend.

## When to reach for what

- "Run this on a GPU / in Colab" → `colabctl run file.py --gpu T4` (or MCP `run_code`).
- "Train for hours without babysitting" → `colabctl job run train.py --backend colab --gpu A100
  --detach --resumable`, then poll `job status`/`job result`.
- "Cheapest place to run an A100" → `colabctl cost --gpu A100 --live`; then
  `job run ... --allow colab,modal,runpod,vast --cheapest --budget 5`.
- "Run a parameterized notebook" → `colabctl notebook run nb.ipynb --param epochs=10 --gpu T4`.

For the complete command catalog see `references/commands.md`; for end-to-end worked flows see
`examples/recipes.md`; for the MCP-tool mapping see `references/mcp-tools.md`.
