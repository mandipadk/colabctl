"""Domain models for colabctl.

These pydantic models are the *lingua franca* every layer and backend speaks.
The accelerator/variant/shape enums and the assignment/proxy models mirror the
Colab backend wire contract verified in Phase 0 (see ``spikes/PHASE0-FINDINGS.md``
§3); the session/execution/output models are the provider-neutral domain types
the SDK, CLI, and MCP server expose to callers.
"""

from __future__ import annotations

import enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Accelerators / runtime variants (verified Colab wire enums)
# ---------------------------------------------------------------------------


class Accelerator(enum.StrEnum):
    """Accelerator types accepted by ``/tun/m/assign`` (verified from CLI source)."""

    NONE = "NONE"
    T4 = "T4"
    L4 = "L4"
    G4 = "G4"
    A100 = "A100"
    H100 = "H100"
    V5E1 = "V5E1"  # TPU
    V6E1 = "V6E1"  # TPU

    @property
    def is_gpu(self) -> bool:
        return self in {
            Accelerator.T4,
            Accelerator.L4,
            Accelerator.G4,
            Accelerator.A100,
            Accelerator.H100,
        }

    @property
    def is_tpu(self) -> bool:
        return self in {Accelerator.V5E1, Accelerator.V6E1}

    @property
    def label(self) -> str:
        """Human label used by the CLI: ``NONE`` renders as ``CPU``."""
        return "CPU" if self is Accelerator.NONE else self.value


class Variant(enum.StrEnum):
    """Runtime variant (string form, as sent on the assign query)."""

    DEFAULT = "DEFAULT"
    GPU = "GPU"
    TPU = "TPU"

    @classmethod
    def for_accelerator(cls, accelerator: Accelerator) -> Variant:
        if accelerator.is_tpu:
            return cls.TPU
        if accelerator.is_gpu:
            return cls.GPU
        return cls.DEFAULT


class MachineShape(enum.IntEnum):
    """Machine shape (the integer ``machineShape`` field in assignments)."""

    STANDARD = 0
    HIGH_RAM = 1


# ---------------------------------------------------------------------------
# Assignment / proxy (verified backend response shapes)
# ---------------------------------------------------------------------------


class RuntimeProxyInfo(BaseModel):
    """The reachable Jupyter server for an allocated VM.

    ``url`` + ``token`` are what the kernel client connects to; ``token`` is the
    header-only ``X-Colab-Runtime-Proxy-Token`` credential and expires after
    ``token_expires_in_seconds`` (must be refreshed for long-lived sessions).
    """

    model_config = ConfigDict(populate_by_name=True)

    token: str
    token_expires_in_seconds: int = Field(alias="tokenExpiresInSeconds")
    url: str


class Assignment(BaseModel):
    """A provisioned runtime assignment returned by ``/tun/m/assign``/``/assignments``."""

    model_config = ConfigDict(populate_by_name=True)

    endpoint: str
    accelerator: Accelerator = Accelerator.NONE
    variant: Variant = Variant.DEFAULT
    machine_shape: MachineShape = Field(default=MachineShape.STANDARD, alias="machineShape")
    runtime_proxy_info: RuntimeProxyInfo | None = Field(default=None, alias="runtimeProxyInfo")


# ---------------------------------------------------------------------------
# Public runtime request + session view
# ---------------------------------------------------------------------------


class RuntimeSpec(BaseModel):
    """Caller's request for a runtime: *what hardware do I want?*

    Provider-neutral; each backend maps it onto its own concepts. ``accelerator``
    of ``NONE`` requests a CPU runtime.
    """

    accelerator: Accelerator = Accelerator.T4
    machine_shape: MachineShape = MachineShape.STANDARD
    name: str | None = None
    idle_timeout_seconds: int | None = None

    @property
    def variant(self) -> Variant:
        return Variant.for_accelerator(self.accelerator)


class SessionStatus(enum.StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    UNKNOWN = "UNKNOWN"


class SessionInfo(BaseModel):
    """A view of one live session, normalized across transports."""

    name: str
    endpoint: str
    accelerator: Accelerator = Accelerator.NONE
    variant: Variant = Variant.DEFAULT
    status: SessionStatus = SessionStatus.UNKNOWN
    running: str | None = None  # the file/cell currently executing, when BUSY
    last_execution: str | None = None

    @property
    def hardware_label(self) -> str:
        return self.accelerator.label


class CcuInfo(BaseModel):
    """Compute-unit standing for a Colab account (``/tun/m/ccu-info``).

    Shape verified live (canary, 2026-06-11); ``extra="ignore"`` so the model tolerates
    the undocumented endpoint adding fields without breaking. All fields are optional.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    current_balance: float | None = Field(default=None, alias="currentBalance")
    consumption_rate_hourly: float | None = Field(default=None, alias="consumptionRateHourly")
    assignments_count: int | None = Field(default=None, alias="assignmentsCount")
    eligible_gpus: list[str] = Field(default_factory=list, alias="eligibleGpus")
    eligible_tpus: list[str] = Field(default_factory=list, alias="eligibleTpus")

    @property
    def runway_hours(self) -> float | None:
        """Hours of balance left at the current burn rate (None if either is unknown/zero)."""
        if self.current_balance is None or not self.consumption_rate_hourly:
            return None
        return self.current_balance / self.consumption_rate_hourly

    @classmethod
    def from_raw(cls, raw: object) -> CcuInfo | None:
        """Parse the raw ``ccu_info`` passthrough into a typed view (None if not a dict)."""
        return cls.model_validate(raw) if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# Execution outputs (standard Jupyter output types, typed)
# ---------------------------------------------------------------------------


class StreamOutput(BaseModel):
    output_type: Literal["stream"] = "stream"
    name: Literal["stdout", "stderr"]
    text: str


class ExecuteResultOutput(BaseModel):
    output_type: Literal["execute_result"] = "execute_result"
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    execution_count: int | None = None


class DisplayDataOutput(BaseModel):
    output_type: Literal["display_data"] = "display_data"
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ErrorOutput(BaseModel):
    output_type: Literal["error"] = "error"
    ename: str = ""
    evalue: str = ""
    traceback: list[str] = Field(default_factory=list)


Output = Annotated[
    StreamOutput | ExecuteResultOutput | DisplayDataOutput | ErrorOutput,
    Field(discriminator="output_type"),
]


class ExecutionResult(BaseModel):
    """The result of executing one code unit on a runtime."""

    status: Literal["ok", "error", "abort"] = "ok"
    execution_count: int | None = None
    outputs: list[Output] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def stdout(self) -> str:
        return "".join(
            o.text for o in self.outputs if isinstance(o, StreamOutput) and o.name == "stdout"
        )

    @property
    def stderr(self) -> str:
        return "".join(
            o.text for o in self.outputs if isinstance(o, StreamOutput) and o.name == "stderr"
        )

    @property
    def error(self) -> ErrorOutput | None:
        for o in self.outputs:
            if isinstance(o, ErrorOutput):
                return o
        return None

    @property
    def text(self) -> str:
        """Best-effort flat text: stream text plus any ``text/plain`` results."""
        chunks: list[str] = []
        for o in self.outputs:
            if isinstance(o, StreamOutput):
                chunks.append(o.text)
            elif isinstance(o, (ExecuteResultOutput, DisplayDataOutput)):
                plain = o.data.get("text/plain")
                if isinstance(plain, str):
                    chunks.append(plain)
                elif isinstance(plain, list):
                    chunks.append("".join(str(p) for p in plain))
        return "".join(chunks)
