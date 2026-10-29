#!/usr/bin/env python3
"""
企业微信「智能机器人」长连接 → Obsidian 知识库

在企业微信里给机器人发文字 / 语音 / 图片 / 文件，自动写入知识库：
  - 默认追加到  <收件箱>/YYYY-MM-DD.md
  - 带指定标签（如 #工作）或前缀（如 “踩坑：”）的消息按 config.toml 的 routes 归档
  - 语音由企业微信自动转成文字后推送，无需本地语音识别
  - 图片 / 文件解密后存入附件目录，并在笔记里插入链接

协议参考：https://developer.work.weixin.qq.com/document/path/101463
"""
import argparse
import asyncio
import base64
import json
import logging
import re
import ssl
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib

import certifi
import websockets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_SECONDS = 30
HEARTBEAT_TIMEOUT = 90
BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger("kb-bot")


# ───────────────────────────── 配置 ─────────────────────────────

class Config:
    def __init__(self, path: Path):
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        wecom = raw.get("wecom", {})
        weixin = raw.get("weixin", {})
        vault = raw.get("vault", {})
        # 消息通道：weixin = 普通微信（ClawBot 官方接口），wecom = 企业微信智能机器人
        self.channel = str(raw.get("channel", "weixin")).strip().lower()
        self.weixin_token_file = BASE_DIR / "weixin_token.json"
        self.weixin_allowed = [str(u) for u in weixin.get("allowed_users", [])]
        self.weixin_api_base = weixin.get("api_base_url")   # 仅测试用，正常不填
        self.weixin_cdn_base = weixin.get("cdn_base_url")   # 仅测试用，正常不填
        self.bot_id = str(wecom.get("bot_id", "")).strip()
        self.secret = str(wecom.get("secret", "")).strip()
        self.ws_url = wecom.get("ws_url", DEFAULT_WS_URL)
        self.allowed_users = [str(u) for u in wecom.get("allowed_users", [])]
        # 企业微信是国内服务，默认直连、不走系统代理（代理软件开着也不受影响）
        self.use_system_proxy = bool(raw.get("use_system_proxy", wecom.get("use_system_proxy", False)))
        # 告警邮件、备份、巡检
        self.alert = raw.get("alert", {})
        backup = raw.get("backup", {})
        self.backup_dir = Path(str(backup.get("dir", "~/kb-backups"))).expanduser()
        self.backup_keep_days = int(backup.get("keep_days", 30))
        self.backup_hour = int(backup.get("hour", 3))
        monitor = raw.get("monitor", {})
        self.report_hour = int(monitor.get("daily_report_hour", 8))
        self.min_free_gb = float(monitor.get("min_free_gb", 10))
        # userid → 显示名，写进笔记的 author 字段（家人共用时区分是谁记的）
        self.names = {str(k): str(v) for k, v in raw.get("names", {}).items()}
        self.vault = Path(str(vault.get("path", "~/知识库"))).expanduser()
        self.inbox_dir = vault.get("inbox_dir", "00-收件箱")
        self.attach_dir = vault.get("attachments_dir", "00-收件箱/附件")
        self.routes = []
        for r in raw.get("routes", []):
            self.routes.append({
                "name": r.get("name") or r.get("tag") or r.get("dir"),
                "tags": [t for t in ([r["tag"]] if r.get("tag") else []) + r.get("tags", [])],
                "prefixes": r.get("prefixes", []),
                "dir": r["dir"],
            })

    def validate(self):
        errors = []
        if self.channel == "weixin":
            if not self.weixin_token_file.exists():
                errors.append("还没有绑定微信。请在终端运行：.venv/bin/python bot.py --login ，用微信扫码")
        elif self.channel == "wecom":
            if not self.bot_id or "填" in self.bot_id:
                errors.append("config.toml 里的 wecom.bot_id 还没填")
            if not self.secret or "填" in self.secret:
                errors.append("config.toml 里的 wecom.secret 还没填")
        else:
            errors.append(f"config.toml 里的 channel 只能是 weixin 或 wecom，现在是 {self.channel!r}")
        return errors


# ───────────────────────────── 状态（去重 / 撤销） ─────────────────────────────

class State:
    def __init__(self, path: Path):
        self.path = path
        self.seen = []          # 最近处理过的 msgid，防止重复回调
        self.last_write = None  # {"file":..., "text":...} 用于 /撤销
        if path.exists():
            try:
                d = json.loads(path.read_text("utf-8"))
                self.seen = d.get("seen", [])
                self.last_write = d.get("last_write")
            except Exception:
                log.warning("state.json 读取失败，已忽略")

    def save(self):
        self.seen = self.seen[-500:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"seen": self.seen, "last_write": self.last_write},
                                  ensure_ascii=False), "utf-8")
        tmp.replace(self.path)


# ───────────────────────────── 知识库写入 ─────────────────────────────

class Vault:
    def __init__(self, cfg: Config, state: State):
        self.cfg = cfg
        self.state = state
        self.lock = asyncio.Lock()

    @staticmethod
    def _slug(text: str, n=20) -> str:
        t = re.sub(r"!\[\[.*?\]\]|\[\[.*?\]\]|#\S+|[\\/:*?\"<>|#^\[\]\n\r]", " ", text)
        t = re.sub(r"\s+", " ", t).strip()
        return t[:n].strip() or "记录"

    def route(self, text: str):
        """返回 (目录, 路由名, 处理后的文本)。第一条命中的规则生效。
        手机输入法常打出全角符号，这里统一转成半角：＃→#，开头的“工作:”与“工作：”同等对待；
        标签不区分大小写（#ai 等同 #AI），并把命中的标签统一写成配置里的写法，方便 Obsidian 按标签检索。"""
        text = text.replace("＃", "#")
        for r in self.cfg.routes:
            for p in r["prefixes"]:
                for variant in {p, p.replace("：", ":"), p.replace(":", "：")}:
                    if text.startswith(variant):
                        body = text[len(variant):].strip()
                        tag = r["tags"][0] if r["tags"] else ""
                        if tag and not re.search(re.escape(tag) + r"(?![A-Za-z0-9_])", body, re.I):
                            body = f"{body} {tag}".strip()
                        return r["dir"], r["name"], body
            for t in r["tags"]:
                # 标签后面紧跟英文字母/数字时不算（#AIx 不是 #AI）；紧跟中文时算，并补一个空格，
                # 让 Obsidian 把它识别成 #AI 而不是 #AI测试
                pat = re.compile(re.escape(t) + r"(?![A-Za-z0-9_])", re.I)
                if pat.search(text):
                    def fix(m, t=t):
                        nxt = m.string[m.end():m.end() + 1]
                        return t + (" " if nxt and not nxt.isspace() and nxt not in "，。！？,.!?;；" else "")
                    return r["dir"], r["name"], pat.sub(fix, text)
        return self.cfg.inbox_dir, "收件箱", text

    async def create_note(self, rel_dir: str, body: str, now: datetime, meta: dict,
                          slug: str = None) -> Path:
        """每条消息单独存成一个新笔记。机器人只创建、从不修改已有文件，
        所以你在任何设备上编辑、移动、合并这些笔记都不会和机器人冲突。"""
        async with self.lock:
            d = self.cfg.vault / rel_dir
            d.mkdir(parents=True, exist_ok=True)
            base = f"{now:%Y-%m-%d %H%M} {self._slug(slug or body)}"
            f = d / f"{base}.md"
            i = 2
            while f.exists():
                f = d / f"{base} ({i}).md"
                i += 1
            fm = ["---", f"created: {now:%Y-%m-%dT%H:%M:%S}"]
            fm += [f"{k}: {v}" for k, v in meta.items() if v]
            fm.append("---")
            content = "\n".join(fm) + "\n\n" + body.strip() + "\n"
            # 先写临时文件再改名，避免同步工具读到写了一半的文件
            tmp = d / f".{f.name}.tmp"
            tmp.write_text(content, "utf-8")
            tmp.replace(f)
            self.state.last_write = {"file": str(f), "text": content}
            self.state.save()
            return f

    async def undo(self) -> str:
        async with self.lock:
            lw = self.state.last_write
            if not lw:
                return "没有可撤销的记录。"
            f = Path(lw["file"])
            self.state.last_write = None
            self.state.save()
            if not f.exists():
                return "上一条记录已经被移动或删除了，未做改动。"
            if f.read_text("utf-8") != lw["text"]:
                return "上一条记录已经被编辑过，为了不丢你的改动，没有删除。"
            f.unlink()
            return f"↩️ 已撤销：{f.stem[16:] or f.stem}"

    def today_summary(self, now: datetime, limit=3000) -> str:
        prefix = f"{now:%Y-%m-%d} "
        dirs = [self.cfg.inbox_dir] + [r["dir"] for r in self.cfg.routes]
        parts = []
        for d in dict.fromkeys(dirs):
            p = self.cfg.vault / d
            if not p.is_dir():
                continue
            files = sorted((x for x in p.glob("*.md") if x.name.startswith(prefix)),
                           key=lambda x: x.stat().st_mtime)
            if not files:
                continue
            lines = []
            for x in files:
                body = x.read_text("utf-8")
                body = re.sub(r"^---\n.*?\n---\n", "", body, flags=re.S).strip()
                hhmm = x.name[11:13] + ":" + x.name[13:15]
                lines.append(f"- {hhmm} " + body.replace("\n", " ")[:120])
            parts.append(f"**{d}**\n" + "\n".join(lines))
        if not parts:
            return "今天还没有记录。"
        text = "\n\n".join(parts)
        return text if len(text) <= limit else text[:limit] + "\n…（内容较多，已截断）"

    def save_attachment(self, data: bytes, filename: str, now: datetime) -> str:
        d = self.cfg.vault / self.cfg.attach_dir
        d.mkdir(parents=True, exist_ok=True)
        filename = re.sub(r'[\\/:*?"<>|\n\r]', "_", filename).strip() or "附件"
        p = d / filename
        stem, suf = p.stem, p.suffix
        i = 1
        while p.exists():
            p = d / f"{stem}-{i}{suf}"
            i += 1
        p.write_bytes(data)
        return str(p.relative_to(self.cfg.vault))


# ───────────────────────────── 媒体下载与解密 ─────────────────────────────

def _b64key(aeskey: str) -> bytes:
    s = aeskey.strip()
    try:
        key = base64.b64decode(s + "=" * (-len(s) % 4))
        if len(key) == 32:
            return key
    except Exception:
        pass
    if len(s) == 32:
        return s.encode()
    raise ValueError("无法解析 aeskey")


def decrypt_media(data: bytes, aeskey: str) -> bytes:
    """AES-256-CBC，IV 取 key 前 16 字节，PKCS#7 填充到 32 字节倍数。"""
    key = _b64key(aeskey)
    dec = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = dec.update(data) + dec.finalize()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 32 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    return plain


def guess_ext(data: bytes) -> str:
    sigs = [(b"\xff\xd8\xff", ".jpg"), (b"\x89PNG", ".png"), (b"GIF8", ".gif"),
            (b"%PDF", ".pdf"), (b"RIFF", ".webp"), (b"PK\x03\x04", ".zip"),
            (b"#!AMR", ".amr")]
    for sig, ext in sigs:
        if data.startswith(sig):
            return ext
    if data[4:8] == b"ftyp":
        return ".mp4"
    return ".bin"


USE_SYSTEM_PROXY = False  # 启动时由配置覆盖

# 使用 certifi 自带的标准根证书列表验证服务器证书。
# macOS 上部分 Python（如 python.org 安装包）不带系统根证书，会报 CERTIFICATE_VERIFY_FAILED；
# 这里只是指定根证书来源，证书验证仍然完整开启。
SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def download(url: str, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "wechat-obsidian-inbox/1.0"})
    # 默认绕过系统代理直连；ProxyHandler({}) 表示不使用任何代理
    https = urllib.request.HTTPSHandler(context=SSL_CTX)
    opener = urllib.request.build_opener(https) if USE_SYSTEM_PROXY \
        else urllib.request.build_opener(urllib.request.ProxyHandler({}), https)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get_filename()


async def fetch_media(item: dict, kind: str):
    """返回 (明文 bytes, 建议文件名)。"""
    url, aeskey = item.get("url"), item.get("aeskey")
    if not url:
        raise ValueError("消息里没有下载地址")
    raw, name = await asyncio.to_thread(download, url)
    data = decrypt_media(raw, aeskey) if aeskey else raw
    return data, name


# ───────────────────────────── 机器人主体 ─────────────────────────────

HELP = """📒 知识库机器人 使用说明

【随手记】
直接发文字或语音 → 存进「收件箱」，每条单独一篇笔记
语音会自动转成文字，回复里附上识别结果，方便核对

【自动归档】在消息任意位置加标签：
{routes}
例：今天学了注意力机制 #AI
例：踩坑：pip 安装要加参数

【图片 / 文件】
直接发照片、截图、PDF 等 → 存进附件，并记一篇笔记

【引用补充】
长按之前的消息 → 引用 → 写下想法，原文会一起保存

【查看和修改】
/目录  浏览知识库，回复数字打开
/搜 关键词  /最近
打开笔记后可以追加、改写、替换、移动、删除
发「/帮助 浏览」看详细用法

【其他指令】
/今天  看今天记了什么
/撤销  删掉刚存的那条（已改过的不会删）
/帮助  显示本说明

小技巧：一条消息只记一件事，以后整理更方便"""


class Bot:
    def __init__(self, cfg: Config):
        global USE_SYSTEM_PROXY
        USE_SYSTEM_PROXY = cfg.use_system_proxy
        self.cfg = cfg
        self.state = State(BASE_DIR / "state.json")
        self.vault = Vault(cfg, self.state)
        from browse import Browser
        self.browser = Browser(cfg.vault)
        self.ws = None
        self.last_pong = time.monotonic()

    # ---------- 发送 ----------
    async def send(self, payload: dict):
        if self.ws is None:
            return
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def reply(self, req_id: str, content: str):
        await self.send({
            "cmd": "aibot_respond_msg",
            "headers": {"req_id": req_id},
            "body": {"msgtype": "stream",
                     "stream": {"id": uuid.uuid4().hex, "finish": True, "content": content}},
        })

    def help_text(self):
        lines = []
        for r in self.cfg.routes:
            keys = r["tags"][:1] + r["prefixes"][:1]
            if keys:
                lines.append(f"{' 或 '.join(keys)} → {r['dir']}")
        return HELP.format(routes="\n".join(lines) if lines else "（还没有配置归档规则）")

    # ---------- 连接 ----------
    async def run_forever(self):
        backoff = 1
        while True:
            try:
                await self.run_once()
                log.warning("服务器关闭了连接，准备重连")
                backoff = 1
            except AuthError as e:
                log.error("订阅失败：%s。请检查 Bot ID / Secret，5 分钟后重试", e)
                await asyncio.sleep(300)
                continue
            except KickedError:
                log.warning("连接被新的连接顶掉（是不是另一台机器也在运行本程序？），60 秒后重连")
                await asyncio.sleep(60)
                continue
            except ssl.SSLCertVerificationError as e:
                log.warning("证书验证失败：%s。如果仍出现，可能是网络里有软件在拦截 HTTPS"
                            "（例如开启了 MITM 的代理），%d 秒后重连", e, backoff)
            except (OSError, websockets.exceptions.WebSocketException, asyncio.TimeoutError) as e:
                log.warning("连接断开：%r，%d 秒后重连", e, backoff)
            except Exception:
                log.exception("未预期的错误，%d 秒后重连", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def run_once(self):
        log.info("正在连接 %s", self.cfg.ws_url)
        # proxy=None：不读取系统代理，直接连接企业微信
        proxy = True if self.cfg.use_system_proxy else None
        extra = {"ssl": SSL_CTX} if self.cfg.ws_url.startswith("wss://") else {}
        async with websockets.connect(self.cfg.ws_url, ping_interval=None, proxy=proxy,
                                      max_size=None, open_timeout=20, **extra) as ws:
            self.ws = ws
            req_id = uuid.uuid4().hex
            await self.send({"cmd": "aibot_subscribe", "headers": {"req_id": req_id},
                             "body": {"bot_id": self.cfg.bot_id, "secret": self.cfg.secret}})
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            if resp.get("errcode", 0) != 0:
                raise AuthError(f"{resp.get('errcode')} {resp.get('errmsg')}")
            log.info("✅ 已连接企业微信，开始接收消息")
            self.last_pong = time.monotonic()
            hb = asyncio.create_task(self.heartbeat())
            try:
                async for raw in ws:
                    await self.dispatch(raw)
            finally:
                hb.cancel()
                self.ws = None

    async def heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if time.monotonic() - self.last_pong > HEARTBEAT_TIMEOUT:
                log.warning("心跳超时，主动断开重连")
                await self.ws.close()
                return
            await self.send({"cmd": "ping", "headers": {"req_id": uuid.uuid4().hex}})

    async def dispatch(self, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            log.warning("收到无法解析的数据：%r", raw[:200])
            return
        cmd = msg.get("cmd")
        if cmd is None:  # 对我们请求的响应（含心跳）
            self.last_pong = time.monotonic()
            if msg.get("errcode", 0) != 0:
                log.warning("请求返回错误：%s", msg)
            return
        if cmd == "aibot_msg_callback":
            asyncio.create_task(self.safe(self.on_message(msg)))
        elif cmd == "aibot_event_callback":
            await self.on_event(msg)
        else:
            log.debug("忽略命令 %s", cmd)

    async def safe(self, coro):
        try:
            await coro
        except Exception:
            log.exception("处理消息出错")

    async def on_event(self, msg):
        body = msg.get("body", {})
        etype = body.get("event", {}).get("eventtype")
        if etype == "disconnected_event":
            raise KickedError()
        if etype == "enter_chat":
            await self.send({"cmd": "aibot_respond_welcome_msg",
                             "headers": {"req_id": msg["headers"]["req_id"]},
                             "body": {"msgtype": "text", "text": {"content":
                                      "你好！直接发文字或语音给我，就会存进知识库。发 /帮助 查看用法。"}}})

    # ---------- 消息处理 ----------
    async def on_message(self, msg):
        req_id = msg["headers"]["req_id"]
        body = msg.get("body", {})
        msgid = body.get("msgid")
        if msgid and msgid in self.state.seen:
            return
        if msgid:
            self.state.seen.append(msgid)
        userid = body.get("from", {}).get("userid", "")
        if self.cfg.allowed_users and userid not in self.cfg.allowed_users:
            log.info("拒绝未授权用户 %s", userid)
            await self.reply(req_id, "抱歉，你没有使用这个知识库机器人的权限。")
            return

        now = datetime.now()
        mtype = body.get("msgtype")
        try:
            result = await self.handle(mtype, body, userid, now)
        except Exception as e:
            log.exception("保存失败")
            result = f"⚠️ 保存失败：{e}"
        self.state.save()
        if result:
            await self.reply(req_id, result)

    async def handle(self, mtype, body, userid, now):
        quote = self.quote_text(body.get("quote"))
        meta = {"source": "企业微信", "type": mtype, "author": self.cfg.names.get(userid, userid)}

        async def save(rel, content, slug=None):
            slug = slug or content  # 文件名取自正文，不含引用
            if quote:
                content += f"\n\n> 引用：{quote}"
            return await self.vault.create_note(rel, content, now, meta, slug=slug)

        if mtype in ("text", "voice"):
            text = body.get(mtype, {}).get("content", "")
            text = re.sub(r"^@\S+\s*", "", text.strip()).strip()  # 群聊里去掉 @机器人
            if not text:
                return "（空消息，未保存）"
            if mtype == "text":
                cmd = self.command(text, userid, now)
                if cmd is not None:
                    return await cmd if asyncio.iscoroutine(cmd) else cmd
            rel, name, text = self.vault.route(text)
            f = await save(rel, text)
            extra = f"\n> {text}" if mtype == "voice" else ""
            return f"✓ 已存入 {name}{extra}"

        if mtype == "image":
            data, _ = await fetch_media(body.get("image", {}), "image")
            path = self.vault.save_attachment(data, f"{now:%Y%m%d-%H%M%S}{guess_ext(data)}", now)
            await save(self.cfg.inbox_dir, f"![[{path}]]", slug="图片")
            return "🖼 图片已存入 收件箱"

        if mtype in ("file", "video"):
            data, fname = await fetch_media(body.get(mtype, {}), mtype)
            filename = fname or f"{now:%Y%m%d-%H%M%S}{guess_ext(data)}"
            path = self.vault.save_attachment(data, filename, now)
            label = Path(path).name
            await save(self.cfg.inbox_dir, f"📎 [[{path}|{label}]]", slug=Path(label).stem)
            return f"📎 {label} 已存入 收件箱"

        if mtype == "mixed":
            texts, embeds = [], []
            for item in body.get("mixed", {}).get("msg_item", []):
                if item.get("msgtype") == "text":
                    texts.append(item.get("text", {}).get("content", ""))
                elif item.get("msgtype") == "image":
                    try:
                        data, _ = await fetch_media(item.get("image", {}), "image")
                        p = self.vault.save_attachment(
                            data, f"{now:%Y%m%d-%H%M%S}{guess_ext(data)}", now)
                        embeds.append(f"![[{p}]]")
                    except Exception as e:
                        embeds.append(f"（图片保存失败：{e}）")
            text = re.sub(r"^@\S+\s*", "", " ".join(texts).strip()).strip()
            rel, name, text = self.vault.route(text) if text else (self.cfg.inbox_dir, "收件箱", "")
            content = "\n".join([text] + embeds).strip()
            await save(rel, content, slug=None if text else "图文")
            return f"✓ 图文已存入 {name}"

        return f"暂不支持这种消息类型（{mtype}）"

    def command(self, text, userid, now, voice=False):
        # 先交给浏览/编辑模块：处理 /目录、回复数字、等待中的追加内容等
        reply = self.browser.handle(userid, text, voice=voice)
        if reply is not None:
            return reply
        if voice:
            return None
        t = text.replace("／", "/").strip()
        if not t.startswith("/"):
            return None
        c = t[1:].strip()
        if c in ("帮助", "help", "h", "?"):
            return self.help_text()
        if c in ("今天", "today"):
            return self.vault.today_summary(now)
        if c in ("撤销", "undo"):
            return self.vault.undo()
        if c in ("我是谁", "whoami"):
            return f"你的 userid：`{userid}`"
        return None  # 不认识的 “/xxx” 当作普通笔记保存

    @staticmethod
    def quote_text(q):
        if not q:
            return ""
        t = q.get("msgtype")
        if t in ("text", "voice"):
            return q.get(t, {}).get("content", "").replace("\n", " ")[:200]
        if t == "mixed":
            return " ".join(i.get("text", {}).get("content", "")
                            for i in q.get("mixed", {}).get("msg_item", [])
                            if i.get("msgtype") == "text")[:200]
        return f"[{t}]"


class AuthError(Exception):
    pass


class KickedError(Exception):
    pass


# ───────────────────────────── 入口 ─────────────────────────────

def check(cfg: Config) -> int:
    ok = True
    print(f"  消息通道：{'普通微信（ClawBot）' if cfg.channel == 'weixin' else '企业微信' if cfg.channel == 'wecom' else cfg.channel}")
    for e in cfg.validate():
        print("✗", e)
        ok = False
    try:
        cfg.vault.mkdir(parents=True, exist_ok=True)
        probe = cfg.vault / ".kb-bot-write-test"
        probe.write_text("ok", "utf-8")
        probe.unlink()
        print(f"✓ 知识库可写：{cfg.vault}")
    except Exception as e:
        print(f"✗ 知识库无法写入：{cfg.vault}（{e}）")
        print("  提示：后台程序访问“文稿/桌面/下载/iCloud”会被 macOS 拦截，建议把知识库放在 ~/知识库")
        ok = False
    print(f"  收件箱：{cfg.inbox_dir}    附件：{cfg.attach_dir}")
    for r in cfg.routes:
        print(f"  路由：{' '.join(r['tags'] + r['prefixes'])} → {r['dir']}")
    return 0 if ok else 1


def merge_config(cfg_path: Path, example: Path) -> int:
    """把模板里新增的配置段（如 [alert]、[backup]）补到 config.toml 末尾，已有内容不动。"""
    if not cfg_path.exists() or not example.exists():
        return 0
    have = {m.group(1) for m in re.finditer(r"(?m)^\[([A-Za-z_]+)\]\s*$", cfg_path.read_text("utf-8"))}
    blocks, cur, name = [], [], None
    for line in example.read_text("utf-8").splitlines(keepends=True):
        m = re.match(r"^\[\[?([A-Za-z_]+)\]?\]\s*$", line)
        if m:
            if name:
                blocks.append((name, cur))
            name, cur = (m.group(1) if not line.startswith("[[") else None), [line]
        elif name is not None or cur:
            cur.append(line)
    if name:
        blocks.append((name, cur))
    add = [(n, b) for n, b in blocks if n and n not in have and n in ("alert", "backup", "monitor", "weixin")]
    if not add:
        return 0
    # 每段只保留到下一个注释块之前的有效内容（去掉属于下一段的说明注释）
    text = "\n# ── 以下由安装脚本从模板补充 ──\n"
    for n, b in add:
        while b and b[-1].lstrip().startswith("#") or (b and not b[-1].strip()):
            b = b[:-1]
        text += "\n" + "".join(b).rstrip("\n") + "\n"
    with open(cfg_path, "a", encoding="utf-8") as f:
        f.write(text)
    print("已在 config.toml 末尾补充新的配置段：" + "、".join(f"[{n}]" for n, _ in add))
    return 0


def main():
    ap = argparse.ArgumentParser(description="微信 / 企业微信机器人 → Obsidian 知识库")
    ap.add_argument("-c", "--config", default=str(BASE_DIR / "config.toml"))
    ap.add_argument("--check", action="store_true", help="只检查配置和知识库路径")
    ap.add_argument("--login", action="store_true", help="用微信扫码绑定（普通微信通道）")
    ap.add_argument("--needs-login", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--merge-config", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    # 让 weixin_channel 里的 `import bot` 拿到当前这个模块，而不是再加载一份
    sys.modules.setdefault("bot", sys.modules[__name__])

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stdout)
    cfg_path = Path(args.config)
    if args.merge_config:
        sys.exit(merge_config(cfg_path, BASE_DIR / "config.example.toml"))
    if not cfg_path.exists():
        print(f"找不到配置文件 {cfg_path}，请先复制 config.example.toml 为 config.toml 并填写")
        sys.exit(1)
    cfg = Config(cfg_path)
    if args.needs_login:  # 供 install.sh 判断：需要扫码时退出码为 10
        sys.exit(10 if cfg.channel == "weixin" and not cfg.weixin_token_file.exists() else 0)
    if args.login:
        import weixin_channel
        sys.exit(weixin_channel.login(cfg))
    if args.check:
        sys.exit(check(cfg))
    errs = cfg.validate()
    if errs:
        for e in errs:
            log.error(e)
        sys.exit(1)
    cfg.vault.mkdir(parents=True, exist_ok=True)
    try:
        if cfg.channel == "weixin":
            import weixin_channel
            asyncio.run(weixin_channel.WeixinBot(cfg).run_forever())
        else:
            asyncio.run(Bot(cfg).run_forever())
    except KeyboardInterrupt:
        log.info("已退出")


if __name__ == "__main__":
    main()
