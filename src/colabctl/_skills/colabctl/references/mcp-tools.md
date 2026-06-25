# colabctl MCP tools ↔ CLI commands

The `colabctl-mcp` server exposes these tools (bare names; if multiple MCP servers are
connected, confirm the colabctl server is the one providing them). Prefer the MCP tool over the
CLI when the server is connected — it returns structured JSON you can chain. If no colabctl MCP
server is connected, use the equivalent CLI command instead.

## Interactive runtimes

| MCP tool | Args | CLI equivalent |
|---|---|---|
| `allocate_runtime` | `gpu="T4"`, `name?` | `colabctl new -s NAME --gpu T4` |
| `run_code` | `session`, `code`, `timeout?` | `colabctl exec -s NAME --code ...` |
| `list_runtimes` | — | `colabctl sessions` |
| `runtime_status` | `session` | `colabctl status -s NAME` |
| `upload_file` | `session`, `local_path`, `remote_path` | `colabctl upload -s NAME LOCAL REMOTE` |
| `download_file` | `session`, `remote_path`, `local_path` | `colabctl download -s NAME REMOTE LOCAL` |
| `interrupt_runtime` | `session` | `colabctl interrupt NAME` |
| `stop_runtime` | `session` | `colabctl stop -s NAME` |

## Jobs (one-shot + detached/durable)

| MCP tool | Args | CLI equivalent |
|---|---|---|
| `run_job` | `code/file`, `backend`, `gpu`, `requirements?`, `allow?` | `colabctl job run ... --backend B --gpu G [--allow ...]` |
| `run_notebook` | `notebook`, `params?`, `gpu`, `backend?` | `colabctl notebook run nb.ipynb --param k=v --gpu G` |
| `list_backends` | — | `colabctl job backends` |
| `submit_job` | `code/file`, `gpu`, `resumable?` | `colabctl job run ... --detach [--resumable]` |
| `job_status` | `job_id` | `colabctl job status ID` |
| `job_logs` | `job_id`, `offset?` | `colabctl job logs ID` |
| `job_result` | `job_id` | `colabctl job result ID` |
| `cancel_job` | `job_id` | `colabctl job cancel ID` |

## Notes

- `run_job` runs synchronously and returns the result; `submit_job` is the durable
  detached path (returns an id; poll with `job_status`/`job_logs`/`job_result`).
- Cost flags (`--cheapest`, `--max-price`, `--budget`, `--spot`) currently live on the **CLI**
  `job run`; for cost-routed runs prefer the CLI even when MCP is connected.
- Exact tool argument names can change — if a tool call is rejected, re-read the MCP tool schema
  the server advertises rather than trusting this table.
