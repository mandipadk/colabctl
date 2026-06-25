# Security Policy

## Supported versions

colabctl is pre-1.0 and ships frequently; only the **latest released minor** gets security
fixes. Please upgrade (`colabctl update`) before reporting.

| Version | Supported |
| --- | --- |
| 0.4.x   | ✅ |
| < 0.4   | ❌ |

## Reporting a vulnerability

Report security issues **privately** through GitHub's private vulnerability reporting — open the
repository's **Security** tab → **Report a vulnerability**
([direct link](https://github.com/mandipadk/colabctl/security/advisories/new)). **Please do not
open a public issue for a vulnerability.**

Include the affected version, a description, reproduction steps, and the impact. We aim to
acknowledge within a few days and to coordinate a fix and disclosure with you.

## Scope

colabctl handles credentials and drives remote runtimes, so the security-relevant surface is:

- credential handling — the OS-keychain / encrypted-file secret store, environment injection, and
  proxy-token caching;
- the reverse-engineered native `/tun/m/*` transport and the detached-job runner;
- code/argument marshalling for `@remote` and detached jobs (cloudpickle by value).

**Out of scope** (report upstream): Google Colab itself, third-party backends (Modal, Vertex,
RunPod, Vast, …), and the tracking libraries (Weights & Biases, MLflow).

## How colabctl handles secrets

By design, credentials are **never** written into the state document. Secrets live in the OS
keychain or an encrypted file; experiment-tracking credentials are injected as environment
variables (never baked into pickled code) and **re-resolved from the secret store on every run**
rather than persisted. If you find any path where a credential is logged or persisted in
plaintext, that's a vulnerability — please report it.
