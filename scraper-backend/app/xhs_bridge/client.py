"""Client for the restricted local XHS Bridge.

The client intentionally uses a short-lived WebSocket per command. It never
accepts arbitrary JavaScript and never serializes browser credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

import websockets.sync.client as ws_client

from .protocol import SAFE_METHODS, sanitize_result, validate_bridge_url, validate_method, validate_params
from .runtime import token as runtime_token

logger = logging.getLogger("scraper.xhs_bridge.client")


class SafeBridgeError(RuntimeError):
    """A safe Bridge transport or protocol error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class SafeXhsBridgeClient:
    """Async facade over a loopback-only, fixed-command Bridge server."""

    def __init__(
        self,
        bridge_url: str | None = None,
        token: str | None = None,
        *,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.bridge_url = validate_bridge_url(
            bridge_url or os.environ.get("XHS_BRIDGE_URL", "ws://127.0.0.1:9333")
        )
        self.token = str(token if token is not None else runtime_token())
        if not self.token:
            raise SafeBridgeError("BRIDGE_NOT_CONFIGURED", "XHS Bridge token is not configured")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 30.0))

    async def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        validate_method(method)
        safe_params = validate_params(method, params)
        payload = {
            "role": "scraper",
            "auth": self.token,
            "id": uuid.uuid4().hex,
            "method": method,
            "params": safe_params,
        }
        result = await asyncio.to_thread(self._call_sync, payload)
        return sanitize_result(method, result)

    def _call_sync(self, payload: dict[str, Any]) -> Any:
        try:
            with ws_client.connect(
                self.bridge_url,
                open_timeout=self.timeout_seconds,
                close_timeout=2,
                max_size=1_000_000,
            ) as ws:
                ws.send(json.dumps(payload, ensure_ascii=False))
                raw = ws.recv(timeout=self.timeout_seconds)
        except TimeoutError as exc:
            raise SafeBridgeError("BRIDGE_TIMEOUT", "XHS Bridge command timed out") from exc
        except OSError as exc:
            raise SafeBridgeError("BRIDGE_NOT_LISTENING", "XHS Bridge is not listening") from exc
        except Exception as exc:
            logger.debug("safe XHS Bridge transport failed: %s", type(exc).__name__)
            raise SafeBridgeError("BRIDGE_UNAVAILABLE", "XHS Bridge is unavailable") from exc

        try:
            response = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SafeBridgeError("BRIDGE_PROTOCOL_ERROR", "Invalid XHS Bridge response") from exc
        if response.get("error"):
            error = response["error"] if isinstance(response["error"], dict) else {}
            code = str(error.get("code") or "BRIDGE_ERROR")
            message = str(error.get("message") or "XHS Bridge rejected the command")
            raise SafeBridgeError(code, message)
        return response.get("result")

    async def ping(self) -> dict[str, Any]:
        result = await self.call("ping_server")
        return result if isinstance(result, dict) else {}

    async def get_page_state(self, note_id: str) -> dict[str, Any]:
        result = await self.call("get_xhs_page_state", {"note_id": str(note_id)})
        return result if isinstance(result, dict) else {}

    async def click_note_card(self, note_id: str) -> bool:
        result = await self.call("click_xhs_note_card", {"note_id": str(note_id)})
        return bool(result)


def bridge_from_environment() -> SafeXhsBridgeClient | None:
    """Create the client only when the opt-in flag and a token are present."""
    enabled = (os.environ.get("XHS_BRIDGE_ENABLED", "0") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    try:
        return SafeXhsBridgeClient()
    except (SafeBridgeError, ValueError) as exc:
        logger.warning("XHS safe Bridge disabled: %s", str(exc))
        return None
