"""
邮件通知（QQ 邮箱）。

在 config.toml 里配置：
    [alert]
    email = "12345678@qq.com"      # 发件邮箱（QQ 邮箱）
    auth_code = "QQ邮箱授权码"       # 不是 QQ 密码！QQ 邮箱 → 设置 → 账号与安全 → 开启 SMTP 后生成
    to = ""                        # 收件人，留空 = 发给自己

微信里开启“QQ邮箱提醒”后，新邮件会在微信里弹出提醒。
"""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

log = logging.getLogger("kb-bot")


class Notifier:
    def __init__(self, alert: dict, ssl_ctx: ssl.SSLContext = None):
        a = alert or {}
        self.email = str(a.get("email", "")).strip()
        self.auth_code = str(a.get("auth_code", "")).strip().replace(" ", "")
        self.to = str(a.get("to", "")).strip() or self.email
        self.host = a.get("smtp_host", "smtp.qq.com")
        self.port = int(a.get("smtp_port", 465))
        self.use_ssl = bool(a.get("smtp_ssl", True))
        self.ssl_ctx = ssl_ctx or ssl.create_default_context()

    @property
    def configured(self) -> bool:
        return bool(self.email and self.auth_code and "填" not in self.email + self.auth_code)

    def send(self, subject: str, body: str, image_png: bytes = None, image_name: str = "qrcode.png") -> bool:
        """发送一封邮件；成功返回 True。失败只记日志，不抛异常（通知失败不能拖垮主流程）。"""
        if not self.configured:
            log.warning("未配置告警邮箱，跳过通知：%s", subject)
            return False
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("知识库机器人", self.email))
        msg["To"] = self.to
        msg.set_content(body)
        if image_png:
            cid = make_msgid(domain="kb.local")
            html = "<br>".join(_esc(line) for line in body.splitlines())
            msg.add_alternative(
                f'<div style="font-size:15px;line-height:1.6">{html}</div>'
                f'<p><img src="cid:{cid[1:-1]}" width="260" height="260" alt="二维码"></p>', subtype="html")
            msg.get_payload()[1].add_related(image_png, "image", "png", cid=cid, filename=image_name)
        try:
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.host, self.port, context=self.ssl_ctx, timeout=30) as s:
                    s.login(self.email, self.auth_code)
                    s.send_message(msg)
            else:  # 仅测试用
                with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                    if self.auth_code != "-":
                        s.login(self.email, self.auth_code)
                    s.send_message(msg)
            log.info("已发送通知邮件：%s", subject)
            return True
        except smtplib.SMTPAuthenticationError:
            log.error("发送邮件失败：邮箱或授权码不对（注意要用 QQ 邮箱的“授权码”，不是 QQ 密码）")
        except Exception as e:
            log.error("发送邮件失败：%r", e)
        return False


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
