#!/usr/bin/env python3
"""
普通微信（ClawBot / iLink）通道的离线自测：本机模拟微信服务器和 CDN，不需要真实微信。
运行：.venv/bin/python tests/selftest_weixin.py
"""
import asyncio
import base64
import builtins
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402
sys.modules.setdefault("bot", bot)
import weixin_channel as wx  # noqa: E402

OWNER = "owner@im.wechat"
TOKEN = "TEST-BOT-TOKEN"
PNG = b"\x89PNG\r\n\x1a\n" + os.urandom(200)
PDF = b"%PDF-1.7 weekly report"
SILK = b"#!SILK_V3" + os.urandom(50)
IMG_KEY, FILE_KEY, SILK_KEY = os.urandom(16), os.urandom(16), os.urandom(16)


def ecb(data, key):
    pad = 16 - len(data) % 16
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data + bytes([pad]) * pad) + enc.finalize()


MEDIA = {"IMG1": ecb(PNG, IMG_KEY), "FILE1": ecb(PDF, FILE_KEY), "SILK1": ecb(SILK, SILK_KEY)}


def umsg(i, items, user=OWNER, mtype=1, **extra):
    return {"message_id": 7000000000000000000 + i, "from_user_id": user, "to_user_id": "bot@im",
            "message_type": mtype, "message_state": 2, "context_token": f"ctx{i}",
            "item_list": items, **extra}


def text(t):
    return {"type": 1, "text_item": {"text": t}}


SCRIPT = [
    umsg(1, [text("Transformer 的注意力本质是加权求和 #AI")]),
    umsg(2, [{"type": 3, "voice_item": {"text": "明天上午十点开项目周会", "playtime": 2300}}]),
    umsg(3, [text("踩坑：pip 要加 --break-system-packages")]),
    umsg(3, [text("踩坑：pip 要加 --break-system-packages")]),            # 重复推送
    umsg(4, [{"type": 2, "image_item": {"aeskey": IMG_KEY.hex(),
                                         "media": {"encrypt_query_param": "IMG1"}}}]),
    umsg(5, [{"type": 4, "file_item": {"file_name": "周报.pdf", "media": {
        "full_url": "__BASE__/c2c/download?encrypted_query_param=FILE1",
        # 文件的 aes_key：base64(32 位 hex 字符串)
        "aes_key": base64.b64encode(FILE_KEY.hex().encode()).decode()}}}]),
    umsg(6, [{"type": 3, "voice_item": {"media": {
        "encrypt_query_param": "SILK1", "aes_key": base64.b64encode(SILK_KEY).decode()}}}]),
    umsg(7, [text("同意这个思路 #工作"), ], ),
    umsg(8, [dict(text("随手一记"), ref_msg={"message_item": text("被引用的话")})]),
    umsg(9, [text("陌生人的消息")], user="stranger@im.wechat"),
    umsg(10, [text("机器人自己发的")], mtype=2),
    umsg(11, [text("这条马上撤销")]),
    umsg(12, [text("/撤销")]),
    umsg(13, [text("/今天")]),
]


class Server(BaseHTTPRequestHandler):
    sent, polls, qr_polls, headers_seen = [], [0], [0], []
    queue = list(SCRIPT)
    errored = [False]

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/ilink/bot/get_qrcode_status":
            n = self.qr_polls[0]
            self.qr_polls[0] += 1
            if n == 0:
                return self._json({"status": "wait"})
            if n == 1:
                return self._json({"status": "scaned"})
            if q.get("verify_code") != ["1234"]:
                return self._json({"status": "need_verifycode"})
            return self._json({"status": "confirmed", "bot_token": TOKEN, "ilink_bot_id": "bot@im",
                               "ilink_user_id": OWNER, "baseurl": BASE})
        if u.path == "/c2c/download":
            data = MEDIA[q["encrypted_query_param"][0]]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        self._json({"err": "not found"}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        u = urlparse(self.path)
        assert body["base_info"]["channel_version"], body
        if u.path == "/ilink/bot/get_bot_qrcode":
            return self._json({"qrcode": "QR-KEY", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/test"})
        # 以下接口需要登录凭证
        self.headers_seen.append(dict(self.headers))
        assert self.headers["Authorization"] == f"Bearer {TOKEN}", self.headers
        assert self.headers["AuthorizationType"] == "ilink_bot_token"
        assert self.headers["iLink-App-Id"] == "bot"
        base64.b64decode(self.headers["X-WECHAT-UIN"])
        if u.path == "/ilink/bot/msg/notifystart":
            return self._json({"ret": 0})
        if u.path == "/ilink/bot/getupdates":
            self.polls[0] += 1
            if not self.errored[0]:            # 第一次返回错误，测试重试
                self.errored[0] = True
                return self._json({"ret": -1, "errmsg": "temporary"})
            cur = int(body["get_updates_buf"] or 0)
            if cur < len(self.queue):
                m = self.queue[cur]
                return self._json({"ret": 0, "msgs": [m], "get_updates_buf": str(cur + 1),
                                   "longpolling_timeout_ms": 2000})
            time.sleep(0.3)
            return self._json({"ret": 0, "msgs": [], "get_updates_buf": str(cur)})
        if u.path == "/ilink/bot/sendmessage":
            self.sent.append(body["msg"])
            return self._json({"ret": 0, "message_id": 1})
        self._json({"err": "not found"}, 404)


async def main():
    global BASE
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Server)
    BASE = f"http://127.0.0.1:{srv.server_port}"
    for m in SCRIPT:
        for it in m["item_list"]:
            fi = it.get("file_item")
            if fi:
                fi["media"]["full_url"] = fi["media"]["full_url"].replace("__BASE__", BASE)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp())
    cfg_text = (ROOT / "config.example.toml").read_text("utf-8")
    cfg_text = cfg_text.replace('path = "~/知识库"', f'path = "{tmp / "vault"}"')
    cfg_text = cfg_text.replace("[weixin]", f'[weixin]\napi_base_url = "{BASE}"\ncdn_base_url = "{BASE}/c2c"')
    (tmp / "config.toml").write_text(cfg_text, "utf-8")
    bot.BASE_DIR = tmp
    cfg = bot.Config(tmp / "config.toml")
    assert cfg.channel == "weixin"
    assert cfg.validate(), "未登录时应提示需要扫码"

    # ── 1. 扫码登录（含手机上显示数字、需要输入验证码的情况）──
    # 测试用的是假二维码，不要在屏幕上显示或自动打开，免得被误扫
    wx._show_qr = lambda content: None
    real_input = builtins.input
    builtins.input = lambda prompt="": "1234"
    try:
        rc = await asyncio.to_thread(wx.login, cfg)
    finally:
        builtins.input = real_input
    assert rc == 0, "登录应成功"
    tok = json.loads(cfg.weixin_token_file.read_text())
    assert tok["bot_token"] == TOKEN and tok["ilink_user_id"] == OWNER
    assert oct(cfg.weixin_token_file.stat().st_mode)[-3:] == "600"
    assert not cfg.validate(), "登录后配置检查应通过"
    print("✓ 扫码登录（含验证码）")

    # ── 2. 收发消息 ──
    b = wx.WeixinBot(cfg)
    task = asyncio.create_task(b.run_forever())
    for _ in range(200):
        await asyncio.sleep(0.05)
        if len(Server.sent) >= 11 and int(b.cursor or 0) >= len(SCRIPT):
            break
    await asyncio.sleep(0.5)
    task.cancel()

    replies = {m["context_token"]: m["item_list"][0]["text_item"]["text"] for m in Server.sent}
    for k in sorted(replies, key=lambda x: int(x[3:])):
        print(f"[{k}] {replies[k]}")

    vault = tmp / "vault"

    def notes(d):
        p = vault / d
        return {f.name: f.read_text("utf-8") for f in p.glob("*.md")} if p.is_dir() else {}

    def find(d, needle):
        hits = [c for c in notes(d).values() if needle in c]
        assert len(hits) == 1, f"{d} 中应恰好一篇含 {needle!r}，实际 {len(hits)}"
        return hits[0]

    find("01-学习/AI", "加权求和 #AI")
    v = find("00-收件箱", "明天上午十点")
    assert "type: voice" in v and "source: 微信" in v and f"author: {OWNER}" in v
    assert "明天上午十点" in replies["ctx2"], "语音回复应附上转写文字"
    assert len(notes("01-学习/踩坑笔记")) == 1, "重复消息只存一次"
    att = vault / "00-收件箱/附件"
    assert any(p.read_bytes() == PNG and p.suffix == ".png" for p in att.iterdir()), "图片应解密正确"
    assert (att / "周报.pdf").read_bytes() == PDF, "文件应解密正确并保留文件名"
    assert any(p.suffix == ".silk" and p.read_bytes() == SILK for p in att.iterdir()), "未转写语音应保存原文件"
    find("00-收件箱", "未能自动转文字")
    find("02-工作/工作日志", "同意这个思路 #工作")
    q = find("00-收件箱", "随手一记")
    assert "> 引用：被引用的话" in q
    assert not any("陌生人" in c for c in notes("00-收件箱").values()), "陌生人消息应被忽略"
    assert "ctx9" not in replies and "ctx10" not in replies
    assert not any("这条马上撤销" in c for c in notes("00-收件箱").values()), "撤销应生效"
    assert "撤销" in replies["ctx12"]
    assert "加权求和" in replies["ctx13"] and "**" not in replies["ctx13"], "/今天 应为纯文本"
    assert all(m["message_type"] == 2 and m["to_user_id"] == OWNER for m in Server.sent)
    assert (tmp / "weixin_cursor.txt").read_text() == str(len(SCRIPT)), "游标应持久化"
    assert Server.polls[0] >= 2, "出错后应重试"
    print("\n✅ 普通微信通道测试全部通过")
    srv.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
