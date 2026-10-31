#!/usr/bin/env python3
"""
巡检、告警、日报、备份。由 launchd 每 30 分钟运行一次（install.sh 会自动设置）。

每次运行：
  1. 检查机器人、数据库、Tailscale、Obsidian、硬盘、备份是否正常
     - 能自己修好的先尝试修复（重启数据库、重新打开 Obsidian、恢复 Tailscale 的 HTTPS 共享）
     - 修不好的发邮件告警；同一个问题 12 小时内不重复发；恢复后发一封“已恢复”
  2. 到了凌晨（默认 3 点后）且今天还没备份 → 备份知识库，保留 30 天
  3. 到了早上（默认 8 点后）且今天还没发日报 → 发日报

手动用法（在程序所在文件夹里运行）：
  .venv/bin/python monitor.py --test-email   发一封测试邮件
  .venv/bin/python monitor.py --check        立即巡检并打印结果（不发告警）
  .venv/bin/python monitor.py --backup       立即备份
  .venv/bin/python monitor.py --report       立即发一份日报
"""
import argparse
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
import bot as core  # noqa: E402
sys.modules.setdefault("bot", core)
from notify import Notifier  # noqa: E402

log = logging.getLogger("kb-bot")

LABEL_BOT = "com.kb.wecom-bot"
CRED_FILE = Path.home() / ".kb-livesync" / "credentials.txt"
REALERT_HOURS = 12
BOT_STALE_MINUTES = 15
TAILSCALE_CANDIDATES = ["/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                        "/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale"]
BREW_CANDIDATES = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]


def _run(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def _first(paths):
    return next((p for p in paths if Path(p).exists()), None)


def _http_ok(url, user=None, pw=None, timeout=10):
    req = urllib.request.Request(url)
    if user:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=core.SSL_CTX))
    try:
        with opener.open(req, timeout=timeout) as r:
            return 200 <= r.status < 300, ""
    except Exception as e:
        return False, str(e)[:200]


def _couch_cred():
    d = {}
    if CRED_FILE.exists():
        for line in CRED_FILE.read_text("utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d.get("COUCH_USER"), d.get("COUCH_PASS")


# ───────────────────────────── 备份 ─────────────────────────────

def do_backup(cfg) -> Path:
    """把知识库和机器人配置打包成 tar.gz，校验后保留最近 keep_days 天。"""
    dest = cfg.backup_dir
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    final = dest / f"知识库-{stamp}.tar.gz"
    tmp = dest / f".{final.name}.part"
    vault = cfg.vault

    def skip(ti):
        name = Path(ti.name).name
        return None if name.endswith(".tmp") or name == ".DS_Store" else ti

    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(vault, arcname=vault.name, filter=skip)
        conf = BASE_DIR / "config.toml"
        if conf.exists():
            tar.add(conf, arcname="机器人配置/config.toml")
    # 校验：能完整读一遍、且包含笔记
    with tarfile.open(tmp, "r:gz") as tar:
        names = tar.getnames()
    if not any(n.endswith(".md") for n in names):
        tmp.unlink(missing_ok=True)
        raise RuntimeError("备份里没有任何笔记，可能知识库路径不对")
    tmp.replace(final)
    os.chmod(final, 0o600)

    cutoff = time.time() - cfg.backup_keep_days * 86400
    for old in dest.glob("知识库-*.tar.gz"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    log.info("备份完成：%s（%.1f MB）", final, final.stat().st_size / 1e6)
    return final


def latest_backup(cfg):
    files = sorted(cfg.backup_dir.glob("知识库-*.tar.gz"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


# ───────────────────────────── 巡检 ─────────────────────────────

class Check:
    def __init__(self, key, name, ok, detail="", fixed=""):
        self.key, self.name, self.ok, self.detail, self.fixed = key, name, ok, detail, fixed


def check_bot(cfg):
    code, out = _run(["/bin/launchctl", "list"])
    if code == 0 and LABEL_BOT not in out:
        return Check("bot", "机器人", False, f"后台服务没有加载。在 Mac mini 上运行 {BASE_DIR}/install.sh 重新安装")
    if cfg.channel != "weixin":
        return Check("bot", "机器人", True, "企业微信通道")
    st_file = BASE_DIR / "status.json"
    if not st_file.exists():
        return Check("bot", "机器人", False, "没有状态文件，机器人可能没在运行。查看日志：~/Library/Logs/kb-wecom-bot.log")
    st = json.loads(st_file.read_text("utf-8"))
    state = st.get("state")
    if state == "relogin":
        # 机器人自己会把登录二维码发到邮箱，这里只在日报里体现，不重复告警
        return Check("bot", "机器人", True, "微信会话失效，等待重新扫码（二维码已发到邮箱）")
    if state == "paused":
        return Check("bot", "机器人", True, "微信会话暂时失效，按官方做法暂停中，会自动重试")
    age = (time.time() - st.get("last_ok", 0)) / 60
    if age > BOT_STALE_MINUTES:
        return Check("bot", "机器人", False,
                     f"已经 {age:.0f} 分钟没有成功连上微信。最近的错误：{st.get('detail') or '无'}")
    return Check("bot", "机器人", True, "在线")


def check_couchdb(fix=True):
    user, pw = _couch_cred()
    if not user:
        return Check("couchdb", "同步数据库", True, "未安装 LiveSync 数据库，跳过")
    ok, err = _http_ok("http://127.0.0.1:5984/_up", user, pw)
    if ok:
        return Check("couchdb", "同步数据库", True, "正常")
    fixed = ""
    brew = _first(BREW_CANDIDATES)
    if fix and brew:
        _run([brew, "services", "restart", "couchdb"], timeout=60)
        for _ in range(15):
            time.sleep(2)
            if _http_ok("http://127.0.0.1:5984/_up", user, pw)[0]:
                return Check("couchdb", "同步数据库", True, "曾无响应，已自动重启恢复", fixed="已自动重启数据库")
        fixed = "尝试自动重启，但没有恢复"
    return Check("couchdb", "同步数据库", False, f"数据库无响应（{err}）。{fixed}")


def tailscale_status(retries=3):
    """返回 (tailscale 程序路径, status 字典或 None, 出错信息)。
    只解析 stdout：tailscale 偶尔往 stderr 打警告（比如版本不一致），混进来会导致 JSON 解析失败。
    命令行偶尔会超时或返回非 0（例如刚从睡眠唤醒），所以失败时隔几秒重试。"""
    ts = _first(TAILSCALE_CANDIDATES)
    if not ts:
        return None, None, ""
    err = ""
    for i in range(retries):
        if i:
            time.sleep(5)
        try:
            r = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=30)
        except Exception as e:
            err = str(e)
            continue
        if r.returncode != 0:
            err = (r.stderr or r.stdout or f"退出码 {r.returncode}").strip()
            continue
        try:
            return ts, json.loads(r.stdout), ""
        except ValueError:
            err = "输出不是 JSON：" + (r.stdout or r.stderr or "").strip()[:120]
    return ts, None, err[:200]


def _ts_host(st=None):
    """上次成功时记下 Mac mini 的域名；tailscale 命令失灵时用它直接测 HTTPS。"""
    f = BASE_DIR / "tailscale_host.txt"
    host = (((st or {}).get("Self") or {}).get("DNSName") or "").rstrip(".")
    if host:
        try:
            if not f.exists() or f.read_text().strip() != host:
                f.write_text(host)
        except OSError:
            pass
        return host
    try:
        return f.read_text().strip()
    except OSError:
        return ""


def check_tailscale(fix=True):
    ts, st, err = tailscale_status()
    if not ts:
        return Check("tailscale", "Tailscale", True, "未安装，跳过")
    user, pw = _couch_cred()
    host = _ts_host(st)
    if not st or st.get("BackendState") != "Running":
        state = st.get("BackendState") if st else "未知"
        # 命令行查不到状态，但其他设备实际能连上数据库，就不算故障（最终目的是同步可用）
        if st is None and user and host:
            ok, _ = _http_ok(f"https://{host}/_up", user, pw, timeout=20)
            if ok:
                return Check("tailscale", "Tailscale", True,
                             "在线（tailscale 命令暂时没有响应，但 HTTPS 访问数据库正常）")
        why = f"；命令输出：{err}" if err else ""
        return Check("tailscale", "Tailscale", False,
                     f"Tailscale 没有连接（状态：{state}{why}）。请通过屏幕共享检查 Tailscale 是否已登录")
    detail = "在线"
    if user and host:
        url = f"https://{host}/_up"
        ok, err = _http_ok(url, user, pw, timeout=20)
        if not ok and fix:
            _run([ts, "serve", "--bg", "5984"])
            time.sleep(3)
            ok, err2 = _http_ok(url, user, pw, timeout=20)
            if ok:
                return Check("tailscale", "Tailscale", True, detail + "；HTTPS 共享曾中断，已自动恢复",
                             fixed="已恢复 Tailscale HTTPS 共享")
        if not ok:
            return Check("tailscale", "Tailscale", False,
                         f"其他设备无法通过 {url} 访问数据库（{err}），MacBook 和手机将无法同步")
    return Check("tailscale", "Tailscale", True, detail)


def check_tailscale_key():
    _, st, _ = tailscale_status(retries=1)
    exp = ((st or {}).get("Self") or {}).get("KeyExpiry")
    if not exp:
        return Check("tailscale_key", "Tailscale 密钥", True, "不会过期" if st else "未知")
    try:
        days = (datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.now().astimezone()).days
    except ValueError:
        return Check("tailscale_key", "Tailscale 密钥", True, f"过期时间 {exp}")
    if days < 14:
        return Check("tailscale_key", "Tailscale 密钥", False,
                     f"Mac mini 的 Tailscale 密钥 {days} 天后过期，过期后同步会中断。"
                     "请到 login.tailscale.com → Machines → Mac mini → … → Disable key expiry")
    return Check("tailscale_key", "Tailscale 密钥", True, f"{days} 天后过期（建议在后台关闭密钥过期）")


def check_obsidian(fix=True):
    if sys.platform != "darwin":
        return Check("obsidian", "Obsidian", True, "非 macOS，跳过")
    code, _ = _run(["/usr/bin/pgrep", "-x", "Obsidian"])
    if code == 0:
        return Check("obsidian", "Obsidian", True, "运行中")
    if fix:
        _run(["/usr/bin/open", "-ga", "Obsidian"])
        time.sleep(15)
        if _run(["/usr/bin/pgrep", "-x", "Obsidian"])[0] == 0:
            return Check("obsidian", "Obsidian", True, "曾被关闭，已自动重新打开", fixed="已自动重新打开 Obsidian")
    return Check("obsidian", "Obsidian", False,
                 "Obsidian 没有运行，机器人新存的笔记暂时不会同步到其他设备")


def check_disk(cfg):
    free = shutil.disk_usage(Path.home()).free / 1e9
    if free < cfg.min_free_gb:
        return Check("disk", "硬盘", False, f"剩余空间只有 {free:.1f} GB")
    return Check("disk", "硬盘", True, f"剩余 {free:.0f} GB")


def check_backup(cfg):
    b = latest_backup(cfg)
    if not b:
        return Check("backup", "备份", True, "还没有备份（今天凌晨会自动执行）")
    hours = (time.time() - b.stat().st_mtime) / 3600
    if hours > 30:
        return Check("backup", "备份", False, f"最近一次备份是 {hours:.0f} 小时前，备份可能出问题了")
    return Check("backup", "备份", True, f"{datetime.fromtimestamp(b.stat().st_mtime):%m-%d %H:%M}（{b.stat().st_size / 1e6:.1f} MB）")


def run_checks(cfg, fix=True):
    checks = []
    for fn in (lambda: check_bot(cfg), lambda: check_couchdb(fix), lambda: check_tailscale(fix),
               check_tailscale_key, lambda: check_obsidian(fix), lambda: check_disk(cfg), lambda: check_backup(cfg)):
        try:
            checks.append(fn())
        except Exception as e:
            log.exception("巡检项出错")
            checks.append(Check("monitor", "巡检程序", False, f"检查时出错：{e!r}"))
    return checks


# ───────────────────────────── 日报 ─────────────────────────────

def note_stats(cfg, day):
    """统计某一天新建的笔记（按文件名开头的日期）和笔记总数。"""
    prefix = f"{day:%Y-%m-%d} "
    total, by_dir = 0, {}
    for dirpath, dirnames, files in os.walk(cfg.vault):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in files:
            if f.endswith(".md") and not f.startswith("."):
                total += 1
                if f.startswith(prefix):
                    rel = Path(dirpath).relative_to(cfg.vault).as_posix()
                    by_dir[rel] = by_dir.get(rel, 0) + 1
    return total, by_dir


def build_report(cfg, checks):
    y = datetime.now() - timedelta(days=1)
    total, by_dir = note_stats(cfg, y)
    n = sum(by_dir.values())
    bad = [c for c in checks if not c.ok]
    lines = [f"昨天（{y:%m-%d}）新增 {n} 篇笔记" + ("：" if n else "")]
    lines += [f"  · {d}：{k} 篇" for d, k in sorted(by_dir.items())]
    lines += [f"知识库共 {total} 篇笔记", "", "系统状态："]
    lines += [f"{'✅' if c.ok else '❌'} {c.name}：{c.detail}" for c in checks]
    subject = f"📒 知识库日报 {datetime.now():%m-%d} " + ("✅ 一切正常" if not bad else f"⚠️ {len(bad)} 项异常")
    lines += ["", "没收到某天的日报，说明 Mac mini 可能整机宕机或断网了。"]
    return subject, "\n".join(lines)


# ───────────────────────────── 主流程 ─────────────────────────────

def load_state():
    p = BASE_DIR / "monitor_state.json"
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return {"issues": {}}


def save_state(st):
    p = BASE_DIR / "monitor_state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def run(cfg, notifier, now=None):
    now = now or datetime.now()
    st = load_state()
    issues = st.setdefault("issues", {})

    # 备份
    today = now.strftime("%Y-%m-%d")
    if now.hour >= cfg.backup_hour and st.get("last_backup") != today:
        try:
            do_backup(cfg)
            st["last_backup"] = today
            issues.pop("backup_failed", None)
        except Exception as e:
            log.exception("备份失败")
            if "backup_failed" not in issues:
                notifier.send("⚠️ 知识库告警：备份失败", f"今天的自动备份失败了：\n{e!r}\n\n明天会再试一次。")
                issues["backup_failed"] = {"since": time.time(), "last_sent": time.time()}

    checks = run_checks(cfg, fix=True)

    # 告警与恢复通知
    current = {c.key: c for c in checks if not c.ok}
    for key, c in current.items():
        it = issues.get(key)
        if not it or time.time() - it.get("last_sent", 0) > REALERT_HOURS * 3600:
            again = "（仍未恢复）" if it else ""
            sent = notifier.send(f"⚠️ 知识库告警：{c.name}异常{again}",
                                 f"{c.detail}\n\n检查时间：{now:%Y-%m-%d %H:%M}\n恢复后会再通知你。")
            issues[key] = {"since": (it or {}).get("since", time.time()),
                           "last_sent": time.time() if sent else (it or {}).get("last_sent", 0), "name": c.name}
    for key in [k for k in issues if k not in current and k != "backup_failed"]:
        it = issues.pop(key)
        if it.get("last_sent"):
            mins = (time.time() - it.get("since", time.time())) / 60
            notifier.send(f"✅ 知识库已恢复：{it.get('name', key)}",
                          f"{it.get('name', key)} 已恢复正常（持续约 {mins:.0f} 分钟）。")
    for c in checks:
        if c.ok and c.fixed:
            log.info("自动修复：%s", c.fixed)

    # 日报
    if now.hour >= cfg.report_hour and st.get("last_report") != today:
        subject, body = build_report(cfg, checks)
        if notifier.send(subject, body):
            st["last_report"] = today

    save_state(st)
    for c in checks:
        log.info("%s %s：%s", "✓" if c.ok else "✗", c.name, c.detail)
    return checks


def main():
    ap = argparse.ArgumentParser(description="知识库巡检、告警、日报、备份")
    ap.add_argument("-c", "--config", default=str(BASE_DIR / "config.toml"))
    ap.add_argument("--test-email", action="store_true", help="发一封测试邮件")
    ap.add_argument("--check", action="store_true", help="立即巡检并打印（不发告警、不自动修复）")
    ap.add_argument("--backup", action="store_true", help="立即备份")
    ap.add_argument("--report", action="store_true", help="立即发日报")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    cfg = core.Config(Path(args.config))
    core.USE_SYSTEM_PROXY = cfg.use_system_proxy
    notifier = Notifier(cfg.alert, core.SSL_CTX)

    if args.test_email:
        if not notifier.configured:
            print("✗ 还没有配置告警邮箱：在 config.toml 的 [alert] 里填写 email 和 auth_code")
            sys.exit(1)
        ok = notifier.send("✅ 知识库机器人测试邮件",
                           "收到这封邮件，说明告警和日报能正常发到你的邮箱。\n"
                           "建议在微信里开启 QQ 邮箱提醒，这样告警会直接弹在微信里。")
        print("✓ 测试邮件已发送，请查收" if ok else "✗ 发送失败，请看上面的错误信息")
        sys.exit(0 if ok else 1)
    if args.check:
        for c in run_checks(cfg, fix=False):
            print(f"{'✓' if c.ok else '✗'} {c.name}：{c.detail}")
        return
    if args.backup:
        p = do_backup(cfg)
        print(f"✓ 已备份到 {p}（{p.stat().st_size / 1e6:.1f} MB）")
        return
    if args.report:
        subject, body = build_report(cfg, run_checks(cfg, fix=False))
        print(subject + "\n" + body)
        notifier.send(subject, body)
        return
    run(cfg, notifier)


if __name__ == "__main__":
    main()
