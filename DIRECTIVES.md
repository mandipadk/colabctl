# Owner Directives & Locked Decisions

> Recorded 2026-05-31, after the planning workflow. These **govern implementation** and refine (do not replace) [`SPEC.md`](./SPEC.md). Where a build choice is ambiguous, this file wins.

## Locked decisions

| Fork | Decision |
|------|----------|
| **ToS posture** | **Sanctioned default.** Official `google-colab-cli` / `colab-mcp` is the default-*enabled* Colab transport. The reverse-engineered `/tun/m/*` client ships **disabled-by-default** (opt-in, disclosed-risk). |
| **Primary goal** | **Colab Pro is the literal target backend** — not merely "any affordable GPU." |
| **v1 backend scope** | **Implement Colab + Modal + Vertex.** HF Jobs / Kaggle / RunPod / vast / hyperscaler are registered behind the provider abstraction but **deferred**. |
| **Deploy target** | **Both Mac and headless Linux/CI.** Build the full pluggable secret-store backend abstraction (keyring + SecretService + Windows Credential Manager + age-encrypted file + passphrase-from-env) **up front in Phase 1.** |

## Governing directive — do not let `google-colab-cli` become load-bearing

The owner explicitly rejected *dependence* on the official CLI: it is immature (v0.5.x, recently started, yanked releases, no confirmed stable machine-readable output, Python-3.13-only). The reconciliation with the "sanctioned default" posture is:

1. **The official CLI is the default sanctioned adapter — but it is one adapter among co-equals, never "everything."**
2. **We build our own native Colab transport to first-class production quality:** the `/tun/m/*` assign client (GET-then-POST, XSSI stripping, `X-Goog-Colab-Token` XSRF), `jupyter-kernel-client` websocket execution with the **header-only** `X-Colab-Runtime-Proxy-Token` recipe, and a runtime-proxy-token + assignment lifecycle manager (refresh on `tokenExpiresInSeconds`, re-assign on idle/lifetime, surface `412 TooManyAssignmentsError` and quota `Outcome`).
3. This native transport is a **fully-engineered, contract-tested, co-primary adapter that happens to be disabled-by-default** per the ToS posture — **not** a thin "escape hatch." Treat the spec's "escape hatch" framing as *elevated* to "co-primary, opt-in."
4. **The provider abstraction must guarantee no CLI lock-in:** if the CLI regresses or disappears, the native `/tun/m/*` adapter (opt-in) and the Modal / Vertex backends must be able to carry the product unchanged.
5. **Never make runtime allocation or code execution depend *solely* on shelling out to the CLI.** Every transport sits behind the same `TransportAdapter` interface.

Owner's original framing, preserved as the standing ethos: **"be prepared to write everything from scratch."**

## Always-applicable defaults (carried from the prompt)

- **Best-quality code; no cut corners.** Full typing (strict mypy/pyright), tests, docs, and error handling on every module — not MVP-grade.
- **Durable state is externalized** (Drive/GCS) because runtimes are ephemeral; long jobs checkpoint and resume.
- **Hard spend caps + guaranteed teardown** on all paid alt-backends (Modal/Vertex) by default — an autonomous agent loop must not be able to run up unbounded cost.
- **Abuse-detection ban risk is a disclosed, first-class product fact**, never hidden.
