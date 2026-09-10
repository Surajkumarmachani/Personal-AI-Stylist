"""LiteLLM gateway client (Step 4.1).

Two distinct surfaces, deliberately separated:

  ADMIN  — create/read virtual keys and budgets, using the master key. Only the
           API touches this, at signup.
  CHAT   — make a model call using a TENANT'S virtual key. The worker uses
           this, and it never sees the master key.

That split is the whole point of per-tenant keys. If the worker held the master
key, a bug in the tag stage could spend every tenant's budget, and the spend
log could not attribute cost to anyone. With a virtual key per tenant, the
budget is enforced by the gateway on the credential itself — feature code
cannot exceed it even if it tries.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Tagging is async-plane work with no user waiting on it, so this is generous.
# The reranker (Phase 7) gets a hard 1200ms timeout for the opposite reason.
CHAT_TIMEOUT = 180.0
ADMIN_TIMEOUT = 15.0
CONNECT_TIMEOUT = 5.0


class LiteLLMUnavailable(RuntimeError):  # noqa: N818 - a state, not an error type
    """The gateway is unreachable or not ready. Backpressure, not a verdict."""

    def __init__(self, reason: str, retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after


class BudgetExhausted(RuntimeError):  # noqa: N818 - a state, not an error type
    """This tenant's virtual key is over its budget.

    NOT retryable and NOT an error to surface as a failure: §B3 makes budget
    exhaustion a feature downgrade. The caller degrades to the zero-cost path
    and the user keeps full cataloguing and deterministic boards.
    """


class ProviderError(RuntimeError):
    """The gateway reached a provider and the call failed anyway — no
    credentials, a refusal, a malformed request. Retryable a couple of times,
    then degrade."""


@dataclass(frozen=True, slots=True)
class ChatResult:
    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class VirtualKey:
    key: str
    user_id: uuid.UUID
    max_budget: float | None


class LiteLLMClient:
    def __init__(self, base_url: str, master_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._master_key = master_key

    def _timeout(self, read: float) -> httpx.Timeout:
        return httpx.Timeout(read, connect=CONNECT_TIMEOUT)

    # ---------------------------------------------------------- admin

    async def create_virtual_key(
        self, *, user_id: uuid.UUID, max_budget: float, duration: str = "30d"
    ) -> VirtualKey:
        """One key per tenant, created at signup.

        `budget_duration` gives LiteLLM the monthly reset, so nothing in our
        code has to remember to zero a counter — a cron that forgets to run
        would otherwise permanently lock every tenant out of tagging.
        """
        payload = {
            "user_id": str(user_id),
            "max_budget": max_budget,
            "budget_duration": duration,
            "metadata": {"tenant": str(user_id)},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout(ADMIN_TIMEOUT)) as client:
                resp = await client.post(
                    f"{self._base}/key/generate",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._master_key}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LiteLLMUnavailable(f"{type(exc).__name__}: {exc}") from exc

        if resp.status_code in (502, 503, 504):
            raise LiteLLMUnavailable(f"HTTP {resp.status_code} from gateway")
        resp.raise_for_status()
        body = resp.json()
        return VirtualKey(key=body["key"], user_id=user_id, max_budget=max_budget)

    async def key_info(self, key: str) -> dict[str, Any]:
        """Spend and budget for one virtual key.

        Used to REPORT cost per tenant, never to decide whether to allow a
        call: the gateway enforces the budget on the credential, so a check
        here would be advisory at best and a race at worst.
        """
        async with httpx.AsyncClient(timeout=self._timeout(ADMIN_TIMEOUT)) as client:
            resp = await client.get(
                f"{self._base}/key/info",
                params={"key": key},
                headers={"Authorization": f"Bearer {self._master_key}"},
            )
        resp.raise_for_status()
        return dict(resp.json())

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=self._timeout(5.0)) as client:
                resp = await client.get(f"{self._base}/health/liveliness")
            return resp.status_code == 200
        except Exception:
            return False

    # ----------------------------------------------------------- chat

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        api_key: str,
        response_format: dict[str, Any] | None = None,
        max_tokens: int = 2048,
    ) -> ChatResult:
        """One model call on a TENANT'S key.

        `api_key` is the tenant's virtual key, never the master key: that is
        what makes the budget enforceable and the spend attributable.
        """
        import time

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,
        }
        if response_format is not None:
            body["response_format"] = response_format

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self._timeout(CHAT_TIMEOUT)) as client:
                resp = await client.post(
                    f"{self._base}/v1/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise LiteLLMUnavailable(f"{type(exc).__name__}: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        if resp.status_code in (502, 503, 504):
            retry_after = resp.headers.get("Retry-After")
            raise LiteLLMUnavailable(
                f"HTTP {resp.status_code} from gateway",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if resp.status_code == 429:
            # LiteLLM returns 429 both for provider rate limits and for an
            # exhausted key budget. The body distinguishes them, and the two
            # need opposite handling: wait vs degrade permanently.
            detail = resp.text.lower()
            if "budget" in detail or "exceeded" in detail:
                raise BudgetExhausted(resp.text[:300])
            raise LiteLLMUnavailable("provider rate limited", retry_after=5.0)
        if resp.status_code >= 400:
            raise ProviderError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        payload = resp.json()
        choice = payload["choices"][0]
        usage = payload.get("usage") or {}
        return ChatResult(
            content=choice["message"]["content"] or "",
            model=payload.get("model", model),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            # LiteLLM reports its computed cost in a response header; the
            # gateway is the authority on price, not us.
            cost_usd=_header_float(resp, "x-litellm-response-cost"),
            latency_ms=latency_ms,
            cache_hit=resp.headers.get("x-litellm-cache-hit", "").lower() == "true",
        )


def _header_float(resp: httpx.Response, name: str) -> float | None:
    raw = resp.headers.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
