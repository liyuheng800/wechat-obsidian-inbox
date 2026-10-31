#!/usr/bin/env python3
"""
运维功能离线自测：邮件通知、备份、巡检告警与恢复、日报、会话失效后邮件扫码重新登录。
运行：.venv/bin/python tests/selftest_ops.py
（需要测试依赖 aiosmtpd：.venv/bin/pip install aiosmtpd）
"""
import asyncio
import email
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
from datetime import datetime
from email import policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402
sys.modules.setdefault("bot", bot)
import monitor  # noqa: E402
import weixin_channel as wx  # noqa: E402
from aiosmtpd.controller import Controller  # noqa: E402

# ───────── 假邮件服务器 ─────────
MAILS = []


class Handler:
    async def handle_DATA(self, server, session, envelope):
        MAILS.append(email.message_from_bytes(envelope.content, policy=policy.default))
        return "250 OK"


def _free_port():
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


SMTP_PORT = _free_port()
smtp = Controller(Handler(), hostname="127.0.0.1", port=SMTP_PORT)
smtp.start()


def subjects():
    return [m["Subject"] for m in MAILS]


# ───────── 假的微信 iLink 服务器 ─────────
OLD, NEW = "OLD-TOKEN", "NEW-TOKEN"


class Ilink(BaseHTTPRequestHandler):
    qr_polls = [0]
    sent = []
    tokens_seen = []

    def log_message(self, *a):
        pass

    def _json(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/ilink/bot/get_qrcode_status":
            self.qr_polls[0] += 1
            if self.qr_polls[0] < 3:
                return self._json({"status": "wait"})
            if self.qr_polls[0] == 3:
                return self._json({"status": "scaned"})
            return self._json({"status": "confirmed", "bot_token": NEW, "ilink_bot_id": "bot@im",
                               "ilink_user_id": "me", "baseurl": BASE})
        self._json({})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        u = urlparse(self.path)
        tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        if u.path == "/ilink/bot/get_bot_qrcode":
            assert body["local_token_list"] == [OLD], body
            return self._json({"qrcode": "QR1", "qrcode_img_content": "https://liteapp.weixin.qq.com/q/abc"})
        if u.path == "/ilink/bot/getupdates":
            self.tokens_seen.append(tok)
            if tok == OLD:
                return self._json({"ret": -14, "errcode": -14, "errmsg": "session timeout"})
            cur = int(body.get("get_updates_buf") or 0)
            if cur == 0:
                return self._json({"ret": 0, "get_updates_buf": "1", "msgs": [{
                    "message_id": 1, "from_user_id": "me", "message_type": 1, "context_token": "c1",
                    "item_list": [{"type": 1, "text_item": {"text": "恢复后的第一条 #AI"}}]}]})
            time.sleep(0.2)
            return self._json({"ret": 0, "get_updates_buf": str(cur)})
        if u.path == "/ilink/bot/sendmessage":
            self.sent.append(body["msg"])
        self._json({"ret": 0})


def make_env():
    tmp = Path(tempfile.mkdtemp())
    vault = tmp / "知识库"
    for d in ["00-收件箱", "01-学习/AI", ".obsidian"]:
        (vault / d).mkdir(parents=True)
    y = datetime.fromtimestamp(time.time() - 86400)
    (vault / f"00-收件箱/{y:%Y-%m-%d} 0930 昨天的笔记.md").write_text("昨天", "utf-8")
    (vault / f"01-学习/AI/{y:%Y-%m-%d} 1000 注意力.md").write_text("AI", "utf-8")
    (vault / "01-学习/AI/很早的笔记.md").write_text("old", "utf-8")
    (vault / ".obsidian/app.json").write_text("{}")
    cfg = (ROOT / "config.example.toml").read_text("utf-8")
    cfg = cfg.replace('path = "~/知识库"', f'path = "{vault}"')
    cfg = cfg.replace('email = "填你的QQ邮箱，如 12345678@qq.com"', 'email = "me@qq.com"')
    cfg = cfg.replace('auth_code = "填QQ邮箱授权码"',
                      f'auth_code = "-"\nsmtp_host = "127.0.0.1"\nsmtp_port = {SMTP_PORT}\nsmtp_ssl = false')
    cfg = cfg.replace('dir = "~/kb-backups"', f'dir = "{tmp / "backups"}"')
    cfg = cfg.replace("[weixin]", f'[weixin]\napi_base_url = "{BASE}"\ncdn_base_url = "{BASE}/c2c"')
    (tmp / "config.toml").write_text(cfg, "utf-8")
    return tmp, vault


async def main():
    global BASE
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ilink)
    BASE = f"http://127.0.0.1:{srv.server_port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp, vault = make_env()
    bot.BASE_DIR = monitor.BASE_DIR = tmp
    cfg = bot.Config(tmp / "config.toml")
    from notify import Notifier
    n = Notifier(cfg.alert)
    assert n.configured

    # ── 1. 邮件（含二维码图片）──
    assert n.send("测试主题", "正文第一行\n第二行", image_png=wx.qr_png("https://x"))
    m = MAILS[-1]
    assert m["Subject"] == "测试主题" and m["To"] == "me@qq.com"
    parts = [p.get_content_type() for p in m.walk()]
    assert "text/plain" in parts and "text/html" in parts and "image/png" in parts, parts
    print("✓ 邮件发送（纯文本 + 内嵌二维码图片）")

    # ── 2. 备份 ──
    p = monitor.do_backup(cfg)
    with tarfile.open(p) as t:
        names = t.getnames()
    assert any(x.endswith("昨天的笔记.md") for x in names) and "机器人配置/config.toml" in names
    assert oct(p.stat().st_mode)[-3:] == "600" and oct(cfg.backup_dir.stat().st_mode)[-3:] == "700"
    old = cfg.backup_dir / "知识库-20200101-0300.tar.gz"
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    time.sleep(1.1)
    monitor.do_backup(cfg)
    assert not old.exists(), "超过保留天数的备份应被删除"
    print("✓ 备份、校验、按天数清理")

    # ── 3. 巡检：机器人掉线 → 告警；恢复 → 恢复通知；不重复告警；日报只发一次 ──
    status = tmp / "status.json"
    status.write_text(json.dumps({"state": "error", "last_ok": time.time() - 3600, "detail": "网络不通"}))
    MAILS.clear()
    now = datetime.now().replace(hour=9, minute=0)
    monitor.run(cfg, n, now=now)
    subs = subjects()
    assert any("告警：机器人异常" in s for s in subs), subs
    report = [m for m in MAILS if "日报" in m["Subject"]]
    assert len(report) == 1 and "1 项异常" in report[0]["Subject"]
    body = report[0].get_body(("plain",)).get_content()
    assert "新增 2 篇" in body and "01-学习/AI：1 篇" in body and "共 3 篇" in body, body
    print("✓ 掉线告警 + 日报：", report[0]["Subject"])

    MAILS.clear()
    monitor.run(cfg, n, now=now)
    assert not MAILS, f"同一问题不应重复告警、日报不应重发：{subjects()}"
    status.write_text(json.dumps({"state": "ok", "last_ok": time.time()}))
    monitor.run(cfg, n, now=now)
    assert any("已恢复：机器人" in s for s in subjects()), subjects()
    print("✓ 不重复告警、恢复后发恢复通知")

    status.write_text(json.dumps({"state": "relogin", "last_ok": time.time() - 9999}))
    MAILS.clear()
    monitor.run(cfg, n, now=now)
    assert not MAILS, "重新登录期间由机器人自己发二维码，巡检不应重复告警"
    print("✓ 等待扫码期间巡检不重复告警")

    # ── 4. 会话失效 → 暂停 → 邮件发二维码 → 扫码 → 自动恢复 ──
    wx.PAUSE_SECONDS, wx.RELOGIN_RETRY, wx.QR_WAIT_SECONDS, wx.TICK = 0.3, [0.3], 20, 0.05
    (tmp / "weixin_token.json").write_text(json.dumps({"bot_token": OLD, "ilink_user_id": "me", "baseurl": BASE}))
    MAILS.clear()
    b = wx.WeixinBot(bot.Config(tmp / "config.toml"))
    task = asyncio.create_task(b.run_forever())
    for _ in range(300):
        await asyncio.sleep(0.05)
        if Ilink.sent:
            break
    task.cancel()
    subs = subjects()
    assert any("需要重新扫码" in s for s in subs), subs
    qr_mail = [m for m in MAILS if "需要重新扫码" in m["Subject"]][0]
    assert any(p.get_content_type() == "image/png" for p in qr_mail.walk())
    assert "liteapp.weixin.qq.com/q/abc" in qr_mail.get_body(("plain",)).get_content()
    assert any("已重新绑定" in s for s in subs), subs
    assert json.loads((tmp / "weixin_token.json").read_text())["bot_token"] == NEW
    assert Ilink.tokens_seen.count(OLD) >= 2 and NEW in Ilink.tokens_seen
    assert Ilink.sent and Ilink.sent[0]["context_token"] == "c1", "恢复后应能正常收发消息"
    assert list((vault / "01-学习/AI").glob("*恢复后的第一条*"))
    assert json.loads(status.read_text())["state"] == "ok"
    print("✓ 会话失效 → 暂停重试 → 邮件发二维码 → 扫码 → 自动恢复收发")

    # ── 5. 暂停期间手动 --login 更新了凭证 → 立刻换用 ──
    (tmp / "weixin_token.json").write_text(json.dumps({"bot_token": OLD, "ilink_user_id": "me", "baseurl": BASE}))
    wx.PAUSE_SECONDS = 30
    Ilink.sent.clear()
    Ilink.tokens_seen.clear()
    (tmp / "weixin_cursor.txt").write_text("0")
    b = wx.WeixinBot(bot.Config(tmp / "config.toml"))
    task = asyncio.create_task(b.run_forever())
    await asyncio.sleep(0.5)
    assert json.loads(status.read_text())["state"] == "paused"
    (tmp / "weixin_token.json").write_text(json.dumps({"bot_token": NEW, "ilink_user_id": "me", "baseurl": BASE}))
    t0 = time.time()
    for _ in range(100):
        await asyncio.sleep(0.05)
        if NEW in Ilink.tokens_seen and json.loads(status.read_text())["state"] == "ok":
            break
    task.cancel()
    assert NEW in Ilink.tokens_seen and json.loads(status.read_text())["state"] == "ok", \
        "手动更新凭证后应在暂停期间立即恢复"
    assert time.time() - t0 < 5, "不应等到暂停结束"
    print("✓ 暂停期间手动重新绑定，立即恢复（无需重启）")

    # ── 7. Tailscale 检查：stderr 警告不影响解析；命令失灵时以 HTTPS 实测为准 ──
    fake = tmp / "fake-tailscale"
    js = json.dumps({"BackendState": "Running", "Self": {"DNSName": "mini.example.ts.net."}})
    mode = tmp / "ts-mode"
    fake.write_text("#!/bin/sh\n"
                    f"if [ \"$(cat {mode})\" = ok ]; then echo 'Warning: client version != tailscaled version' >&2; "
                    f"echo '{js}'; exit 0; fi\n"
                    "echo 'failed to connect to local Tailscale service' >&2; exit 1\n")
    fake.chmod(0o755)
    saved = (monitor.TAILSCALE_CANDIDATES, monitor._couch_cred, monitor._http_ok, monitor.time.sleep)
    https_up = {"v": True}
    monitor.TAILSCALE_CANDIDATES = [str(fake)]
    monitor._couch_cred = lambda: ("u", "p")
    monitor._http_ok = lambda url, *a, **k: (https_up["v"], "" if https_up["v"] else "timed out")
    monitor.time.sleep = lambda s: None
    try:
        mode.write_text("ok")
        c = monitor.check_tailscale(fix=False)
        assert c.ok and c.detail == "在线", c
        assert (tmp / "tailscale_host.txt").read_text() == "mini.example.ts.net"
        mode.write_text("fail")
        c = monitor.check_tailscale(fix=False)
        assert c.ok and "HTTPS 访问数据库正常" in c.detail, c
        https_up["v"] = False
        c = monitor.check_tailscale(fix=False)
        assert not c.ok and "failed to connect" in c.detail, c
    finally:
        monitor.TAILSCALE_CANDIDATES, monitor._couch_cred, monitor._http_ok, monitor.time.sleep = saved
    print("✓ Tailscale：忽略 stderr 警告、命令失灵时重试并以 HTTPS 实测为准")

    smtp.stop()
    srv.shutdown()
    print("\n✅ 运维功能测试全部通过")


if __name__ == "__main__":
    asyncio.run(main())
