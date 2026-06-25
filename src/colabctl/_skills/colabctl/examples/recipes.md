# colabctl recipes (worked end-to-end flows)

Copy-pasteable flows that the one-line `--help` can't convey. Verify flags with
`colabctl <cmd> --help`; values like GPU/backends are examples.

## 1. Quick: run a script on a Colab GPU

```bash
colabctl run train.py --gpu T4          # allocate → run → release
colabctl run train.py --gpu T4 --keep   # keep the runtime for follow-up `exec`
```

## 2. Durable training that survives a runtime reclaim (the moat)

Make the workload checkpoint to Drive/disk and resume idempotently, then:

```bash
ID=$(colabctl job run train.py --backend colab --gpu A100 --detach --resumable)
colabctl job status "$ID"         # cross-process; safe to close your shell
colabctl job logs "$ID" --follow  # stitched across auto-resume incarnations
colabctl job result "$ID"         # blocks until terminal
colabctl job history "$ID"        # see each reclaim → resume transition
colabctl job gc                   # reconcile/prune when done
```

If the runtime is reclaimed, colabctl re-allocates and the job auto-resumes from its checkpoint
(bounded by an incarnation cap so a flapping runtime can't bill GPUs forever).

## 3. Find the cheapest place to run, with a hard budget

```bash
colabctl cost --gpu A100 --live                 # per-backend $/hr, cheapest first
colabctl spot-risk --gpu A100                    # is spot worth it? (interruption vs savings)
colabctl job run train.py --gpu A100 \
  --allow colab,modal,runpod,vast --cheapest --budget 10
# routes to the cheapest qualifying backend; refuses to launch if it would exceed $10 (fail-closed)
colabctl spend                                   # what you've spent so far
```

## 4. Spot/interruptible with automatic fallback

```bash
colabctl job run train.py --gpu A100 \
  --allow vast,runpod,modal --cheapest --spot --max-price 1.50
# bids on the spot tier; on preemption it fails over to the next candidate (or on-demand)
```

## 5. Cross-backend failover (resilience, not cost)

```bash
colabctl job run train.py --backend colab --allow colab,modal,vertex --gpu T4
# tries colab first; on an infra/allocation error, re-runs on modal, then vertex
```

## 6. Parameterized notebook on a remote GPU

```bash
colabctl notebook run experiment.ipynb \
  --param epochs=10 --param lr=0.001 --gpu T4 --out experiment.executed.ipynb
```

## 7. Check entitlement before committing

```bash
colabctl auth status     # are Colab/Drive credentials set up?
colabctl quota           # compute-unit balance, burn rate, runway, entitled GPUs
colabctl job backends    # which backends are configured + their capabilities
```

## Transport notes

- Default transport is `cli` (Google's `colab` binary, bundled in `colabctl[cli]`). For the
  durable detached-job features and cross-process `attach`, use the native transport:
  `COLABCTL_ENABLE_NATIVE=1 colabctl -t native job run ... --detach --resumable`.
- No external binary available? `-t native` (opt-in) and `-t browser` (logged-in tab) need none.
