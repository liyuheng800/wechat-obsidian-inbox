#!/usr/bin/env python3
"""
离线自测：在本机模拟企业微信长连接服务器，不需要真实账号。
运行：.venv/bin/python tests/selftest.py
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from websockets.asyncio.server import serve

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402

KEY = os.urandom(32)
AESKEY = base64.b64encode(KEY).decode().rstrip("=")
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(100)
PDF = b"%PDF-1.4 test document"


def encrypt(data):
    pad = 32 - len(data) % 32
    data += bytes([pad]) * pad
    enc = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
    return enc.update(data) + enc.finalize()


class Media(BaseHTTPRequestHandler):
    def do_GET(self):
        body = encrypt(PNG if self.path == "/img" else PDF)
        self.send_response(200)
        if self.path == "/file":
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%E5%91%A8%E6%8A%A5.pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def msg(i, mtype, payload, **extra):
    body = {"msgid": f"m{i}", "aibotid": "B", "chattype": "single",
            "from": {"userid": "me"}, "msgtype": mtype, mtype: payload, **extra}
    return {"cmd": "aibot_msg_callback", "headers": {"req_id": f"r{i}"}, "body": body}


async def run():
    http = ThreadingHTTPServer(("127.0.0.1", 0), Media)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{http.server_port}"

    replies = {}
    done = asyncio.Event()
    got_ping = asyncio.Event()

    async def handler(ws):
        sub = json.loads(await ws.recv())
        assert sub["cmd"] == "aibot_subscribe" and sub["body"]["secret"] == "S", sub
        await ws.send(json.dumps({"headers": sub["headers"], "errcode": 0, "errmsg": "ok"}))

        await ws.send(json.dumps({"cmd": "aibot_event_callback", "headers": {"req_id": "welcome"},
                                  "body": {"msgid": "e1", "msgtype": "event",
                                           "event": {"eventtype": "enter_chat"}}}))
        script = [
            msg(1, "text", {"content": "Transformer 的注意力本质是加权求和 #AI"}),
            msg(2, "voice", {"content": "明天上午十点开项目周会"}),
            msg(3, "text", {"content": "工作：完成接口联调"}),
            msg(4, "text", {"content": "踩坑：pip 要加 --break-system-packages"}),
            msg(4, "text", {"content": "踩坑：pip 要加 --break-system-packages"}),  # 重复回调
            msg(5, "text", {"content": "随手一记\n第二行"},
                quote={"msgtype": "text", "text": {"content": "被引用的话"}}),
            msg(6, "image", {"url": base + "/img", "aeskey": AESKEY}),
            msg(7, "file", {"url": base + "/file", "aeskey": AESKEY}),
            msg(8, "mixed", {"msg_item": [
                {"msgtype": "text", "text": {"content": "白板照片 #工作"}},
                {"msgtype": "image", "image": {"url": base + "/img", "aeskey": AESKEY}}]}),
            msg(9, "text", {"content": "这条马上撤销"}),
            msg(10, "text", {"content": "/撤销"}),
            msg(11, "text", {"content": "/今天"}),
            msg(12, "text", {"content": "/我是谁"}),
        ]
        for m in script:
            await ws.send(json.dumps(m, ensure_ascii=False))
            await asyncio.sleep(0.3)

        async def reader():
            async for raw in ws:
                d = json.loads(raw)
                if d["cmd"] == "ping":
                    await ws.send(json.dumps({"headers": d["headers"], "errcode": 0}))
                    got_ping.set()
                else:
                    replies.setdefault(d["headers"]["req_id"], []).append(d)
                if len(replies) >= 13:
                    done.set()
        await reader()

    async def collect_while_sending(ws):
        await handler(ws)

    tmp = Path(tempfile.mkdtemp())
    async with serve(collect_while_sending, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg_path = tmp / "config.toml"
        example = (ROOT / "config.example.toml").read_text("utf-8")
        example = example.replace('"填你的 Bot ID"', '"B"').replace('"填你的 Secret"', '"S"')
        example = example.replace('path = "~/知识库"', f'path = "{tmp / "vault"}"')
        example = example.replace("\n[wecom]\n", f'\n[wecom]\nws_url = "ws://127.0.0.1:{port}"\n').replace('channel = "weixin"', 'channel = "wecom"')
        cfg_path.write_text(example, "utf-8")

        bot.BASE_DIR = tmp
        bot.HEARTBEAT_SECONDS = 1
        b = bot.Bot(bot.Config(cfg_path))
        task = asyncio.create_task(b.run_forever())
        await asyncio.wait_for(done.wait(), timeout=20)
        await asyncio.wait_for(got_ping.wait(), timeout=5)
        task.cancel()

    def text(rid):
        r = replies[rid][0]["body"]
        return r.get("stream", {}).get("content") or r.get("text", {}).get("content")

    for rid in sorted(replies, key=lambda x: (len(x), x)):
        print(f"[{rid}] {text(rid)}")
    print()

    vault = tmp / "vault"
    for f in sorted(vault.rglob("*.md")):
        print(f"── {f.relative_to(vault)}")
        print(f.read_text("utf-8"))
    print("附件：", sorted(p.name for p in (vault / "00-收件箱/附件").iterdir()))

    # 断言
    def notes(d):
        p = vault / d
        return {f.name: f.read_text("utf-8") for f in p.glob("*.md")} if p.is_dir() else {}

    def find(d, needle):
        hits = [c for c in notes(d).values() if needle in c]
        assert len(hits) == 1, f"{d} 里应恰好有一篇包含 {needle!r}，实际 {len(hits)}"
        return hits[0]

    find("01-学习/AI", "加权求和 #AI")
    v = find("00-收件箱", "明天上午十点")
    assert "type: voice" in v and "author: me" in v and "created: " in v
    find("02-工作/工作日志", "完成接口联调 #工作")
    w = find("02-工作/工作日志", "白板照片 #工作")
    assert "![[" in w
    assert len(notes("01-学习/踩坑笔记")) == 1, "重复消息应只存一次"
    q = find("00-收件箱", "随手一记")
    assert "第二行" in q and "> 引用：被引用的话" in q
    assert not any("这条马上撤销" in c for c in notes("00-收件箱").values())
    find("00-收件箱", "周报.pdf")
    assert any(n.endswith("图片.md") for n in notes("00-收件箱"))
    att = vault / "00-收件箱/附件"
    assert (att / "周报.pdf").read_bytes() == PDF
    assert any(p.suffix == ".png" and p.read_bytes() == PNG for p in att.iterdir())
    assert "m4" in json.loads((tmp / "state.json").read_text())["seen"]
    assert replies["welcome"][0]["cmd"] == "aibot_respond_welcome_msg"
    assert "me" in text("r12")
    assert "加权求和" in text("r11") and "明天上午十点" in text("r11")
    assert "r4" in replies and len(replies["r4"]) == 1
    # 机器人只新建文件：所有笔记的文件名都不同，且没有残留临时文件
    assert not list(vault.rglob("*.tmp"))
    print("\n✅ 全部测试通过")
    http.shutdown()


if __name__ == "__main__":
    asyncio.run(run())
