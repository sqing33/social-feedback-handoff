"""最小 WS 客户端，模拟 XHS Bridge 扩展握手并测试 get_cookies 命令。"""

import asyncio
import base64
import hashlib
import json
import os
import socket
import struct


WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_connect(host: str, port: int, path: str) -> socket.socket:
    s = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    s.sendall(req.encode())
    # 读响应头
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise RuntimeError(f"handshake failed: {head!r}")
    return s


def ws_send_text(s: socket.socket, msg: str) -> None:
    data = msg.encode("utf-8")
    # client frames must be masked
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytearray()
    header.append(0x81)  # FIN + text
    length = len(data)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", length))
    header.extend(mask)
    s.sendall(bytes(header) + masked)


def ws_recv_text(s: socket.socket, prebuf: bytes = b"") -> tuple[str, bytes]:
    """return (text, leftover_bytes)"""
    buf = prebuf

    def read_exact(n: int) -> bytes:
        nonlocal buf
        while len(buf) < n:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("connection closed")
            buf += chunk
        out = buf[:n]
        buf = buf[n:]
        return out

    # parse frame header
    b1, b2 = read_exact(2)
    opcode = b1 & 0x0F
    if opcode == 0x8:  # close
        raise RuntimeError("server closed")
    if opcode == 0x9:  # ping — just ignore for now
        length = b2 & 0x7F
        if length == 126:
            read_exact(2)
        elif length == 127:
            read_exact(8)
        if length:
            read_exact(length)
        return ws_recv_text(s, buf)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", read_exact(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", read_exact(8))[0]
    payload = read_exact(length)
    return payload.decode("utf-8"), buf


def main() -> None:
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8001"))
    path = "/ws/bridge"

    print(f"connecting ws://{host}:{port}{path}")
    s = ws_connect(host, port, path)
    print("handshake OK")

    # 1. 读服务端的 ready
    text, _ = ws_recv_text(s)
    print("<-", text[:200])

    # 2. 发 extension handshake
    ws_send_text(
        s,
        json.dumps(
            {
                "role": "extension",
                "version": "1.1.0",
                "capabilities": ["heartbeat", "managed_tab_recycle", "runtime_status"],
            }
        ),
    )
    print("-> handshake sent")

    # 3. 发 get_cookies 命令
    cmd_id = "test-1"
    ws_send_text(
        s,
        json.dumps(
            {
                "id": cmd_id,
                "method": "get_cookies",
                "params": {"domain": ".xiaohongshu.com"},
            }
        ),
    )
    print(f"-> get_cookies sent (id={cmd_id})")

    # 4. 收响应
    text, _ = ws_recv_text(s)
    print("<-", text[:500])

    # 5. 关掉
    s.close()
    print("done")


if __name__ == "__main__":
    main()
