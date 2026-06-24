"""Native Colab backend client — the ``/tun/m/*`` assignment protocol.

Verified from ``google-colab-cli`` v0.5.7 source in Phase 0 (Apache-2.0, port
permitted). Pure helpers (``web_safe_nbh``, ``strip_xssi``, ``build_assign_params``,
the public-API-key decode) are deterministic and unit-tested offline; the network
methods use an injected ``httpx.AsyncClient`` and a pluggable bearer-token provider
so auth (ADC/OAuth) is decoupled from transport.

Keep-alive note (live-confirmed): the RuntimeService keep-alive RPC is unusable from
token auth — bearer returns 403 (no serviceusage permission on Colab's project),
API-key-only returns 401 ("API keys are not supported by this API"). Only the browser
web client succeeds, via session cookies. The working keep-alive is therefore kernel
activity, not this RPC — see ``NativeColabTransport.keep_alive`` and PHASE0-FINDINGS §2.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from colabctl.errors import (
    AcceleratorUnavailableError,
    AllocationError,
    KeepAliveError,
    KernelError,
    RuntimeUnavailableError,
    TooManyAssignmentsError,
    TransportError,
)
from colabctl.models import Accelerator, Assignment, MachineShape, RuntimeProxyInfo, Variant
from colabctl.observability import retry_async

# The backend encodes ``variant`` as an integer (AssignmentVariant: DEFAULT=0/GPU=1/TPU=2),
# while our domain ``Variant`` is a string enum — map between them on the wire boundary.
_VARIANT_BY_INT = {0: Variant.DEFAULT, 1: Variant.GPU, 2: Variant.TPU}

# --- verified constants -----------------------------------------------------

COLAB_DOMAIN = "https://colab.research.google.com"
COLAB_API_DOMAIN = "https://colab.pa.googleapis.com"
TUN_ENDPOINT = "/tun/m"
ASSIGN_PATH = f"{TUN_ENDPOINT}/assign"
ASSIGNMENTS_PATH = f"{TUN_ENDPOINT}/assignments"
UNASSIGN_PATH = f"{TUN_ENDPOINT}/unassign"
KEEPALIVE_RPC = "/$rpc/google.internal.colab.v1.RuntimeService/KeepAliveAssignment"

XSSI_PREFIX = ")]}'\n"

#: Header names (verified).
CLIENT_AGENT_HEADER = "X-Colab-Client-Agent"
XSRF_HEADER = "X-Goog-Colab-Token"
PROXY_TOKEN_HEADER = "X-Colab-Runtime-Proxy-Token"
USER_PROJECT_HEADER = "x-goog-user-project"
API_CLIENT_HEADER = "x-goog-api-client"
#: The tunnel keep-alive ping header (google-colab-cli recipe). Works under token auth,
#: unlike the RuntimeService RPC above.
TUNNEL_HEADER = "X-Colab-Tunnel"
TUNNEL_HEADER_VALUE = "Google"

CLIENT_AGENT = "colabctl"
#: Colab's own GCP project that owns the public web-client API key.
COLAB_PROJECT_ID = "1014160490159"


# The CLI obfuscates its public web-client key in a packed byte registry; we
# decode it once here (it is a *public* web key, not a secret) so the value is
# auditable rather than hidden. See client.py::_PUBLIC_CLIENT_REGISTRY upstream.
_PUBLIC_CLIENT_REGISTRY = (
    b"\x1c"
    b"782d676f6f672d6170692d6b6579"
    b"\x4e"
    b"41497a615379413242766e744c774e7746746855423477365f42686e30634d6c56487779614863"
)


def _registry_field(index: int) -> str:
    cursor = 0
    blob = _PUBLIC_CLIENT_REGISTRY
    for _ in range(index):
        cursor += 1 + blob[cursor]
    length = blob[cursor]
    return bytes.fromhex(blob[cursor + 1 : cursor + 1 + length].decode("ascii")).decode("ascii")


#: ``("x-goog-api-key", "AIzaSy...")`` — the public web-client API key.
PUBLIC_API_KEY_HEADER: str = _registry_field(0)
PUBLIC_API_KEY: str = _registry_field(1)


# --- pure helpers (offline-tested) ------------------------------------------


def web_safe_nbh(notebook_id: uuid.UUID) -> str:
    """Encode a UUID as Colab's ``nbh`` query value.

    ``str(uuid)`` → ``-``→``_`` → right-pad to 44 chars with ``.`` (verified).
    """
    s = str(notebook_id)
    return s.replace("-", "_") + "." * (44 - len(s))


def strip_xssi(text: str) -> str:
    """Strip Colab's anti-XSSI prefix ``)]}'\\n`` if present."""
    return text[len(XSSI_PREFIX) :] if text.startswith(XSSI_PREFIX) else text


def build_assign_params(
    nbh: str,
    *,
    variant: Variant | None = None,
    accelerator: Accelerator | None = None,
) -> dict[str, str]:
    """Build the ``/tun/m/assign`` query params (``authuser`` is added per-request)."""
    params: dict[str, str] = {"nbh": nbh}
    if variant is not None:
        params["variant"] = variant.value
    if accelerator is not None:
        params["accelerator"] = accelerator.value
    return params


def keepalive_headers(*, pin_colab_project: bool = True) -> dict[str, str]:
    """Headers for the KeepAliveAssignment RPC (browser-style, API-key auth)."""
    headers = {
        "Content-Type": "application/json+protobuf",
        PUBLIC_API_KEY_HEADER: PUBLIC_API_KEY,
        "x-user-agent": "grpc-web-javascript/0.1",
        API_CLIENT_HEADER: "grpc-web/0.1",
    }
    if pin_colab_project:
        headers[USER_PROJECT_HEADER] = COLAB_PROJECT_ID
    return headers


def _coerce_variant(value: Any) -> Variant:
    """Map a wire ``variant`` (int or str) to our :class:`Variant`."""
    if isinstance(value, bool):  # bool is an int subclass; never a valid variant
        return Variant.DEFAULT
    if isinstance(value, int):
        return _VARIANT_BY_INT.get(value, Variant.DEFAULT)
    if isinstance(value, str):
        try:
            return Variant(value)
        except ValueError:
            return Variant.DEFAULT
    return Variant.DEFAULT


def _accelerator_from_wire(value: Any) -> Accelerator:
    if not value:
        return Accelerator.NONE
    try:
        return Accelerator(str(value))
    except ValueError:
        return Accelerator.NONE


def assignment_from_wire(d: dict[str, Any]) -> Assignment:
    """Build an :class:`Assignment` from a raw backend response dict.

    Handles the wire quirks our domain models intentionally don't: integer
    ``variant``/``machineShape`` and the ``runtimeProxyInfo`` alias.
    """
    if "endpoint" not in d:
        raise AllocationError("Backend assignment response is missing the 'endpoint' field.")
    rpi = d.get("runtimeProxyInfo")
    shape = d.get("machineShape")
    return Assignment(
        endpoint=d["endpoint"],
        accelerator=_accelerator_from_wire(d.get("accelerator")),
        variant=_coerce_variant(d.get("variant")),
        machine_shape=MachineShape(shape) if isinstance(shape, int) else MachineShape.STANDARD,
        runtime_proxy_info=RuntimeProxyInfo.model_validate(rpi) if rpi else None,
    )


# --- async client -----------------------------------------------------------

TokenProvider = Callable[[], Awaitable[str]]


class ColabBackendClient:
    """Async client for the Colab ``/tun/m/*`` backend.

    Args:
        http: an ``httpx.AsyncClient`` (caller owns its lifecycle/proxies/timeouts).
        token_provider: async callable returning a fresh OAuth bearer (colaboratory
            scope). Required for assign/unassign; not used by API-key-only keep-alive.
        domain / api_domain: override for sandbox/staging environments.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        token_provider: TokenProvider | None = None,
        domain: str = COLAB_DOMAIN,
        api_domain: str = COLAB_API_DOMAIN,
    ) -> None:
        self._http = http
        self._token_provider = token_provider
        self._domain = domain.rstrip("/")
        self._api_domain = api_domain.rstrip("/")

    async def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            CLIENT_AGENT_HEADER: CLIENT_AGENT,
        }
        if self._token_provider is not None:
            token = await self._token_provider()
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
    ) -> httpx.Response:
        """Issue a request (auth + authuser + transient retry); map 412.

        Returns the response for any non-5xx status — including 4xx — so callers can
        inspect it (e.g. assign maps 400 → AcceleratorUnavailableError).
        """
        merged = await self._auth_headers()
        if headers:
            merged.update(headers)
        req_params = dict(params or {})
        if self._domain in url:  # Colab requires authuser on the frontend host
            req_params.setdefault("authuser", "0")

        async def _attempt() -> httpx.Response:
            r = await self._http.request(
                method, url, params=req_params or None, headers=merged, json=json_body
            )
            # 5xx are transient — raise so retry_async backs off and re-issues.
            if r.status_code >= 500:
                raise TransportError(f"{method} {url}: HTTP {r.status_code} (server error)")
            return r

        resp = await retry_async(_attempt, retry_on=(httpx.TransportError, TransportError))
        if resp.status_code == 412:
            raise TooManyAssignmentsError(
                "Colab returned 412: the account already has too many assignments."
            )
        return resp

    @staticmethod
    def _parse(resp: httpx.Response) -> object | None:
        if not resp.is_success:
            raise TransportError(
                f"{resp.request.method} {resp.url}: HTTP {resp.status_code} {resp.text[:300]!r}"
            )
        body = strip_xssi(resp.text)
        if not body:
            return None
        try:
            parsed: object = json.loads(body)
        except ValueError as exc:
            raise TransportError(
                f"{resp.request.method} {resp.url}: response body was not valid JSON "
                f"({body[:200]!r})."
            ) from exc
        return parsed

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
    ) -> object | None:
        return self._parse(
            await self._send(method, url, params=params, headers=headers, json_body=json_body)
        )

    async def assign(
        self,
        *,
        accelerator: Accelerator = Accelerator.T4,
        notebook_id: uuid.UUID | None = None,
    ) -> Assignment:
        """Allocate a runtime (GET pre-flight → POST with XSRF token)."""
        nb = notebook_id or uuid.uuid4()
        nbh = web_safe_nbh(nb)
        variant = Variant.for_accelerator(accelerator)
        params = build_assign_params(nbh, variant=variant, accelerator=accelerator)
        url = f"{self._domain}{ASSIGN_PATH}"

        pre = await self._request_json("GET", url, params=params)
        if not isinstance(pre, dict):
            raise AllocationError("Unexpected assign GET response (not an object).")
        # An existing assignment carries an endpoint + runtimeProxyInfo directly.
        if "endpoint" in pre and "runtimeProxyInfo" in pre:
            return assignment_from_wire(pre)

        xsrf = pre.get("token")
        if not xsrf:
            raise AllocationError("Assign GET did not return an XSRF token.")
        post_resp = await self._send("POST", url, params=params, headers={XSRF_HEADER: xsrf})
        # Colab returns 400 when the caller isn't entitled to the requested accelerator.
        if post_resp.status_code == 400:
            raise AcceleratorUnavailableError(
                f"Colab rejected accelerator {accelerator.value!r} — no quota or "
                "entitlement on this account/tier. Try a different --gpu or CPU.",
                accelerator=accelerator.value,
            )
        post = self._parse(post_resp)
        if not isinstance(post, dict):
            raise AllocationError("Unexpected assign POST response (not an object).")
        return assignment_from_wire(post)

    async def refresh_assignment(
        self,
        notebook_id: uuid.UUID,
        *,
        accelerator: Accelerator = Accelerator.T4,
    ) -> Assignment:
        """Reattach to an existing runtime by notebook id, minting a fresh proxy token.

        Runs ONLY the assign GET pre-flight with the given ``nbh`` — it never POSTs, so
        it cannot accidentally allocate a new runtime. If the runtime still exists the
        backend returns it directly with a *fresh* ``runtimeProxyInfo`` (live-verified
        Phase A §②: same endpoint, new token); if it has been reclaimed the pre-flight
        returns only an XSRF token (the prelude to a *new* allocation), which we refuse
        with :class:`RuntimeUnavailableError` instead of silently re-allocating.

        This is the primitive behind native cross-process *attach* and the
        non-disruptive proxy-token refresh (plan §5.10).
        """
        nbh = web_safe_nbh(notebook_id)
        variant = Variant.for_accelerator(accelerator)
        params = build_assign_params(nbh, variant=variant, accelerator=accelerator)
        url = f"{self._domain}{ASSIGN_PATH}"
        pre = await self._request_json("GET", url, params=params)
        if not isinstance(pre, dict):
            raise AllocationError("Unexpected assign GET response (not an object).")
        if "endpoint" in pre and "runtimeProxyInfo" in pre:
            return assignment_from_wire(pre)
        raise RuntimeUnavailableError(
            "No live assignment for this notebook id — the runtime was reclaimed "
            "(refusing to allocate a new one during reattach)."
        )

    async def list_assignments(self) -> list[Assignment]:
        url = f"{self._domain}{ASSIGNMENTS_PATH}"
        body = await self._request_json("GET", url)
        if not isinstance(body, dict):
            return []
        return [assignment_from_wire(a) for a in body.get("assignments", [])]

    async def ccu_info(self) -> object | None:
        """Best-effort compute-unit balance/usage info from ``/tun/m/ccu-info``.

        The response shape is undocumented and returned as-is (a dict); surface it to
        the user for quota awareness rather than modeling fields we can't verify.
        """
        return await self._request_json("GET", f"{self._domain}{TUN_ENDPOINT}/ccu-info")

    async def unassign(self, endpoint: str) -> None:
        url = f"{self._domain}{UNASSIGN_PATH}/{endpoint}"
        pre = await self._request_json("GET", url)
        token = pre.get("token") if isinstance(pre, dict) else None
        headers = {XSRF_HEADER: token} if token else {}
        await self._request_json("POST", url, headers=headers)

    async def proxy_request(
        self,
        method: str,
        proxy_url: str,
        path: str,
        *,
        proxy_token: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        json_body: object | None = None,
        content: bytes | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Issue a request to a runtime's Jupyter proxy (header-only proxy-token auth).

        The runtime proxy authenticates with the ``X-Colab-Runtime-Proxy-Token`` header
        (verified header-only in Phase A §①), NOT the OAuth bearer the assign/unassign
        endpoints use — so this deliberately does not route through ``_send``. It is the
        shared primitive for kernel interrupt (§5.3) and the contents-API file transfer
        (Pillar 3a). Returns the response as-is so callers map status codes themselves.
        """
        url = f"{proxy_url.rstrip('/')}/{path.lstrip('/')}"
        merged = {**self.proxy_kernel_headers(proxy_token), **(headers or {})}
        kwargs: dict[str, Any] = {"params": params, "headers": merged}
        if json_body is not None:
            kwargs["json"] = json_body
        if content is not None:
            kwargs["content"] = content
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await self._http.request(method, url, **kwargs)

    async def interrupt_kernel(self, proxy_url: str, kernel_id: str, *, proxy_token: str) -> None:
        """Interrupt the running cell on a kernel via the proxy REST API.

        Live-verified in Phase A §④ (HTTP 204). Lets an agent stop a runaway cell
        without killing the whole runtime.
        """
        resp = await self.proxy_request(
            "POST", proxy_url, f"/api/kernels/{kernel_id}/interrupt", proxy_token=proxy_token
        )
        if resp.status_code not in (200, 204):
            raise KernelError(
                f"interrupt of kernel {kernel_id} failed: "
                f"HTTP {resp.status_code} {resp.text[:200]!r}"
            )

    async def keep_alive(self, endpoint: str, *, use_bearer: bool = False) -> None:
        """Send one KeepAliveAssignment RPC.

        NOTE: live-confirmed UNUSABLE under token auth (PHASE0-FINDINGS §2) —
        ``use_bearer=False`` (API-key-only) returns HTTP 401 ("API keys are not
        supported by this API"), and ``use_bearer=True`` returns HTTP 403 (no
        serviceusage permission on Colab's project). The browser web client only
        succeeds because it authenticates with session cookies. Retained for
        reference and a possible future cookie-auth path; do not rely on it. The
        working keep-alive is kernel activity (see NativeColabTransport.keep_alive).
        """
        url = f"{self._api_domain}{KEEPALIVE_RPC}"
        headers = keepalive_headers()
        if use_bearer and self._token_provider is not None:
            headers["Authorization"] = f"Bearer {await self._token_provider()}"
        resp = await self._http.post(url, headers=headers, json=[endpoint])
        if not resp.is_success:
            raise KeepAliveError(
                f"KeepAliveAssignment failed: HTTP {resp.status_code} {resp.text[:300]!r}"
            )

    async def tunnel_keep_alive(self, endpoint: str, *, timeout: float = 10.0) -> None:
        """Send one tunnel keep-alive ping for ``endpoint`` — the google-colab-cli recipe.

        ``GET {domain}/tun/m/<endpoint>/keep-alive/`` with header ``X-Colab-Tunnel: Google``.
        Unlike the RuntimeService RPC (:meth:`keep_alive`, unusable under token auth), this
        works with the ordinary bearer token. The tunnel holds the request open, so the
        official client treats a ``ReadTimeout`` as **success** (the lease is refreshed
        server-side regardless) — we do the same. A non-timeout, non-2xx response is a real
        failure. Live-validate it holds a runtime past idle before trusting it (see
        ``spikes/phase_b_keepalive.py``).
        """
        url = f"{self._domain}{TUN_ENDPOINT}/{endpoint}/keep-alive/"
        headers = {**await self._auth_headers(), TUNNEL_HEADER: TUNNEL_HEADER_VALUE}
        try:
            resp = await self._http.get(url, headers=headers, timeout=timeout)
        except httpx.ReadTimeout:
            return  # the tunnel held the connection open → lease refreshed → success
        if not resp.is_success:
            raise KeepAliveError(
                f"tunnel keep-alive failed for {endpoint!r}: "
                f"HTTP {resp.status_code} {resp.text[:200]!r}"
            )

    @staticmethod
    def proxy_kernel_headers(proxy_token: str) -> dict[str, str]:
        """Headers for the Jupyter-websocket kernel connection (verified recipe)."""
        return {CLIENT_AGENT_HEADER: CLIENT_AGENT, PROXY_TOKEN_HEADER: proxy_token}

    @staticmethod
    def proxy_ws_params(proxy_token: str) -> dict[str, str]:
        """Extra websocket query params for the kernel connection (verified recipe)."""
        return {"colab-runtime-proxy-token": proxy_token}


__all__ = [
    "COLAB_API_DOMAIN",
    "COLAB_DOMAIN",
    "PUBLIC_API_KEY",
    "PUBLIC_API_KEY_HEADER",
    "ColabBackendClient",
    "RuntimeProxyInfo",
    "build_assign_params",
    "keepalive_headers",
    "strip_xssi",
    "web_safe_nbh",
]
