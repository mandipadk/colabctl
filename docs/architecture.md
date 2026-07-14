# Architecture

colabctl separates interactive runtimes from batch jobs. Callers can use one developer-facing
surface while each provider keeps its own allocation, execution, status, and cleanup behavior.

## Layers

```text
               Python SDK · CLI · MCP server
                            │
          ┌─────────────────┴─────────────────┐
    TransportAdapter                       Backend
    interactive runtimes                   batch jobs
          │                                  │
    cli · native · browser       colab · modal · vertex · hf
                                  kaggle · runpod · vast
                                             │
                                      BackendRouter

        auth · secrets · state · cost · audit · observability
```

`TransportAdapter` models a warm interactive runtime: allocate it, execute code, transfer files,
inspect it, and release it. `Backend` models a batch job: submit, inspect status, fetch logs,
collect a result, and cancel. Colab implements both shapes.

## Colab transports

- The official `google-colab-cli` transport is the default and ships with the `cli` extra.
- The custom `/tun/m/*` transport uses the CLI name `native`. It has no external binary
  dependency and adds cross-process attach, streaming kernel output, keep-alive, interrupt, and
  file transfer. It is disabled until the user sets `COLABCTL_ENABLE_NATIVE=1` or opts in through
  the Python API.
- The browser transport connects through Colab's MCP tools in a logged-in tab. It serves
  interactive browser workflows and has narrower allocation and teardown support.

## Persistent local state

The state store records sessions, detached jobs, audit entries, and spend estimates in an
atomic, lock-guarded JSON document. A runtime created by one process can be attached or stopped
by another process. Reconciliation compares local records with the provider's live assignments,
and garbage collection can remove stale records or release untracked runtimes when requested.

Credentials do not belong in the state document. colabctl keeps them in the OS keychain or an
encrypted file when those stores are configured.

## Detached jobs

The detached Colab backend launches the user's code as a supervised operating-system process on
the runtime. The Jupyter kernel starts and monitors that process, but the process continues after
the client disconnects.

The local record stores the job specification and runtime identity. When `status` or `result`
observes that a resumable job lost its runtime, colabctl can allocate another runtime and relaunch
the specification. The application remains responsible for writing checkpoints to external
storage and restoring them on startup. Recovery currently depends on a later client poll, and
runtime-local log bytes can be lost with the runtime.

## Routing and costs

`BackendRouter` filters providers by accelerator capability and can try them in an explicit
order. Automatic fallback re-executes the job, so callers must restrict it to idempotent
workloads. The cost catalog can order candidates and filter them by a catalog hourly price.

Catalog prices and the local spend ledger are estimates. They do not replace provider invoices
or account for every storage, egress, concurrency, and delayed-billing charge.

## Data movement

The custom transport moves files through the runtime's Jupyter contents and files APIs. Uploads
use chunks, and downloads use HTTP ranges. Drive helpers can move checkpoints between the
runtime and a user-owned Drive location without routing the bytes through the local client.

## Provider boundaries

Every backend owns its provider client and converts provider states into the common job model.
Tests inject fake HTTP, GraphQL, SDK, and command clients so the full suite remains hermetic. The
public support level of each adapter appears in [Backends](backends.md) and the
[public roadmap](https://github.com/mandipadk/colabctl/blob/main/ROADMAP.md).
