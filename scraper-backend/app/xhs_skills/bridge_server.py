"""XHS Extension Bridge Server

Extension 连接到这里（WebSocket），CLI 命令通过同一端口发送（role=cli），
Bridge 将命令路由给 Extension 并把结果返回给 CLI。

启动方式：
    python scripts/bridge_server.py

端口：9333（可通过 --port 覆盖）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import websockets
from websockets.server import ServerConnection

logger = logging.getLogger("xhs-bridge")


@dataclass
class PendingCommand:
    future: asyncio.Future[Any]
    generation: int


class BridgeServer:
    def __init__(self) -> None:
        self._extension_ws: ServerConnection | None = None
        self._extension_generation = 0
        self._extension_connected_at: str | None = None
        self._last_heartbeat_at: str | None = None
        self._extension_ready = asyncio.Event()
        self._pending: dict[str, PendingCommand] = {}

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _error(code: str, message: str) -> str:
        return json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)

    async def handle(self, ws: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("握手超时或失败: %s", e)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        role = msg.get("role")
        if role == "extension":
            await self._handle_extension(ws)
        elif role == "cli":
            await self._handle_cli(ws, msg)
        else:
            logger.warning("未知 role: %s", role)

    # ─── Extension 端（长连接） ───────────────────────────────────────

    async def _handle_extension(self, ws: ServerConnection) -> None:
        previous = self._extension_ws
        self._extension_generation += 1
        generation = self._extension_generation
        self._extension_ws = ws
        self._extension_connected_at = self._now()
        self._last_heartbeat_at = self._extension_connected_at
        self._extension_ready.set()
        logger.info("Extension 已连接（generation=%d）", generation)
        try:
            if previous is not None and previous is not ws:
                await previous.close(code=4000, reason="replaced by newer extension connection")
            await ws.send(json.dumps({"type": "ready", "generation": generation}))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") == "heartbeat":
                    if self._extension_ws is not ws:
                        continue
                    self._last_heartbeat_at = self._now()
                    await ws.send(json.dumps({"type": "heartbeat_ack", "generation": generation}))
                    continue
                msg_id = msg.get("id")
                pending = self._pending.get(msg_id) if msg_id else None
                if pending and pending.generation == generation:
                    self._pending.pop(msg_id, None)
                    if not pending.future.done():
                        pending.future.set_result(msg)
        finally:
            if self._extension_ws is ws:
                self._extension_ws = None
                self._extension_ready.clear()
                logger.info("Extension 已断开（generation=%d）", generation)
            stale_ids = [
                msg_id
                for msg_id, pending in self._pending.items()
                if pending.generation == generation
            ]
            for msg_id in stale_ids:
                pending = self._pending.pop(msg_id)
                if not pending.future.done():
                    pending.future.set_exception(ConnectionError("Extension 断开连接"))

    # ─── CLI 端（短连接，发一条命令，收一条回复） ─────────────────────

    async def _handle_cli(self, ws: ServerConnection, msg: dict) -> None:
        # 特殊命令：查询 server/extension 状态，无需转发
        if msg.get("method") == "ping_server":
            await ws.send(json.dumps({
                "result": {
                    "extension_connected": self._extension_ws is not None,
                    "generation": self._extension_generation,
                    "connected_at": self._extension_connected_at,
                    "last_heartbeat_at": self._last_heartbeat_at,
                }
            }))
            return

        if not self._extension_ws:
            try:
                await asyncio.wait_for(self._extension_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                await ws.send(self._error(
                    "EXTENSION_DISCONNECTED",
                    "Extension 未连接，等待 5 秒仍未恢复",
                ))
                return

        extension_ws = self._extension_ws
        generation = self._extension_generation
        if extension_ws is None:
            await ws.send(self._error("EXTENSION_DISCONNECTED", "Extension 未连接"))
            return

        msg_id = str(uuid.uuid4())
        msg["id"] = msg_id

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = PendingCommand(future=future, generation=generation)

        try:
            await extension_ws.send(json.dumps(msg))
        except Exception as exc:
            self._pending.pop(msg_id, None)
            await ws.send(self._error("EXTENSION_DISCONNECTED", f"命令发送失败：{exc}"))
            return

        try:
            result = await asyncio.wait_for(future, timeout=90.0)
            await ws.send(json.dumps(result))
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            await ws.send(self._error("COMMAND_TIMEOUT", "命令执行超时（90s）"))
        except ConnectionError as e:
            await ws.send(self._error("EXTENSION_DISCONNECTED", str(e)))


async def main(port: int) -> None:
    # Suppress noisy websockets connection open/closed logs (extension polls frequently).
    logging.getLogger("websockets.server").setLevel(logging.WARNING)
    server = BridgeServer()
    async with websockets.serve(
        server.handle,
        "localhost",
        port,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
    ):
        logger.info("Bridge server 已启动: ws://localhost:%d", port)
        logger.info("等待浏览器扩展连接...")
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="XHS Extension Bridge Server")
    parser.add_argument("--port", type=int, default=9333, help="监听端口（默认 9333）")
    args = parser.parse_args()

    asyncio.run(main(args.port))
