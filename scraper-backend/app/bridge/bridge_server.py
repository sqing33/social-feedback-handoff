"""XHS Bridge WebSocket 桥接服务（FastAPI 版，跑在主应用 8001 上）。

协议（与 XHS Bridge Chrome 扩展兼容）：
- 扩展连上发 { role: "extension", version, capabilities }
- 服务端回 { type: "ready", generation }
- 服务端发命令：{ id, method, params }
- 扩展回 { id, result } 或 { id, error }
- 心跳：扩展 { type: "heartbeat" } ↔ 服务端 { type: "heartbeat_ack" }

API：
- bridge.call(method, params) — 同步向扩展发命令并等结果
- bridge.is_extension_connected — 状态
- bridge.wait_ready(timeout) — 等扩展连上
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("scraper.bridge")


@dataclass
class _Pending:
    future: asyncio.Future
    generation: int


class BridgeServer:
    def __init__(self) -> None:
        self._extension_ws: WebSocket | None = None
        self._extension_generation: int = 0
        self._extension_connected_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._pending: dict[str, _Pending] = {}
        self._ready_event = asyncio.Event()
        self._lock = asyncio.Lock()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @property
    def is_extension_connected(self) -> bool:
        return self._extension_ws is not None

    async def wait_ready(self, timeout: float = 5.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float = 60.0,
    ) -> Any:
        if not self._extension_ws:
            raise RuntimeError("extension not connected")
        msg_id = f"m-{method}-{asyncio.get_event_loop().time()}"
        generation = self._extension_generation
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = _Pending(future=fut, generation=generation)
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}}, ensure_ascii=False)
        try:
            await self._extension_ws.send_text(payload)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(msg_id, None)

    async def handle(self, ws: WebSocket) -> None:
        """FastAPI WebSocket 入口。"""
        await ws.accept()
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except Exception:
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, Exception):
            return
        if msg.get("role") != "extension":
            return
        await self._handle_extension(ws)

    async def _handle_extension(self, ws: WebSocket) -> None:
        previous = self._extension_ws
        self._extension_generation += 1
        generation = self._extension_generation
        self._extension_ws = ws
        self._extension_connected_at = self._now()
        self._last_heartbeat_at = self._extension_connected_at
        self._ready_event.set()
        logger.info("Extension connected generation=%d", generation)
        try:
            if previous is not None and previous is not ws:
                try:
                    await previous.close(code=4000, reason="replaced")
                except Exception:
                    pass
            await ws.send_text(json.dumps({"type": "ready", "generation": generation}))
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "heartbeat":
                    if self._extension_ws is ws:
                        self._last_heartbeat_at = self._now()
                        await ws.send_text(json.dumps({"type": "heartbeat_ack", "generation": generation}))
                    continue
                msg_id = msg.get("id")
                pending = self._pending.get(msg_id) if msg_id else None
                if pending and pending.generation == generation:
                    if "error" in msg:
                        if not pending.future.done():
                            pending.future.set_exception(RuntimeError(msg["error"]))
                    else:
                        if not pending.future.done():
                            pending.future.set_result(msg.get("result"))
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("bridge session error: %s", e)
        finally:
            if self._extension_ws is ws:
                self._extension_ws = None
                self._ready_event.clear()
                logger.info("Extension disconnected")


# 全局单例
bridge = BridgeServer()
