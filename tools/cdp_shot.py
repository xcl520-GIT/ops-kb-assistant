# -*- coding: utf-8 -*-
"""用 Edge/Chrome 无头模式给前端页面截图（CDP over WebSocket，纯标准库）。

用途：给 README 出界面截图。先启动服务，再执行本脚本。

    python tools/cdp_shot.py --url http://127.0.0.1:8790 \
        --ask "Pod 一直 CrashLoopBackOff 怎么排查？" \
        --out docs/screenshot.png

参数：
    --url    页面地址
    --ask    可选。填了就在页面里自动提一个问题，等流式输出结束再截图
    --out    输出 PNG 路径
    --width / --height   视口尺寸
    --browser  浏览器可执行文件路径（默认自动探测 Edge / Chrome）
    --full   整页截图（默认开）；加 --no-full 则只截视口
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser(explicit: str | None) -> str:
    if explicit:
        return explicit
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    raise SystemExit("没找到 Edge/Chrome，请用 --browser 指定路径")


# --------------------------------------------------------------------------
# 极简 WebSocket 客户端（只实现文本帧，够用）
# --------------------------------------------------------------------------
class WS:
    def __init__(self, url: str) -> None:
        # ws://127.0.0.1:9333/devtools/page/XXXX
        assert url.startswith("ws://"), url
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        self.f = self.sock.makefile("rb")
        status = self.f.readline()
        if b"101" not in status:
            raise RuntimeError(f"WebSocket 握手失败: {status!r}")
        while True:
            line = self.f.readline()
            if line in (b"\r\n", b"\n", b""):
                break
        self._id = 0
        self._buf = b""

    def _send_frame(self, payload: bytes) -> None:
        n = len(payload)
        header = bytearray([0x81])  # FIN + text
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_frame(self) -> tuple[int, bytes]:
        hdr = self.f.read(2)
        if len(hdr) < 2:
            raise ConnectionError("连接已关闭")
        opcode = hdr[0] & 0x0F
        length = hdr[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.f.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.f.read(8))[0]
        data = b""
        while len(data) < length:
            chunk = self.f.read(length - len(data))
            if not chunk:
                raise ConnectionError("读取中断")
            data += chunk
        return opcode, data

    def call(self, method: str, params: dict | None = None, timeout: float = 60.0) -> dict:
        self._id += 1
        mid = self._id
        self._send_frame(json.dumps({"id": mid, "method": method, "params": params or {}}).encode())
        deadline = time.time() + timeout
        while True:
            if time.time() > deadline:
                raise TimeoutError(f"{method} 超时")
            opcode, data = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionError("服务端要求关闭")
            if opcode not in (0x1, 0x0):
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr: str, timeout: float = 30.0):
        r = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        }, timeout=timeout)
        return r.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------


def http_json(port: int, path: str, method: str = "GET") -> dict:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_port(port: int, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(port, "/json/version")
            return
        except Exception:
            time.sleep(0.4)
    raise SystemExit("浏览器调试端口没起来")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--ask", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=1480)
    ap.add_argument("--height", type=int, default=1500)
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--browser", default=None)
    ap.add_argument("--wait", type=float, default=90.0, help="等待回答结束的最长秒数")
    ap.add_argument("--no-full", action="store_true", help="只截视口")
    args = ap.parse_args()

    browser = find_browser(args.browser)
    profile = tempfile.mkdtemp(prefix="cdpshot-")
    proc = subprocess.Popen([
        browser,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={profile}",
        f"--window-size={args.width},{args.height}",
        "--force-device-scale-factor=1",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        wait_port(args.port)
        targets = http_json(args.port, "/json/list")
        page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            raise SystemExit("没找到页面 target")

        ws = WS(page["webSocketDebuggerUrl"])
        ws.call("Page.enable")
        ws.call("Runtime.enable")
        ws.call("Emulation.setDeviceMetricsOverride", {
            "width": args.width, "height": args.height,
            "deviceScaleFactor": 1, "mobile": False,
        })

        ws.call("Page.navigate", {"url": args.url})
        # 等 DOM 就绪
        for _ in range(120):
            time.sleep(0.35)
            try:
                if ws.eval("!!document.getElementById('input')"):
                    break
            except Exception:
                pass
        else:
            raise SystemExit("页面没渲染出来")

        time.sleep(1.5)  # 让首屏请求（配置、会话列表）跑完

        if args.ask:
            js = (
                "(()=>{const i=document.getElementById('input');"
                f"i.value={json.dumps(args.ask, ensure_ascii=False)};"
                "i.dispatchEvent(new Event('input',{bubbles:true}));"
                "i.dispatchEvent(new Event('change',{bubbles:true}));"
                "const b=document.getElementById('btnSend');"
                "if(b&&!b.disabled){b.click();return 'sent';}"
                "return 'blocked';})()"
            )
            print("提问:", ws.eval(js))
            # 等 stageText 空 且 消息区非空
            deadline = time.time() + args.wait
            stable = 0
            last = -1
            while time.time() < deadline:
                time.sleep(0.8)
                try:
                    stage = ws.eval("(document.getElementById('stageText')||{}).textContent||''")
                    length = ws.eval("document.getElementById('messages').innerText.length") or 0
                except Exception:
                    continue
                if not stage and length > 0:
                    if length == last:
                        stable += 1
                        if stable >= 3:
                            break
                    else:
                        stable = 0
                last = length
            print("回答长度:", last)

        # 回到顶部，等布局稳定
        ws.eval("window.scrollTo(0,0);"
                "const m=document.getElementById('messages'); if(m) m.scrollTop=0;")
        time.sleep(0.6)

        shot = ws.call("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": not args.no_full,
        }, timeout=60)
        data = base64.b64decode(shot["data"])
        out = os.path.abspath(args.out)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fp:
            fp.write(data)
        print(f"已保存 {out}  ({len(data)} bytes)")
        ws.close()
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
