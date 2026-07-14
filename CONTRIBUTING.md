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

CI runs exactly this on Python 3.12 and 3.13, plus `uv build`. All four must pass.

## Conventions

- **Strict typing.** `mypy --strict` must pass. Lazy-imported third-party SDKs are
  listed under `[[tool.mypy.overrides]]` in `pyproject.toml`.
- **Tests are offline.** No test may hit the network, use credentials, or spend money. Put
  provider calls behind injectable HTTP, SDK, command, or GraphQL clients and test them with
  fixtures.
- **Transports/backends are pluggable.** New transports implement
  `transport.base.TransportAdapter`; new backends implement `backends.base.Backend`.
  Keep heavy/optional SDKs lazy-imported and declared as an extra.
- **Be honest about limitations.** If something has not been checked on a real account or has a
  known gap, say so in the docstring, support matrix, and relevant user guide.
- **Pin & verify external contracts.** When wrapping an external API/CLI, verify it
  against current docs/source and pin the version; surface contract drift loudly.

## Layout

`docs/architecture.md` describes the architecture. `ROADMAP.md` records public support levels
and current limitations. `src/colabctl/` is the package, and `tests/` contains the hermetic
suite. See the README for the public module and feature map.
