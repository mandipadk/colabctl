# Examples

The Python snippets below are **executed when these docs are built** (via `markdown-exec`), so
they can't silently rot — a broken example fails the docs CI job. The shell flows that need a
live Colab account are shown as plain blocks.

## Compare GPU cost across backends

`colabctl.cost` ranks backends by `$/hr` from a built-in table (the live feed is opt-in). No
account or network needed:

```python exec="true" source="material-block" result="text"
import asyncio
from colabctl.cost import PriceCatalog
from colabctl.models import Accelerator

rows = asyncio.run(PriceCatalog().per_backend(Accelerator.A100))
for r in rows:
    print(f"{r.provider:<8} ${r.rate():>5.2f}/hr   (spot ${r.rate(spot=True):.2f})")
```

## Build a cost-capped, spot-preferring job spec

`JobSpec` carries the cost guards the router enforces — a per-job `$/hr` ceiling and the spot
tier — both fail-closed:

```python exec="true" source="material-block" result="text"
from colabctl.backends.base import JobSpec
from colabctl.models import Accelerator

spec = JobSpec(
    code="train()",
    accelerator=Accelerator.A100,
    max_price_usd_hr=2.0,   # refuse any backend pricier than $2/hr
    spot=True,              # prefer the interruptible tier
)
print(f"{spec.accelerator.value}  cap=${spec.max_price_usd_hr}/hr  spot={spec.spot}")
```

## Per-accelerator spot interruption risk

```python exec="true" source="material-block" result="text"
from colabctl.tracking import resolve_tracking_env

# Experiment tracking is pure env-injection — creds come from the secret store, never code.
env = resolve_tracking_env("wandb", "demo-job", secret_get=lambda _a: None)  # no key -> fail-open
print(sorted(env))  # the job is tagged + W&B disabled when no key is present
```

## Durable, auto-resuming GPU job (CLI)

Needs a Colab account. The job runs as a supervised process on the runtime and **auto-resumes
from its checkpoint** if the runtime is reclaimed — poll it from any shell:

```bash
ID=$(colabctl job run train.py --backend colab --gpu A100 --detach --resumable)
colabctl job status "$ID"          # cross-process; safe to close your laptop
colabctl job logs "$ID" --follow   # stitched across auto-resume incarnations
colabctl job result "$ID"
```

## Cost-routed run with a hard budget + cross-backend failover

```bash
colabctl job run train.py --gpu A100 \
  --allow colab,modal,runpod,vast --cheapest --budget 10 --track wandb
# routes to the cheapest qualifying backend, refuses to launch above $10 (fail-closed),
# fails over on infra/preemption errors, and records the W&B run URL in `colabctl audit`.
```

## Ship a local function to a GPU (`@remote`)

```python
from colabctl.sdk import remote

@remote(gpu="A100", requirements=["torch"], track="wandb")
def train(epochs: int) -> float:
    import torch  # runs on the Colab runtime, not locally
    ...
    return best_accuracy

acc = train(epochs=10)   # blocks; or `await train.aio(...)` inside an event loop
```
