# Deployment & operations

How to run colabctl on a desktop or a headless server/CI, manage credentials, and
operate it safely.

## Credentials

colabctl never writes credentials to plaintext. Secrets live behind one `SecretStore`:

- **Desktop** — OS keychain (`KeyringSecretStore`), used automatically.
- **Headless / CI** — `EncryptedFileSecretStore`: set `COLABCTL_SECRET_PASSPHRASE` and
  secrets are stored in an scrypt+Fernet-encrypted file. `default_secret_store()` picks
  this automatically when the passphrase env var is set.

Per-backend auth:

| Backend | Credentials |
|---|---|
| Colab, Vertex | Google ADC — `gcloud auth application-default login --scopes=…colaboratory` (see below) |
| Modal | `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` (or `~/.modal.toml`) |
| Hugging Face | `HF_TOKEN` |
| Kaggle | `~/.kaggle/kaggle.json` (or `KAGGLE_USERNAME` / `KAGGLE_KEY`) |
| RunPod | `RUNPOD_API_KEY` |

### Colab ADC

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory,\
https://www.googleapis.com/auth/drive.file
```

`cloud-platform` + `openid` are required by gcloud itself; `colaboratory` by the Colab
backend; `drive.file` by Drive sync.

## Headless / long-running jobs

- **Native transport** is opt-in: set `COLABCTL_ENABLE_NATIVE=1` (it's reverse-engineered
  and disabled by default per the ToS posture).
- **Keep-alive limitation (important):** Colab's RuntimeService keep-alive RPC is
  **unusable under token auth** (live-confirmed). There is no reliable way to hold a
  Colab runtime open indefinitely headlessly. For long unattended work:
  - keep the kernel genuinely busy (an active workload isn't idle-reclaimed), and
  - **checkpoint to Drive + re-assign on reclamation** via `RuntimeLifecycleManager`
    (`drive_checkpoint_hooks`), or
  - route deadline-bound production jobs to **Vertex** or **Modal** instead.

```python
from colabctl import RuntimeLifecycleManager, DriveSync, drive_checkpoint_hooks
# ... build a transport ...
checkpoint, restore = drive_checkpoint_hooks(DriveSync(), [("content/state.pkl", "state.pkl")])
mgr = RuntimeLifecycleManager(transport, spec, checkpoint=checkpoint, restore=restore,
                              reassign_before_expiry=True)
```

## Driving from an AI agent (MCP)

```json
{ "mcpServers": { "colabctl": { "command": "colabctl-mcp" } } }
```

The server exposes interactive Colab tools plus `run_job` / `list_backends` across all
backends. Run it under a process manager for always-on agent access.

## Abuse-detection risk (disclosed)

Even on paid Colab Pro with a positive balance, Google operates **opaque, no-recourse
abuse-detection bans** on sustained headless GPU usage — the blast radius is the whole
Google account. colabctl treats this as a first-class fact:

- defaults to the sanctioned CLI path (lowest divergence from first-party clients),
- never fakes "active programming" or runs multi-account quota circumvention,
- enforces single-session-per-runtime by default,
- and lets you **fail over to Modal/Vertex** so a ban degrades capability instead of
  killing the workflow.

Don't share or resell access, and respect each backend's terms.

## Spend guards

- `cap_timeout` enforces a hard billable-time ceiling on paid backends (wired into Modal).
- The RunPod backend always terminates the pod on `result()`.
- Always pass `timeout`s; never point an autonomous agent loop at a paid backend without
  a hard cap.
