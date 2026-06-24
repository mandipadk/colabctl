# Architecture

colabctl is layered so the developer/agent surface never depends on a single way of
reaching Colab. The execution plan is in [`docs/plan.md`](https://github.com/mandipadk/colabctl/blob/main/docs/plan.md);
binding decisions are in [`DIRECTIVES.md`](https://github.com/mandipadk/colabctl/blob/main/DIRECTIVES.md);
the validation findings (including the keep-alive saga) are in
[`spikes/PHASE0-FINDINGS.md`](https://github.com/mandipadk/colabctl/blob/main/spikes/PHASE0-FINDINGS.md).

## Layers

```
            SDK  ·  CLI  ·  MCP server          (developer + agent surfaces)
                       │
        ┌──────────────┴───────────────┐
   TransportAdapter              Backend (provider abstraction)
   (interactive runtimes)        (batch jobs: submit/status/logs/result/cancel)
        │                               │
   cli · native · browser    colab · modal · vertex  ←  BackendRouter (capability + failover)
        │
   state store · auth (ADC) · secrets · observability (logging, retry, spend guard, drift canary)
```

## Key decisions

- **Sanctioned-default, no CLI lock-in.** The official `google-colab-cli` is the
  default Colab transport, but the from-scratch native `/tun/m/*` transport is a
  co-equal, opt-in implementation — so the product survives CLI churn.
- **Two complementary abstractions.** `TransportAdapter` models *interactive* warm-GPU
  runtimes; `Backend` models *batch* jobs. Colab is exposed as both.
- **Survivability via routing.** `BackendRouter` selects by capability and fails over
  on infrastructure errors (a Colab ban/outage degrades to Modal/Vertex), but never
  re-runs a job whose user code merely failed.
- **Durable across processes.** A local **state store** records sessions and jobs, so a
  runtime created in one process is attachable from another, `stop` never silently leaks,
  and `gc` reclaims orphans. **Detached jobs** run as supervised processes on the VM (the
  kernel is a control plane, not the data plane), so a dropped connection costs a reconnect,
  not the job — and a reclaimed runtime triggers auto-resume from a Drive checkpoint.
- **Keep-alive, resolved per transport.** The legacy RuntimeService RPC is unusable under
  token auth, but the **native** transport keeps a runtime alive headlessly with the tunnel
  keep-alive ping (`/tun/m/<endpoint>/keep-alive/?authuser=0` + `X-Colab-Tunnel: Google`,
  the google-colab-cli recipe), live-validated to hold a runtime 100+ min past idle with no
  activity; the **browser** transport keeps *its* runtime alive with genuine cell activity in
  the logged-in tab. Colab's hard 12/24h cap still applies, so durable long jobs rely on
  checkpoint + **auto-resume** regardless of keep-alive.
- **Real-size data movement.** Files move over the Jupyter contents/files REST API (chunked
  upload, ranged download); checkpoints go **runtime-direct to Drive** (the VM uploads, no
  client in the path) so real ML state can actually be persisted.
- **Honest disclosure + spend guards.** Backends report their ToS posture and caveats; paid
  backends enforce a hard timeout ceiling; a pre-allocation spend guard refuses to burn a
  zero compute-unit balance.
