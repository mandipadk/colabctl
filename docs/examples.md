# Examples

The Python snippets marked for execution run during the documentation build. Shell examples that
need a provider account remain plain code blocks.

## Compare catalog prices

`PriceCatalog` returns the known hourly rates without allocating a runtime:

```python exec="true" source="material-block" result="text"
import asyncio
from colabctl.cost import PriceCatalog
from colabctl.models import Accelerator

rows = asyncio.run(PriceCatalog().per_backend(Accelerator.A100))
for row in rows:
    print(f"{row.provider:<8} ${row.rate():>5.2f}/hr   (spot ${row.rate(spot=True):.2f})")
```

Catalog data supports comparison and admission filtering. It does not include every provider
charge or guarantee the final invoice.

## Build a price-filtered job specification

```python exec="true" source="material-block" result="text"
from colabctl.backends.base import JobSpec
from colabctl.models import Accelerator

spec = JobSpec(
    code="train()",
    accelerator=Accelerator.A100,
    max_price_usd_hr=2.0,
    spot=True,
    timeout=3600,
)
print(f"{spec.accelerator.value}  catalog ceiling=${spec.max_price_usd_hr}/hr  spot={spec.spot}")
```

The router excludes catalog rows above `max_price_usd_hr`. Spot instances can be interrupted,
and the provider's billed total can include charges outside that hourly row.

## Resolve tracking settings without a stored secret

```python exec="true" source="material-block" result="text"
from colabctl.tracking import resolve_tracking_env

env = resolve_tracking_env("wandb", "demo-job", secret_get=lambda _alias: None)
print(sorted(env))
```

When no W&B credential is available, colabctl disables W&B for the job instead of placing a
secret in the job specification.

## Run a detached Colab process

```bash
export COLABCTL_ENABLE_NATIVE=1
JOB_ID=$(colabctl --transport native job run train.py --detach --gpu T4)
colabctl --transport native job status "$JOB_ID"
colabctl --transport native job logs "$JOB_ID" --follow
colabctl --transport native job result "$JOB_ID"
```

The process survives the submitting shell and connection while its runtime remains available.

Add `--resumable` only when `train.py` writes a checkpoint to external storage and loads it at
startup. A later `status` or `result` call can then detect runtime loss and relaunch the stored
workload. Recovery is poll-triggered, and logs that existed only on the reclaimed runtime may be
incomplete.

## Route an idempotent job

```bash
colabctl job run train.py \
  --backend colab \
  --allow colab,modal,runpod,vast \
  --cheapest \
  --max-price 2.50 \
  --budget 10 \
  --timeout 3600 \
  --track wandb
```

`--cheapest` orders candidates by the catalog rate. `--max-price` filters that rate, and
`--budget` checks the projected run against the local estimated-spend ledger. Fallback re-runs
the workload after a typed infrastructure error, so this flow requires idempotent code and
durable outputs.

## Ship a function with `@remote`

```python
from colabctl.sdk import remote

@remote(gpu="A100", requirements=["torch"], track="wandb")
def train(epochs: int) -> float:
    import torch
    # Training runs on the remote runtime.
    return float(torch.cuda.is_available() and epochs)

accuracy = train(epochs=10)
```

The synchronous call blocks until the remote result returns. Use `await train.aio(...)` inside an
asyncio application.
