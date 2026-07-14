# Backends

colabctl exposes `submit`, `status`, `logs`, `result`, and `cancel` across seven batch backends.
The `run` method combines submission and result collection for shorter jobs.

Choose a backend explicitly:

```bash
colabctl job run train.py --backend modal --gpu A100 --req torch
colabctl job backends
```

```python
from colabctl.backends import JobSpec, build_backend
from colabctl.models import Accelerator

backend = build_backend("hf")
result = await backend.run(
    JobSpec(code="print('hello')", accelerator=Accelerator.A100)
)
```

## Support matrix

| Backend | GPUs | Streaming logs | Captured stdout | Auth | Public evidence |
|---|---|---:|---:|---|---|
| Colab | T4, L4, G4, A100, H100 | Custom transport | Yes | Google ADC | Official and custom transports checked live |
| Modal | T4, L4, A100, H100 | Yes | Yes | `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | CPU and T4 checked live |
| Vertex AI | T4, L4, A100, H100 | No | Cloud Logging | Google ADC and GCP project | Hermetic tests |
| Hugging Face Jobs | T4, L4, A100, H100 | Yes | Yes | `HF_TOKEN` | Hermetic tests |
| Kaggle | T4 | Final log fetch | Best effort | Kaggle credentials file or environment | Hermetic tests |
| RunPod | T4, L4, A100, H100 | No | No | `RUNPOD_API_KEY` | Hermetic tests |
| Vast.ai | T4, L4, A100, H100 | No | No | `VAST_API_KEY` | Hermetic tests |

Hermetic tests use fake or captured provider responses and never contact a provider. A live check
is a bounded run on a real account. See the
[public roadmap](https://github.com/mandipadk/colabctl/blob/main/ROADMAP.md) for limitations and
the current validation status.

## Routing

`BackendRouter` filters by accelerator and can try an explicit list of providers. The first
typed infrastructure error moves to the next eligible backend. User-code failures return to the
caller without fallback.

```bash
colabctl job run train.py \
  --backend colab \
  --allow colab,modal,runpod,vast \
  --cheapest \
  --max-price 2.50 \
  --timeout 3600
```

Fallback runs the workload again. Limit it to idempotent workloads that can tolerate a duplicate
attempt after an ambiguous provider response.

## Costs and provider caveats

- Colab consumes the user's subscription or compute units. Capacity and limits can change. The
  custom transport is opt-in through `COLABCTL_ENABLE_NATIVE=1`.
- Modal bills by resource usage. colabctl applies the configured timeout ceiling to the sandbox.
- Vertex AI keeps stdout in Cloud Logging and writes artifacts to the configured GCS location.
- Hugging Face Jobs returns a durable provider job ID that a later process can inspect.
- Kaggle supports T4 jobs in this adapter, has no cancel API, and exposes logs after execution.
- RunPod rents a pod. Store outputs on a volume or external object store; the adapter terminates
  the pod while collecting a result.
- Vast.ai selects a marketplace offer. The selected host controls capacity, reliability, and
  price at submission time.

## Price filters and spend estimates

`--max-price` filters the catalog hourly rate. `--budget` checks the new estimate against the
local spend ledger. These checks can refuse a catalog candidate before launch, but they do not
control provider invoices or observe unrelated account usage. Set a timeout, review the chosen
provider, and inspect `colabctl audit` after the run.
