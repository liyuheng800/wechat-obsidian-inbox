"""
普通微信通道：微信 ClawBot 插件的官方个人号 Bot 接口（iLink）。

- 用你的微信扫码绑定一次（bot.py --login），之后在微信的“微信 ClawBot”对话里发消息即可
- Mac mini 通过 HTTPS 长轮询主动拉取消息，不需要公网 IP、域名或备案
- 语音由微信自动转成文字；图片 / 文件 / 视频从微信 CDN 下载并解密（AES-128-ECB）

协议字段参照腾讯官方插件 @tencent-weixin/openclaw-weixin 的源码（src/api/types.ts 等）。
"""
import asyncio
import base64
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import bot as core

log = logging.getLogger("kb-bot")

DEFAULT_API_BASE = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
BOT_TYPE = "3"
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "KbInboxBot/1.0 (personal knowledge base)"
STALE_TOKEN_ERRCODE = -14

# 消息条目类型（proto: MessageItemType）
TEXT, IMAGE, VOICE, FILE, VIDEO = 1, 2, 3, 4, 5
MSG_FROM_USER = 1
MSG_FROM_BOT = 2
STATE_FINISH = 2


# ───────────────────────────── HTTP ─────────────────────────────

def _client_version(v: str) -> int:
    parts = [int(p) for p in (v.split(".") + ["0", "0"])[:3]]
    return ((parts[0] & 0xFF) << 16) | ((parts[1] & 0xFF) << 8) | (parts[2] & 0xFF)


def _opener():
    https = urllib.request.HTTPSHandler(context=core.SSL_CTX)
    if core.USE_SYSTEM_PROXY:
        return urllib.request.build_opener(https)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), https)


def api(method: str, base: str, endpoint: str, body: dict = None, token: str = None,
        timeout: float = 15) -> dict:
    url = base.rstrip("/") + "/" + endpoint
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(_client_version(CHANNEL_VERSION)),
    }
    data = None
    if method == "POST":
        headers.update({
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            # 防重放：随机 uint32 → 十进制字符串 → base64
            "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
        })
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = dict(body or {})
        payload["base_info"] = {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with _opener().open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _is_timeout(e: Exception) -> bool:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(e.reason, (socket.timeout, TimeoutError))


# ───────────────────────────── 媒体 ─────────────────────────────

def _parse_key(item_type: int, item: dict) -> bytes:
    """图片优先用 image_item.aeskey（16 字节的 hex）；其余用 media.aes_key（base64，
    解出来要么是 16 字节原始 key，要么是 32 位 hex 字符串）。"""
    if item_type == IMAGE and item.get("aeskey"):
        return bytes.fromhex(item["aeskey"])
    raw = (item.get("media") or {}).get("aes_key")
    if not raw:
        raise ValueError("消息里没有解密密钥")
    k = base64.b64decode(raw + "=" * (-len(raw) % 4))
    if len(k) == 16:
        return k
    if len(k) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", k):
        return bytes.fromhex(k.decode())
    raise ValueError(f"无法识别的密钥格式（{len(k)} 字节）")


def _decrypt_ecb(data: bytes, key: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = dec.update(data) + dec.finalize()
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
        plain = plain[:-pad]
    return plain


def download_media(item_type: int, item: dict, cdn_base: str) -> bytes:
    media = item.get("media") or {}
    url = media.get("full_url")
    if not url:
        eqp = media.get("encrypt_query_param")
        if not eqp:
            raise ValueError("消息里没有下载地址")
        url = f"{cdn_base}/download?encrypted_query_param={urllib.parse.quote(eqp, safe='')}"
    key = _parse_key(item_type, item)
    req = urllib.request.Request(url, headers={"User-Agent": "kb-inbox-bot/1.0"})
    with _opener().open(req, timeout=60) as resp:
        return _decrypt_ecb(resp.read(), key)


# ───────────────────────────── 扫码登录 ─────────────────────────────

def _show_qr(content: str):
    print("\n请用【手机微信】扫描下面的二维码，并在手机上确认：\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        # 同时生成一张图片并自动打开，终端里的二维码扫不出来时用它
        import qrcode.image.svg
        svg = core.BASE_DIR / "weixin-login-qr.svg"
        qrcode.make(content, image_factory=qrcode.image.svg.SvgPathFillImage).save(str(svg))
        if sys.platform == "darwin":
            subprocess.run(["open", str(svg)], check=False)
            print(f"\n（已同时用浏览器/预览打开二维码图片：{svg}）")
    except Exception as e:  # 没装 qrcode 库也能用
        log.debug("生成二维码失败：%s", e)
    print(f"\n如果二维码显示不出来，把这个链接复制到微信里打开也可以：\n{content}\n")


def request_qr(base: str, old_tokens: list) -> dict:
    r = api("POST", base, f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}",
            {"local_token_list": old_tokens})
    if not r.get("qrcode") or not r.get("qrcode_img_content"):
        raise RuntimeError(f"获取二维码失败：{r}")
    return r


def poll_qr(base: str, key: str, verify: str = None) -> dict:
    ep = f"ilink/bot/get_qrcode_status?qrcode={urllib.parse.quote(key, safe='')}"
    if verify:
        ep += f"&verify_code={urllib.parse.quote(verify, safe='')}"
    try:
        return api("GET", base, ep, timeout=40)
    except Exception as e:
        if not _is_timeout(e):
            log.debug("查询扫码状态出错，继续等待：%s", e)
        return {"status": "wait"}


def save_token(token_file: Path, st: dict, base: str):
    tmp = token_file.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "bot_token": st["bot_token"],
        "baseurl": st.get("baseurl") or base,
        "ilink_bot_id": st["ilink_bot_id"],
        "ilink_user_id": st.get("ilink_user_id", ""),
        "bound_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(token_file)


def qr_png(content: str):
    """生成二维码 PNG（用于邮件）；缺少依赖时返回 None。"""
    try:
        import io
        import qrcode
        from qrcode.image.pure import PyPNGImage
        buf = io.BytesIO()
        qrcode.make(content, image_factory=PyPNGImage, box_size=8, border=3).save(buf)
        return buf.getvalue()
    except Exception as e:
        log.debug("生成二维码图片失败：%s", e)
        return None


def _old_tokens(token_file: Path):
    try:
        return [json.loads(token_file.read_text("utf-8"))["bot_token"]]
    except Exception:
        return []


def login(cfg) -> int:
    """终端里交互式扫码绑定（bot.py --login）。"""
    core.USE_SYSTEM_PROXY = cfg.use_system_proxy
    base = cfg.weixin_api_base or DEFAULT_API_BASE
    token_file = cfg.weixin_token_file
    old_tokens = _old_tokens(token_file)

    def new_qr():
        r = request_qr(base, old_tokens)
        _show_qr(r["qrcode_img_content"])
        return r["qrcode"]

    try:
        qrcode_key = new_qr()
    except Exception as e:
        print(f"❌ 连接微信服务器失败：{e}")
        return 1

    poll_base, verify, refresh, scanned = base, None, 1, False
    deadline = time.time() + 480
    while time.time() < deadline:
        st = poll_qr(poll_base, qrcode_key, verify)
        status = st.get("status")

        if status == "scaned":
            verify = None
            if not scanned:
                print("已扫码，请在手机上确认…")
                scanned = True
        elif status == "need_verifycode":
            verify = input("请输入手机微信上显示的数字，然后回车：").strip()
            continue
        elif status == "verify_code_blocked":
            print("⛔ 数字输错次数太多，请稍后重新运行 --login")
            return 1
        elif status == "scaned_but_redirect" and st.get("redirect_host"):
            poll_base = f"https://{st['redirect_host']}"
        elif status == "expired":
            refresh += 1
            if refresh > 3:
                print("二维码多次过期，请重新运行 --login")
                return 1
            print("二维码已过期，正在刷新…")
            qrcode_key, scanned = new_qr(), False
        elif status == "binded_redirect":
            if token_file.exists():
                print("✅ 这个微信已经绑定过，原来的凭证仍然有效，无需重复绑定。")
                return 0
            print("这个微信之前已绑定过另一处。请在微信“我 → 设置 → 插件 → 微信 ClawBot”里解除旧绑定后重试。")
            return 1
        elif status == "confirmed":
            if not st.get("bot_token") or not st.get("ilink_bot_id"):
                print(f"❌ 登录确认了，但服务器没有返回凭证：{st}")
                return 1
            save_token(token_file, st, poll_base)
            print(f"\n✅ 已绑定微信。凭证保存在 {token_file}（请勿外传）")
            print("如果机器人正在后台运行，它会在半分钟内自动换用新凭证，不需要重启。")
            return 0
        time.sleep(1)

    print("等待扫码超时，请重新运行 --login")
    return 1


# ───────────────────────────── 机器人 ─────────────────────────────

def _plain(text: str) -> str:
    """微信聊天不渲染 Markdown，去掉粗体、行内代码标记。"""
    return text.replace("**", "").replace("`", "")


# 会话失效（-14）时的处理节奏。官方插件的做法是暂停 60 分钟再试；
# 连续两次失效才认定需要重新扫码，然后把二维码发到邮箱。
PAUSE_SECONDS = 3600
RELOGIN_RETRY = [1800, 3600, 7200, 14400, 21600]   # 每次发二维码后，没扫码则隔多久再发一次（最后一档重复）
QR_WAIT_SECONDS = 480                               # 每个二维码最多等多久
TICK = 30                                           # 等待期间多久检查一次凭证文件是否被手动更新


class WeixinBot(core.Bot):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.cdn = cfg.weixin_cdn_base or DEFAULT_CDN_BASE
        self.cursor_file = core.BASE_DIR / "weixin_cursor.txt"
        self.cursor = self.cursor_file.read_text("utf-8").strip() if self.cursor_file.exists() else ""
        self.poll_timeout = 35.0
        self.status_file = core.BASE_DIR / "status.json"
        self.status = {"state": "starting", "last_ok": 0, "detail": ""}
        self._status_written = 0
        from notify import Notifier
        self.notifier = Notifier(cfg.alert, core.SSL_CTX)
        self._token_mtime = None
        self._load_token()

    # ---------- 凭证与状态 ----------
    def _load_token(self):
        f = self.cfg.weixin_token_file
        tok = json.loads(f.read_text("utf-8"))
        self.token = tok["bot_token"]
        self.base = self.cfg.weixin_api_base or tok.get("baseurl") or DEFAULT_API_BASE
        self.owner = tok.get("ilink_user_id", "")
        self._token_mtime = f.stat().st_mtime_ns

    def _token_changed(self) -> bool:
        """凭证文件被更新（例如在终端手动运行了 --login）时，自动换用新凭证。"""
        try:
            m = self.cfg.weixin_token_file.stat().st_mtime_ns
        except FileNotFoundError:
            return False
        if m != self._token_mtime:
            self._load_token()
            log.info("检测到新的微信凭证，已自动换用")
            return True
        return False

    def set_status(self, state, detail="", ok=False, force=False):
        self.status["state"] = state
        self.status["detail"] = detail
        if ok:
            self.status["last_ok"] = time.time()
        now = time.time()
        if force or not ok or now - self._status_written > 60:   # 正常情况下每分钟最多写一次
            self.status["updated"] = now
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.status, ensure_ascii=False), "utf-8")
            tmp.replace(self.status_file)
            self._status_written = now

    async def _wait(self, seconds) -> bool:
        """等待一段时间；期间凭证被手动更新则提前返回 True。"""
        end = time.time() + seconds
        while time.time() < end:
            await asyncio.sleep(min(TICK, max(0.05, end - time.time())))
            if self._token_changed():
                return True
        return False

    async def call(self, endpoint, body, timeout=15):
        return await asyncio.to_thread(api, "POST", self.base, endpoint, body, self.token, timeout)

    # ---------- 收消息 ----------
    async def run_forever(self):
        log.info("正在连接微信（ClawBot）%s", self.base)
        try:
            await self.call("ilink/bot/msg/notifystart", {}, timeout=10)
        except Exception as e:
            log.debug("notifystart 失败（可忽略）：%s", e)
        connected, failures, stale = False, 0, 0
        while True:
            self._token_changed()
            try:
                resp = await self.call("ilink/bot/getupdates", {"get_updates_buf": self.cursor},
                                       timeout=self.poll_timeout + 5)
            except Exception as e:
                if _is_timeout(e):
                    self.set_status("ok", ok=True)
                    continue  # 长轮询超时属于正常情况
                failures += 1
                wait = min(2 ** failures, 60)
                if isinstance(e, core.ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(e):
                    log.warning("证书验证失败：%s。可能有代理软件在拦截 HTTPS，%d 秒后重试", e, wait)
                else:
                    log.warning("连接微信失败：%r，%d 秒后重试", e, wait)
                self.set_status("error", f"连接微信失败：{e!r}"[:300])
                connected = False
                await asyncio.sleep(wait)
                continue

            code = resp.get("errcode") or resp.get("ret") or 0
            if code == STALE_TOKEN_ERRCODE:
                connected = False
                stale += 1
                if stale == 1:
                    log.warning("微信会话失效（errcode -14），按官方插件的做法暂停 %d 分钟后重试", PAUSE_SECONDS // 60)
                    self.set_status("paused", "微信会话失效，暂停后自动重试")
                    await self._wait(PAUSE_SECONDS)
                else:
                    log.error("微信会话持续失效，需要重新扫码，正在把二维码发到邮箱")
                    await self.relogin()
                    stale = 0
                continue
            if code != 0:
                failures += 1
                wait = min(2 ** failures, 60)
                log.warning("拉取消息出错：%s，%d 秒后重试", resp, wait)
                self.set_status("error", f"拉取消息出错：{resp}"[:300])
                await asyncio.sleep(wait)
                continue

            failures, stale = 0, 0
            if not connected:
                log.info("✅ 已连接微信，开始接收消息")
                connected = True
                self.set_status("ok", ok=True, force=True)
            else:
                self.set_status("ok", ok=True)
            if resp.get("longpolling_timeout_ms"):
                self.poll_timeout = max(5.0, resp["longpolling_timeout_ms"] / 1000)
            for m in resp.get("msgs") or []:
                asyncio.create_task(self.safe(self.on_weixin_message(m)))
            new_cursor = resp.get("get_updates_buf")
            if new_cursor is not None and new_cursor != self.cursor:
                self.cursor = new_cursor
                self.cursor_file.write_text(new_cursor, "utf-8")

    # ---------- 会话失效后重新登录 ----------
    async def _session_ok(self) -> bool:
        try:
            r = await self.call("ilink/bot/getupdates", {"get_updates_buf": self.cursor}, timeout=8)
        except Exception as e:
            return _is_timeout(e)   # 服务器在长轮询中挂起 = 会话正常
        return (r.get("errcode") or r.get("ret") or 0) != STALE_TOKEN_ERRCODE

    async def relogin(self):
        """把登录二维码发到邮箱，等你扫码；没扫就隔一段时间再发一张新的。
        期间如果你在终端手动 --login 了，或者会话自己恢复了，就直接继续工作。"""
        token_file = self.cfg.weixin_token_file
        attempt = 0
        while True:
            if attempt:
                delay = RELOGIN_RETRY[min(attempt - 1, len(RELOGIN_RETRY) - 1)]
                if await self._wait(delay):
                    return
            attempt += 1
            if await self._session_ok():
                log.info("微信会话已自行恢复")
                return
            if not self.notifier.configured:
                self.set_status("relogin", "需要重新扫码，但没有配置告警邮箱，请在 Mac mini 上运行 bot.py --login")
                log.error("没有配置告警邮箱，无法把二维码发给你。请在 Mac mini 终端运行：.venv/bin/python bot.py --login")
                if await self._wait(RELOGIN_RETRY[-1]):
                    return
                continue
            try:
                qr = await asyncio.to_thread(request_qr, self.base, _old_tokens(token_file))
            except Exception as e:
                log.warning("获取登录二维码失败：%r", e)
                self.set_status("relogin", f"获取登录二维码失败：{e!r}"[:300])
                continue
            link = qr["qrcode_img_content"]
            next_in = RELOGIN_RETRY[min(attempt - 1, len(RELOGIN_RETRY) - 1)] // 60
            body = (
                "知识库机器人的微信会话失效了，需要你重新扫码确认。\n\n"
                "方法一：在电脑上打开这封邮件，用手机微信“扫一扫”扫描下方二维码，然后在手机上确认。\n"
                "方法二：在手机上打开这封邮件，长按下方二维码图片 →“识别图中二维码”。\n"
                f"方法三：在手机微信里打开这个链接：{link}\n\n"
                f"二维码几分钟内有效。如果过期了，{next_in} 分钟后会自动再发一张新的。\n"
                f"也可以通过屏幕共享在 Mac mini 终端运行：cd {core.BASE_DIR} && .venv/bin/python bot.py --login\n"
                "（重新绑定后机器人会自动恢复，不需要重启。）"
            )
            self.notifier.send("⚠️ 知识库：微信需要重新扫码", body, image_png=qr_png(link))
            self.set_status("relogin", "等待重新扫码，二维码已发到邮箱")

            poll_base, deadline = self.base, time.time() + QR_WAIT_SECONDS
            while time.time() < deadline:
                if self._token_changed():
                    self.set_status("ok", "已手动重新绑定", ok=True, force=True)
                    return
                st = await asyncio.to_thread(poll_qr, poll_base, qr["qrcode"])
                status = st.get("status")
                if status == "confirmed" and st.get("bot_token") and st.get("ilink_bot_id"):
                    save_token(token_file, st, poll_base)
                    self._load_token()
                    log.info("✅ 已通过邮件二维码重新绑定微信")
                    self.notifier.send("✅ 知识库：微信已重新绑定", "扫码成功，机器人已恢复工作。")
                    self.set_status("ok", "已重新绑定", ok=True, force=True)
                    return
                if status == "binded_redirect":
                    log.info("服务器表示原来的绑定仍然有效，继续使用原凭证")
                    self.set_status("ok", "原绑定仍有效", ok=True, force=True)
                    return
                if status == "scaned_but_redirect" and st.get("redirect_host"):
                    poll_base = f"https://{st['redirect_host']}"
                elif status in ("need_verifycode", "verify_code_blocked"):
                    self.notifier.send(
                        "⚠️ 知识库：扫码需要输入验证码",
                        "微信要求在 Mac mini 上输入手机显示的数字，邮件扫码这条路走不通了。\n"
                        "请通过屏幕共享连上 Mac mini，在终端运行：\n"
                        f"cd {core.BASE_DIR} && .venv/bin/python bot.py --login\n"
                        "按提示扫码并输入数字，完成后机器人会自动恢复。")
                    break
                elif status == "expired":
                    break
                await asyncio.sleep(1)

    # ---------- 发消息 ----------
    async def reply_to(self, to_user: str, context_token: str, text: str):
        await self.call("ilink/bot/sendmessage", {"msg": {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": f"kb-{uuid.uuid4().hex}",
            "message_type": MSG_FROM_BOT,
            "message_state": STATE_FINISH,
            "context_token": context_token,
            "item_list": [{"type": TEXT, "text_item": {"text": _plain(text)[:4000]}}],
        }})

    # ---------- 处理 ----------
    async def on_weixin_message(self, m: dict):
        if m.get("message_type") != MSG_FROM_USER:
            return
        if m.get("group_id"):
            return  # 只处理和机器人的单聊
        mid = str(m.get("message_id") or m.get("client_id") or "")
        if mid and mid in self.state.seen:
            return
        if mid:
            self.state.seen.append(mid)
        user = m.get("from_user_id", "")
        allowed = {self.owner, *self.cfg.weixin_allowed} - {""}
        if allowed and user not in allowed:
            log.info("忽略未授权用户 %s 的消息", user)
            return

        now = datetime.now()
        try:
            result = await self.handle_items(m.get("item_list") or [], user, now)
        except Exception as e:
            log.exception("保存失败")
            result = f"⚠️ 保存失败：{e}"
        self.state.save()
        if result and m.get("context_token"):
            try:
                await self.reply_to(user, m["context_token"], result)
            except Exception as e:
                log.warning("回复失败（笔记已保存）：%s", e)

    @staticmethod
    def _ref_text(items):
        for it in items:
            ref = it.get("ref_msg") or {}
            mi = ref.get("message_item") or {}
            t = (mi.get("text_item") or {}).get("text") or (mi.get("voice_item") or {}).get("text") \
                or ref.get("title")
            if t:
                return t.replace("\n", " ")[:200]
        return ""

    async def handle_items(self, items, user, now):
        quote = self._ref_text(items)
        texts, embeds, kinds = [], [], []
        for it in items:
            t = it.get("type")
            if t == TEXT:
                texts.append((it.get("text_item") or {}).get("text", ""))
                kinds.append("text")
            elif t == VOICE:
                v = it.get("voice_item") or {}
                if v.get("text"):
                    texts.append(v["text"])
                else:
                    try:
                        data = await asyncio.to_thread(download_media, VOICE, v, self.cdn)
                        p = self.vault.save_attachment(data, f"{now:%Y%m%d-%H%M%S}.silk", now)
                        embeds.append(f"🎙 语音（未能自动转文字）[[{p}]]")
                    except Exception as e:
                        embeds.append(f"🎙 语音（未能转文字，也未能保存：{e}）")
                kinds.append("voice")
            elif t in (IMAGE, FILE, VIDEO):
                key = {IMAGE: "image_item", FILE: "file_item", VIDEO: "video_item"}[t]
                item = it.get(key) or {}
                try:
                    data = await asyncio.to_thread(download_media, t, item, self.cdn)
                    if t == FILE and item.get("file_name"):
                        name = item["file_name"]
                    else:
                        name = f"{now:%Y%m%d-%H%M%S}{core.guess_ext(data)}"
                    p = self.vault.save_attachment(data, name, now)
                    label = Path(p).name
                    embeds.append(f"![[{p}]]" if t == IMAGE else f"📎 [[{p}|{label}]]")
                except Exception as e:
                    embeds.append(f"（{'图片' if t == IMAGE else '文件'}保存失败：{e}）")
                kinds.append({IMAGE: "image", FILE: "file", VIDEO: "video"}[t])

        text = "\n".join(x for x in texts if x).strip()
        if not text and not embeds:
            return "（空消息或暂不支持的消息类型，未保存）"

        # 纯文字时先看是不是指令；纯语音时看是不是在等待追加/改写内容
        if text and not embeds and kinds in (["text"], ["voice"]):
            cmd = self.command(text, user, now, voice=(kinds == ["voice"]))
            if cmd is not None:
                return await cmd if asyncio.iscoroutine(cmd) else cmd

        if text:
            rel, name, text = self.vault.route(text)
        else:
            rel, name = self.cfg.inbox_dir, "收件箱"
        body = "\n".join([text] + embeds).strip()
        if quote:
            body += f"\n\n> 引用：{quote}"
        mtype = kinds[0] if len(set(kinds)) == 1 else "mixed"
        slug = text or {"image": "图片", "file": "文件", "video": "视频", "voice": "语音"}.get(mtype, "记录")
        meta = {"source": "微信", "type": mtype, "author": self.cfg.names.get(user, user)}
        await self.vault.create_note(rel, body, now, meta, slug=slug)

        if mtype == "voice" and text:
            return f"✓ 已存入 {name}\n「{text}」"
        label = {"image": "🖼 图片", "file": "📎 文件", "video": "🎬 视频"}.get(mtype)
        return f"{label}已存入 {name}" if label else f"✓ 已存入 {name}"
