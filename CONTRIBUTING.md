# Contributing to colabctl

Thanks for helping build colabctl. This guide covers the dev workflow and the quality
bar every change must clear.

## Setup

colabctl uses [uv](https://docs.astral.sh/uv/). Install it, then:

```bash
uv sync --all-extras       # creates .venv with every optional dependency + dev tools
```

## The quality gate (run before every commit)

```bash
uv run ruff check src tests        # lint
uv run ruff format src tests       # format (use --check in CI)
uv run mypy src                    # strict type-check
uv run pytest -q                   # the full suite — offline, no credentials
```

CI runs exactly this on Python 3.11 / 3.12 / 3.13, plus `uv build`. All four must pass.

## Conventions

- **Strict typing.** `mypy --strict` must pass. Lazy-imported third-party SDKs are
  listed under `[[tool.mypy.overrides]]` in `pyproject.toml`.
- **Tests are offline.** No test may hit the network or need credentials. Live checks
  live in `spikes/` and are run by hand against a real account.
- **Transports/backends are pluggable.** New transports implement
  `transport.base.TransportAdapter`; new backends implement `backends.base.Backend`.
  Keep heavy/optional SDKs lazy-imported and declared as an extra.
- **Be honest about limitations.** If something isn't live-validated or has a known
  gap, say so in the docstring and the relevant doc — don't paper over it. (See the
  keep-alive saga in `spikes/PHASE0-FINDINGS.md` for why.)
- **Pin & verify external contracts.** When wrapping an external API/CLI, verify it
  against current docs/source and pin the version; surface contract drift loudly.

## Layout

`docs/architecture.md` describes the architecture. `DIRECTIVES.md` records binding decisions.
`spikes/` holds validation runbooks + findings. `src/colabctl/` is the package; see the
README for the module map.
