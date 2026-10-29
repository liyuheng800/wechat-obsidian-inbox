#!/bin/bash
# 在 Mac mini 上安装 CouchDB 并按 Obsidian Self-hosted LiveSync 的要求配置，
# 然后用 Tailscale Serve 提供 HTTPS 地址给 MacBook 和手机使用。
# 可以重复运行：已有的管理员密码和数据不会被改动。
set -e
# 固定为一定存在的语言环境，避免 C.UTF-8 等不存在的设置让系统工具崩溃
if locale -a 2>/dev/null | grep -qx 'en_US.UTF-8'; then
  export LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
else
  export LC_ALL=C LANG=C
fi

DB_NAME="obsidian"
CRED_DIR="$HOME/.kb-livesync"
CRED_FILE="$CRED_DIR/credentials.txt"
MARK_BEGIN="; >>> kb-livesync begin"
MARK_END="; <<< kb-livesync end"

say()  { printf "\n\033[1m%s\033[0m\n" "$*"; }
fail() { printf "\n❌ %s\n" "$*"; exit 1; }

# ── 0. 前置检查 ───────────────────────────────────────────────
[ "$(uname)" = "Darwin" ] || fail "这个脚本只能在 Mac 上运行"
[ "$(uname -m)" = "arm64" ] || echo "⚠️  不是 Apple 芯片，Homebrew 可能需要从源码编译 CouchDB，会很慢"

OS_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
if [ "$OS_MAJOR" -lt 15 ]; then
  fail "当前 macOS 版本是 $(sw_vers -productVersion)。Homebrew 的 CouchDB 预编译包需要 macOS 15 (Sequoia) 或更新，请先在“系统设置 → 通用 → 软件更新”里升级"
fi

if ! command -v brew >/dev/null 2>&1; then
  for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$p" ] && eval "$("$p" shellenv)" && break
  done
fi
command -v brew >/dev/null 2>&1 || fail "没有找到 Homebrew。请先按 README 里的说明安装 Homebrew，再运行本脚本"

PREFIX="$(brew --prefix)"

# ── 1. 安装 CouchDB ───────────────────────────────────────────
say "① 安装 CouchDB（第一次大约需要几分钟）"
if brew list couchdb >/dev/null 2>&1; then
  echo "已安装：$(brew list --versions couchdb)"
else
  brew install couchdb
fi
# 防止 brew upgrade 自动升级 CouchDB 后配置丢失；需要升级时先 brew unpin couchdb，升级后重跑本脚本
brew pin couchdb >/dev/null 2>&1 || true

# 找到 CouchDB 实际读取的 local.ini：它读的是自己安装目录下的 etc/local.ini。
# 用 realpath 解析符号链接，始终在真实文件上修改。
COUCH_HOME="$(brew --prefix couchdb)"
realpath_py() { /usr/bin/python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$1"; }
INI="$(realpath_py "$COUCH_HOME/etc/local.ini")"
[ -f "$INI" ] || fail "找不到 CouchDB 读取的配置文件：$COUCH_HOME/etc/local.ini"
echo "CouchDB 实际读取的配置：$INI"

# 修复旧版脚本造成的分裂：旧版把 $PREFIX/etc/local.ini 从符号链接替换成了普通文件。
# 若发现该文件是普通文件、带有本脚本的标记、且不是真实配置，就备份它并恢复成指向真实配置的链接。
STRAY="$PREFIX/etc/local.ini"
if [ -f "$STRAY" ] && [ ! -L "$STRAY" ] && [ "$(realpath_py "$STRAY")" != "$INI" ] \
   && grep -q "kb-livesync begin" "$STRAY"; then
  BAK="$CRED_DIR/local.ini.stray-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$CRED_DIR" && chmod 700 "$CRED_DIR"
  mv "$STRAY" "$BAK"
  ln -s "$INI" "$STRAY"
  echo "已修复：旧版脚本写错的配置副本已备份到 $BAK，并把 $STRAY 恢复为指向真实配置的链接"
fi

# ── 2+3. 管理员账号和 LiveSync 配置 ─────────────────────────────
# 用 Python 修改 local.ini（不用 perl，避免语言环境问题导致崩溃）。
# 是否已有管理员以 local.ini 为准，而不是看凭据文件，所以中断后重跑也能恢复。
say "② 设置管理员账号并写入 LiveSync 配置"
mkdir -p "$CRED_DIR" && chmod 700 "$CRED_DIR"

/usr/bin/python3 - "$INI" "$CRED_FILE" "$MARK_BEGIN" "$MARK_END" <<'PY'
import os, re, secrets, string, sys
ini_path, cred_path, mark_begin, mark_end = sys.argv[1:5]
ini_path = os.path.realpath(ini_path)  # 始终修改真实文件，绝不替换符号链接本身

text = open(ini_path, encoding="utf-8").read() if os.path.exists(ini_path) else ""

# 读取已保存的凭据
cred = {}
if os.path.exists(cred_path):
    for line in open(cred_path, encoding="utf-8"):
        if "=" in line:
            k, v = line.strip().split("=", 1)
            cred[k] = v
user = cred.get("COUCH_USER", "admin")

# 找出 [admins] 段里该用户的有效（非注释）行
def admin_line(t):
    sec = None
    for line in t.splitlines():
        s = line.strip()
        m = re.match(r"^\[(.+)\]$", s)
        if m:
            sec = m.group(1)
        elif sec == "admins" and not s.startswith(";") and re.match(rf"^{re.escape(user)}\s*=", s):
            return s.split("=", 1)[1].strip()
    return None

existing = admin_line(text)
if existing is None:
    pw = cred.get("COUCH_PASS") or "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(24))
    entry = f"{user} = {pw}"
    if re.search(r"(?m)^\[admins\]\s*$", text):
        text = re.sub(r"(?m)^\[admins\]\s*$", "[admins]\n" + entry, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n\n[admins]\n{entry}\n"
    print(f"已写入管理员账号 {user}")
else:
    if "COUCH_PASS" not in cred:
        sys.exit(f"local.ini 里已有管理员 {user}，但找不到对应的密码文件 {cred_path}。"
                 "请删除 local.ini 中 [admins] 段下的这一行后重新运行。")
    pw = cred["COUCH_PASS"]
    print(f"沿用已有管理员 {user}")

# 保存凭据（只含字母数字，可被 bash source）
with open(cred_path, "w", encoding="utf-8") as f:
    f.write(f"COUCH_USER={user}\nCOUCH_PASS={pw}\n")
os.chmod(cred_path, 0o600)

# 替换（或新增）LiveSync 配置块
block = f"""{mark_begin}
[couchdb]
single_node = true
max_document_size = 50000000

[chttpd]
; 只监听本机，外部设备一律通过 Tailscale 的 HTTPS 访问
bind_address = 127.0.0.1
port = 5984
require_valid_user = true
max_http_request_size = 4294967296
enable_cors = true

[chttpd_auth]
require_valid_user = true
authentication_redirect = /_utils/session.html

[httpd]
WWW-Authenticate = Basic realm="couchdb"
enable_cors = true

[cors]
origins = app://obsidian.md,capacitor://localhost,http://localhost
credentials = true
headers = accept, authorization, content-type, origin, referer
methods = GET, PUT, POST, HEAD, DELETE
max_age = 3600
{mark_end}
"""
text = re.sub(re.escape(mark_begin) + r".*?" + re.escape(mark_end) + r"\n?", "", text, flags=re.S)
text = text.rstrip("\n") + "\n\n" + block

tmp = ini_path + ".tmp"
open(tmp, "w", encoding="utf-8").write(text)
os.replace(tmp, ini_path)
PY

# shellcheck disable=SC1090
source "$CRED_FILE"
echo "配置文件：$INI"
echo "密码保存在 $CRED_FILE"

# ── 4. 启动并设为开机自启 ─────────────────────────────────────
say "④ 启动 CouchDB 并设为开机自启"
brew services restart couchdb >/dev/null
printf "等待 CouchDB 启动"
for i in $(seq 1 30); do
  if curl -fs -u "$COUCH_USER:$COUCH_PASS" http://127.0.0.1:5984/_up >/dev/null 2>&1; then
    echo " ✓"; break
  fi
  printf "."; sleep 1
  if [ "$i" = 30 ]; then
    brew services info couchdb 2>/dev/null || true
    fail "CouchDB 30 秒内没有启动成功。要看具体错误，可以先 brew services stop couchdb，再在前台运行：$COUCH_HOME/bin/couchdb （看完按 Ctrl+C 退出）"
  fi
done

# 向 CouchDB 查询配置，确认它读到的正是我们写的那份
cfg() { curl -fs -u "$COUCH_USER:$COUCH_PASS" "http://127.0.0.1:5984/_node/_local/_config/$1"; }
if [ "$(cfg chttpd/require_valid_user)" = '"true"' ] && cfg cors/origins | grep -q "capacitor://localhost"; then
  echo "✓ 已确认 CouchDB 加载了 LiveSync 配置"
else
  fail "CouchDB 启动了，但没有读到 LiveSync 配置（require_valid_user=$(cfg chttpd/require_valid_user)）。请把这段输出发给 Claude"
fi

# ── 5. 创建数据库 ─────────────────────────────────────────────
say "⑤ 创建数据库"
for db in _users _replicator "$DB_NAME"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' -u "$COUCH_USER:$COUCH_PASS" -X PUT "http://127.0.0.1:5984/$db")
  case "$code" in
    201|202) echo "已创建 $db" ;;
    412)     echo "已存在 $db" ;;
    401)     fail "管理员密码不对。若你手动改过 local.ini，请删除 $CRED_FILE 和 local.ini 里的 [admins] 段后重新运行" ;;
    *)       fail "创建 $db 失败（HTTP $code）" ;;
  esac
done

# ── 6. Tailscale HTTPS ────────────────────────────────────────
say "⑥ 配置 Tailscale HTTPS 地址"
TS=""
for c in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then TS="$c"; break; fi
done

URL=""
if [ -z "$TS" ]; then
  echo "⚠️  没有找到 Tailscale。请按 README 安装并登录 Tailscale 后，重新运行本脚本"
elif ! "$TS" status >/dev/null 2>&1; then
  echo "⚠️  Tailscale 还没登录或未连接。打开 Tailscale 登录后，重新运行本脚本"
else
  if "$TS" serve --bg 5984 >/tmp/kb-ts-serve.log 2>&1; then
    HOST=$("$TS" status --json | /usr/bin/python3 -c 'import json,sys;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
    URL="https://$HOST"
    echo "✓ HTTPS 地址：$URL"
  else
    cat /tmp/kb-ts-serve.log
    echo "⚠️  tailscale serve 失败。通常是 tailnet 还没开启 HTTPS 证书："
    echo "   打开上面提示的网址点“Enable”，或在 Tailscale 管理后台 DNS 页面开启 MagicDNS 和 HTTPS Certificates，然后重新运行本脚本"
  fi
fi

# ── 7. 汇总 ───────────────────────────────────────────────────
say "✅ 完成。在 Obsidian 的 Self-hosted LiveSync 里填写："
cat <<EOF
   Server URI : ${URL:-（Tailscale 配好后重新运行本脚本获取）}
   Username   : $COUCH_USER
   Password   : 运行 cat $CRED_FILE 查看（为安全起见不直接显示）
   Database   : $DB_NAME

   Mac mini 自己的 Obsidian 也填这个 HTTPS 地址（Setup URI 会带上它）
EOF
