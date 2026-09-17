"""Authenticated local Bridge for the safe XHS extension.

Only the extension and the formal scraper may connect. The server never sees
cookies, request bodies, response bodies, or arbitrary JavaScript.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any

import websockets
from websockets.server import ServerConnection

from .protocol import SAFE_METHODS, sanitize_result, validate_method, validate_params
from .runtime import enabled as bridge_enabled, token as runtime_token

logger = logging.getLogger("scraper.xhs_bridge.server")


class SafeBridgeServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 9333, token: str = "") -> None:
        self.host = host
        self.port = int(port)
        self.token = str(token or "")
        self._server = None
        self._extension_ws: ServerConnection | None = None
        self._extension_generation = 0
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    @staticmethod
    def _safe_error(code: str, message: str) -> str:
        return json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)

    def _auth_ok(self, value: Any) -> bool:
        candidate = str(value or "").encode("utf-8")
        expected = self.token.encode("utf-8")
        return bool(expected) and hmac.compare_digest(candidate, expected)

    async def start(self) -> None:
        if not self.enabled or self._server is not None:
            return
        self._server = await websockets.serve(
            self.handle,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=2,
            max_size=1_000_000,
        )
        logger.info("safe XHS Bridge listening on loopback port %d", self.port)

    async def stop(self) -> None:
        if self._extension_ws is not None:
            try:
                await self._extension_ws.close(code=1001, reason="server stopping")
            except Exception:
                pass
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("XHS Bridge stopped"))
        self._pending.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._extension_ws = None
        self._ready.clear()

    async def handle(self, ws: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            message = json.loads(raw)
        except Exception:
            logger.info("safe XHS Bridge rejected malformed handshake")
            return
        if not isinstance(message, dict) or not self._auth_ok(message.get("auth")):
            logger.info("safe XHS Bridge rejected unauthorized handshake")
            try:
                await ws.send(self._safe_error("BRIDGE_UNAUTHORIZED", "XHS Bridge authentication failed"))
                await ws.close(code=4001, reason="unauthorized")
            except Exception:
                pass
            return
        role = message.get("role")
        if role == "extension":
            await self._handle_extension(ws)
        elif role == "scraper":
            await self._handle_scraper(ws, message)
        else:
            await ws.send(self._safe_error("BRIDGE_BAD_ROLE", "unsupported Bridge role"))

    async def _handle_extension(self, ws: ServerConnection) -> None:
        previous = self._extension_ws
        self._extension_generation += 1
        generation = self._extension_generation
        self._extension_ws = ws
        self._ready.set()
        logger.info("safe XHS extension connected (generation=%d)", generation)
        if previous is not None and previous is not ws:
            try:
                await previous.close(code=4000, reason="replaced")
            except Exception:
                pass
        try:
            await ws.send(json.dumps({"type": "ready", "generation": generation}))
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message.get("type") == "heartbeat":
                    await ws.send(json.dumps({"type": "heartbeat_ack"}))
                    continue
                request_id = message.get("id")
                future = self._pending.get(request_id) if request_id else None
                if future is not None and not future.done():
                    self._pending.pop(request_id, None)
                    future.set_result(message)
        finally:
            if self._extension_ws is ws:
                self._extension_ws = None
                self._ready.clear()
                logger.info("safe XHS extension disconnected (generation=%d)", generation)
            stale = list(self._pending.items())
            for request_id, future in stale:
                self._pending.pop(request_id, None)
                if not future.done():
                    future.set_exception(ConnectionError("XHS extension disconnected"))

    async def _handle_scraper(self, ws: ServerConnection, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        try:
            validate_method(method)
            params = validate_params(method, message.get("params"))
        except ValueError:
            await ws.send(self._safe_error("BRIDGE_BAD_REQUEST", "Bridge method or params are not allowed"))
            return
        if method == "ping_server":
            await ws.send(json.dumps({
                "result": {
                    "extension_connected": self._extension_ws is not None,
                    "generation": self._extension_generation,
                    "allowed_methods": sorted(SAFE_METHODS),
                }
            }))
            return
        if self._extension_ws is None:
            try:
                await asyncio.wait_for(self._ready.wait(), timeout=2)
            except asyncio.TimeoutError:
                await ws.send(self._safe_error("EXTENSION_DISCONNECTED", "safe XHS extension is not connected"))
                return
        extension = self._extension_ws
        if extension is None:
            await ws.send(self._safe_error("EXTENSION_DISCONNECTED", "safe XHS extension is not connected"))
            return
        request_id = str(message.get("id") or "")
        if not request_id:
            await ws.send(self._safe_error("BRIDGE_BAD_REQUEST", "Bridge request id is required"))
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[request_id] = future
        outbound = {
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            await extension.send(json.dumps(outbound, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=8)
            if not isinstance(response, dict) or response.get("error"):
                await ws.send(self._safe_error("XHS_PAGE_OPERATION_FAILED", "controlled XHS page operation failed"))
                return
            safe_result = sanitize_result(method, response.get("result"))
            await ws.send(json.dumps({"result": safe_result}, ensure_ascii=False))
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            await ws.send(self._safe_error("BRIDGE_TIMEOUT", "safe XHS extension command timed out"))
        except ConnectionError:
            self._pending.pop(request_id, None)
            await ws.send(self._safe_error("EXTENSION_DISCONNECTED", "safe XHS extension disconnected"))
        except Exception:
            self._pending.pop(request_id, None)
            await ws.send(self._safe_error("BRIDGE_ERROR", "safe XHS Bridge command failed"))


def bridge_from_environment() -> SafeBridgeServer | None:
    if not bridge_enabled():
        return None
    try:
        port = int(os.environ.get("XHS_BRIDGE_PORT", "9333"))
    except ValueError:
        port = 9333
    return SafeBridgeServer(
        host="127.0.0.1",
        port=port,
        token=runtime_token(),
    )
