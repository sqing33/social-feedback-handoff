"""模拟 XHS Bridge Chrome 扩展连后端 ws://localhost:8001/ws/bridge。

握手 + 响应 evaluate / ping 命令，验证后端 WS 协议跑通。
"""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time


WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_connect(host: str, port: int, path: str) -> socket.socket:
    s = socket.create_connection((host, port), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Origin: http://{host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("closed during handshake")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise RuntimeError(f"handshake failed: {head!r}")
    return s


def ws_send_text(s, msg):
    data = msg.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    header = bytearray()
    header.append(0x81)
    n = len(data)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack(">H", n))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack(">Q", n))
    header.extend(mask)
    s.sendall(bytes(header) + masked)


def ws_recv_text(s, prebuf=b""):
    buf = prebuf

    def read_exact(n):
        nonlocal buf
        while len(buf) < n:
            chunk = s.recv(4096)
            if not chunk:
                raise RuntimeError("closed")
            buf += chunk
        out = buf[:n]
        buf = buf[n:]
        return out

    b1, b2 = read_exact(2)
    opcode = b1 & 0x0F
    if opcode == 0x8:
        raise RuntimeError("server closed")
    if opcode == 0x9:  # ping
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


def main():
    host = "127.0.0.1"
    port = 8001
    path = "/api/ws/bridge"
    print(f"connecting ws://{host}:{port}{path}")
    s = ws_connect(host, port, path)
    print("handshake OK")

    # 1. 读 ready
    text, _ = ws_recv_text(s)
    print("<-", text)

    # 2. 发 extension handshake
    ws_send_text(s, json.dumps({
        "role": "extension",
        "version": "1.1.0",
        "capabilities": ["ping", "evaluate", "navigate", "get_cookies"],
    }))
    print("-> handshake sent")

    # 3. 处理后续消息
    for _ in range(20):
        try:
            text, _ = ws_recv_text(s)
        except Exception as e:
            print("recv closed:", e)
            break
        msg = json.loads(text)
        print("<-", msg)

        if msg.get("type") == "ready":
            # 收到 ready 后发一个 evaluate 命令测试
            ws_send_text(s, json.dumps({
                "id": "eval-1",
                "method": "evaluate",
                "params": {"expression": "1 + 1"},
            }))
            print("-> evaluate sent")
        elif msg.get("id") == "eval-1":
            print("result:", msg.get("result"))
            break
        elif msg.get("id") and msg.get("error"):
            print("error:", msg.get("error"))
            break

    s.close()
    print("done")


if __name__ == "__main__":
    main()
