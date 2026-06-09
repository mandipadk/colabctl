# Backends

colabctl exposes one job API — `submit / status / logs / result / cancel` (and the
`run` convenience) — over pluggable backends, with a `BackendRouter` that selects by
capability and **fails over on infrastructure errors** (a Colab outage/quota/ban
degrades to another backend; a job whose *user code* failed is not retried elsewhere).

Pick a backend explicitly:

```bash
colabctl job run train.py --backend modal --gpu A100 --req torch
colabctl job backends            # capability listing
```
```python
from colabctl.backends import build_backend, JobSpec
backend = build_backend("hf")
result = await backend.run(JobSpec(code="...", accelerator=Accelerator.A100))
```

## Capability & ToS matrix

| Backend | GPUs | Interactive | Streaming logs | stdout captured | ToS posture | Auth | Live-validated |
|---|---|---|---|---|---|---|---|
| **colab** (cli) | T4/L4/A100/H100 | ✅ | — | ✅ | sanctioned | ADC (gcloud) | ✅ |
| **colab** (native) | T4/L4/A100/H100 | ✅ | ✅ | ✅ | sanctioned, **opt-in** | ADC | ✅ |
| **modal** | T4/L4/A100/H100 | — | ✅ | ✅ | sanctioned | `MODAL_TOKEN_ID/SECRET` | ✅ |
| **vertex** | T4/L4/A100/H100 | — | — | ✗ (Cloud Logging) | sanctioned | ADC + GCP project/bucket | ⏳ |
| **hf** | T4/L4/A100/H100 | — | ✅ | ✅ | sanctioned | `HF_TOKEN` | ⏳ |
| **kaggle** | T4 only | — | — | best-effort (log fetch) | sanctioned | `~/.kaggle/kaggle.json` + `KAGGLE_USERNAME` | ⏳ |
| **runpod** | T4/L4/A100/H100 | — | — | ✗ (use a volume) | sanctioned | `RUNPOD_API_KEY` | ⏳ |

⏳ = implemented + unit-tested, not yet live-validated (no account in CI).

## Cost & caveats

- **Colab** — your Colab Pro compute units. Automated use is permitted on paid tiers
  with a positive balance; the native `/tun/m/*` transport is reverse-engineered and
  **disabled by default** (`COLABCTL_ENABLE_NATIVE=1` to opt in). Long unattended jobs:
  see the keep-alive limitation in [deployment](deployment.md).
- **Modal** — pay-per-GPU-second, gVisor-isolated (great for agent-generated code). A
  hard timeout ceiling (`cap_timeout`, default 1 h) guards against runaway spend.
- **Vertex AI** — sanctioned, headless, deadline-bound production jobs. stdout goes to
  Cloud Logging (not captured); `result` returns the terminal state + a console link;
  artifacts go to GCS. Needs a project + staging bucket.
- **Hugging Face Jobs** — durable remote jobs (the id survives your process), cheap GPUs.
- **Kaggle** — free GPU, but **T4 only**, **no cancel API**, and logs are fetched at the
  end (best-effort).
- **RunPod** — IaaS GPU pods (rents a machine). **stdout is not captured** — persist
  outputs to a RunPod volume / object storage. Per-second billing; the backend always
  terminates the pod on `result()`.

> **Spend:** paid backends (Modal/Vertex/HF/RunPod/Kaggle) bill for GPU time. Set
> `timeout`s, prefer the cheapest accelerator that fits, and never run an autonomous
> agent loop against a paid backend without a hard cap.
