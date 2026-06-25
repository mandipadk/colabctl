# API reference

Auto-generated from colabctl's own docstrings and type hints. For the CLI, run
`colabctl --help` (or `colabctl <command> --help`); for the MCP server, see
[Architecture](architecture.md).

## Jobs

::: colabctl.backends.base
    options:
      members:
        - JobSpec
        - JobInfo
        - JobResult
        - JobState
        - BackendCapabilities

## Routing & failover

::: colabctl.backends.router.BackendRouter

## Cost engine

::: colabctl.cost.price
    options:
      members:
        - GpuPrice
        - PriceCatalog
        - PriceSource

## Remote execution (`@remote`)

::: colabctl.sdk.remote.remote

## Experiment tracking

::: colabctl.tracking
    options:
      members:
        - resolve_tracking_env
        - requirements_for

## Errors

::: colabctl.errors.ColabctlError
