# Deployment and operations

Run colabctl on a desktop, an always-on host, or CI with credentials scoped to the provider and
workload that need them.

## Credentials

colabctl exposes two secret stores:

- `KeyringSecretStore` uses the operating-system keychain.
- `EncryptedFileSecretStore` uses an scrypt-derived key and Fernet encryption. Set
  `COLABCTL_SECRET_PASSPHRASE` on a headless host to select it.

The local state document stores session and job metadata, not secret values.

| Backend | Credentials |
|---|---|
| Colab and Vertex AI | Google Application Default Credentials |
| Modal | `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`, or `~/.modal.toml` |
| Hugging Face Jobs | `HF_TOKEN` |
| Kaggle | `~/.kaggle/kaggle.json`, or `KAGGLE_USERNAME` and `KAGGLE_KEY` |
| RunPod | `RUNPOD_API_KEY` |
| Vast.ai | `VAST_API_KEY` |

## Colab authentication

colabctl wraps the Application Default Credentials login and reports missing scopes or quota
configuration:

```bash
colabctl auth login
colabctl auth status
```

The equivalent manual login is:

```bash
gcloud auth application-default login \
  --scopes=openid,https://www.googleapis.com/auth/cloud-platform,\
https://www.googleapis.com/auth/userinfo.email,\
https://www.googleapis.com/auth/colaboratory,\
https://www.googleapis.com/auth/drive.file
```

Runtime-direct Drive operations need a quota project with the Drive API enabled:

```bash
gcloud services enable drive.googleapis.com --project=YOUR_PROJECT
gcloud auth application-default set-quota-project YOUR_PROJECT
```

## Custom Colab transport

The custom transport uses the CLI name `native` and is disabled by default. Enable it for the
process that runs colabctl:

```bash
export COLABCTL_ENABLE_NATIVE=1
colabctl --transport native doctor
```

It supports headless keep-alive through the Colab tunnel endpoint, cross-process attach,
interrupt, runtime file transfer, and detached jobs. Provider protocol changes can break this
transport even when the official CLI continues to work, so keep colabctl current and check the
exact workflow after an update before unattended use.

## Detached jobs

```bash
export COLABCTL_ENABLE_NATIVE=1
JOB_ID=$(colabctl --transport native job run train.py --detach --resumable --gpu T4)
colabctl --transport native job logs "$JOB_ID" --follow
colabctl --transport native job result "$JOB_ID"
```

The supervised process remains on the runtime after the submitting shell exits. A later
`status`, `logs`, or `result` command uses the local state record to reconnect.

If `status` or `result` observes that a resumable job lost its runtime, colabctl can allocate a
replacement and relaunch the stored specification. Your program must save its checkpoint to
Drive or another durable location and restore it when the new process starts. colabctl does not
yet run that restore step for arbitrary application code, and recovery does not run while every
controller process is offline.

Runtime-local logs can disappear with a reclaimed runtime. Treat external experiment tracking,
object storage, or application logs as the durable record for long jobs.

## Lifecycle manager

Applications that need direct control can provide checkpoint and restore callbacks to
`RuntimeLifecycleManager`:

```python
from colabctl import DriveSync, RuntimeLifecycleManager, drive_checkpoint_hooks

checkpoint, restore = drive_checkpoint_hooks(
    DriveSync(),
    [("content/state.pkl", "state.pkl")],
)
manager = RuntimeLifecycleManager(
    transport,
    spec,
    checkpoint=checkpoint,
    restore=restore,
    reassign_before_expiry=True,
)
```

The application owns checkpoint consistency and compatibility. Write a complete checkpoint
before publishing it as the latest restorable version.

## MCP server

```json
{
  "mcpServers": {
    "colabctl": {
      "command": "colabctl-mcp"
    }
  }
}
```

The MCP server can allocate runtimes and execute arbitrary code with the credentials available
to its process. Run it under a dedicated account, limit its filesystem access, and avoid exposing
the local stdio server through an unauthenticated network bridge.

## Operational checks

Run these before a paid or unattended job:

```bash
colabctl doctor
colabctl auth status
colabctl quota
colabctl sessions
colabctl job list
```

Use `colabctl gc` to compare local session records with live Colab assignments. Add
`--release-orphans` only when you intend to release provider resources that have no active local
record.

## Spend controls

- Set a timeout for paid providers.
- Treat `--max-price`, `--budget`, `colabctl cost`, and `colabctl spend` as catalog and ledger
  estimates.
- Persist outputs before result collection tears down an ephemeral pod or sandbox.
- Check the provider console when cancellation or cleanup returns an error.

## Provider terms

Use the official Colab CLI transport by default. Do not share or resell provider access, rotate
accounts to evade limits, or automate behavior that violates a provider's current terms. Quotas,
prices, capacity, and acceptable-use rules can change independently of colabctl.
