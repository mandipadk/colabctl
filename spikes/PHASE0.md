# Phase 0 — Validation Spikes (runbook)

**Goal:** confirm the load-bearing unknowns *empirically* before we write package code. You run a handful of commands against **your** Colab Pro account and paste back one file. I've already resolved the rest from the open-source CLI/MCP source so you don't have to.

---

## Already confirmed from source — NOT something you need to test

| Spike | Result (from reading `google-colab-cli` v0.5.7 + `colab-mcp` source) |
|------|------|
| **B — proxy-token / wire contract** | Runtime-proxy token is the **header** `X-Colab-Runtime-Proxy-Token` **+** WS query param `colab-runtime-proxy-token`. XSRF header `X-Goog-Colab-Token`. XSSI prefix `)]}'\n`. Assign flow is GET-then-POST `/tun/m/assign?nbh=<websafe-b64-uuid>[&variant=][&accelerator=]`; `412` → too many assignments. **Cross-confirmed in both repos** → this is exactly what our native transport implements. |
| **D — Python version** | `google-colab-cli` **requires Python ≥ 3.13** → we run it isolated via `uv tool` (below); our 3.11+ core is unaffected. |
| **D — `colaboratory` scope** | Not in Google's public scope registry; both Google tools carry a Google-owned client. **Assume not third-party-grantable** → the working auth path is almost certainly `--auth adc` (gcloud), *not* a self-registered OAuth client. Step 2 tests this. |
| **CLI surface** | Command is `colab`. GPU via `colab new --gpu {T4,L4,G4,H100,A100}`. **No `--json`/machine-readable output** — our adapter parses human stdout, so we need a real transcript (that's what this run captures). |

**What we still must verify live (only you can):**
1. **Spike A — go/no-go:** does `colab` allocate a GPU and run code end-to-end on *your* account; which `--auth` mode works (source default `adc` vs README's `oauth2` — they disagree); and the exact stdout format.
2. **A100 availability** on your Pro tier (optional).

---

## Prerequisites

- **`uv`** (to install the 3.13-only CLI in isolation). If you don't have it:
  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```
- **`gcloud`** (only if you use the recommended ADC auth path). If missing:
  ```bash
  brew install --cask google-cloud-sdk
  ```

---

## Step 1 — Install the official CLI (isolated, won't touch your system Python)

```bash
uv tool install --python 3.13 'google-colab-cli==0.5.7'
colab version          # confirm it's on PATH
```
> If your shell can't find `colab` afterward, run `uv tool update-shell` and open a new terminal.

---

## Step 2 — Authenticate (do this once, interactively)

Two paths. **Try Path A first** — it's the one we expect to work given the scope finding.

### Path A — ADC via gcloud (recommended)
```bash
gcloud auth application-default login \
  --scopes=openid,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/userinfo.profile,\
https://www.googleapis.com/auth/colaboratory,\
https://www.googleapis.com/auth/drive.file
```
This opens your browser → log in with the **Google account that has Colab Pro**. Then tell the CLI to use ADC:
```bash
export COLAB_CLI_AUTH=adc        # belt-and-suspenders; also pass --auth adc if needed
colab --auth adc whoami
```

### Path B — CLI's own OAuth2 (fallback / comparison)
Only if Path A fails. This needs **your own** Desktop OAuth client JSON at `~/.colab-cli-oauth-config.json` (create a "Desktop app" OAuth client in any Google Cloud project, download the JSON). Then:
```bash
colab --auth oauth2 whoami
```
> **Expected finding:** Path B likely fails at the consent screen because the `colaboratory` scope isn't grantable to a self-registered client. **If it fails, that's a valid, useful result — just record the exact error.** Don't fight it.

**Record for the paste-back:** which path worked, and the full `whoami` output (it prints identity + scopes + expiry).

---

## Step 3 — Run the spike (captures everything to one file)

From the repo root:

```bash
bash spikes/run_phase0.sh                 # T4 only — cheapest, do this first
# optional, only if you want the extra data points (uses more compute units):
SPIKE_A100=1 bash spikes/run_phase0.sh
SPIKE_DRIVE=1 bash spikes/run_phase0.sh
```

The script: records the live `--help` surface → checks auth → CPU smoke test → allocates a **T4**, runs `spikes/gpu_probe.py` on the VM (validates real CUDA compute), does a file upload/download round-trip, lists sessions, exports history → **always tears the session down at the end** so you don't leave an idle VM burning compute units.

Everything is written to **`spikes/phase0-results.txt`**.

---

## Step 4 — Paste back

Paste the entire **`spikes/phase0-results.txt`** here, plus a one-line note on:
- which **auth path** worked (A or B),
- anything the **browser/OAuth consent** showed that isn't in the file (warnings, "unverified app", scope denials),
- whether **A100** was available (if you ran that variant).

---

## What your results decide (the go/no-go)

| Outcome | What we conclude / do next |
|--------|-----|
| CLI allocates a GPU + runs the probe + clean teardown | ✅ Sanctioned-default path is real → proceed to **Phase 1** scaffolding; build the CLI adapter against the captured stdout format (golden-file parser). |
| Only `adc` works (Path B fails on scope) | Auth layer leads with ADC/gcloud token minting; the native `/tun/m/*` transport reuses the same colaboratory-scoped token source. (No CLI lock-in — gcloud is a stable Google tool, not the immature colab CLI.) |
| CLI flaky / output unparseable / breaks | Confirms the directive: don't make the CLI load-bearing → prioritize the **native `/tun/m/*` transport** (Spike B already gives us the exact recipe) and lean on Modal/Vertex sooner. |
| A100 quota-denied | Expected on some Pro tiers; record the `Outcome` enum. Informs the accelerator-request + quota-surfacing design. |

> **Account safety during Phase 0:** this is your own paid account, a single interactive session, torn down immediately — squarely in the low-risk band. The script never runs concurrent sessions or fakes activity.
