# Architecture

colabctl is layered so the developer/agent surface never depends on a single way of
reaching Colab. The canonical, exhaustive spec is [`SPEC.md`](https://github.com/colabctl/colabctl/blob/main/SPEC.md);
binding decisions are in [`DIRECTIVES.md`](https://github.com/colabctl/colabctl/blob/main/DIRECTIVES.md);
the validation findings (including the keep-alive saga) are in
[`spikes/PHASE0-FINDINGS.md`](https://github.com/colabctl/colabctl/blob/main/spikes/PHASE0-FINDINGS.md).

## Layers

```
            SDK  ·  CLI  ·  MCP server          (developer + agent surfaces)
                       │
        ┌──────────────┴───────────────┐
   TransportAdapter              Backend (provider abstraction)
   (interactive runtimes)        (batch jobs: submit/status/logs/result/cancel)
        │                               │
   cli · native              colab · modal · vertex  ←  BackendRouter (capability + failover)
        │
   auth (ADC) · secrets · observability (logging, retry, spend guard)
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
- **Durability over keep-alive.** The Colab keep-alive RPC is unusable under token auth
  (live-confirmed), so long jobs rely on kernel activity plus checkpoint-to-Drive +
  automatic re-assign/restore.
- **Honest disclosure + spend guards.** Backends report their ToS posture and caveats;
  paid backends enforce a hard timeout ceiling.
