#!/bin/bash
# 安装知识库机器人：创建 Python 虚拟环境、安装依赖、设置开机自启
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.kb.wecom-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/kb-wecom-bot.log"

case "$DIR" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*|*"Mobile Documents"*)
    echo "⚠️  请把本文件夹移到用户主目录下（例如 ~/wechat-obsidian-inbox）再安装（文稿/桌面/下载里的程序无法在后台运行）"
    exit 1;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "需要 python3。请先运行：xcode-select --install ，装完后重新执行 ./install.sh"
  exit 1
fi

echo "① 安装依赖…"
[ -d "$DIR/.venv" ] || python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [ ! -f "$DIR/config.toml" ]; then
  cp "$DIR/config.example.toml" "$DIR/config.toml"
  echo
  echo "② 已生成 config.toml。请先确认里面的知识库路径（[vault] 的 path）是否正确："
  echo "   open -e \"$DIR/config.toml\""
  echo "   确认后再运行一次 ./install.sh"
  exit 0
fi

# 旧的 config.toml 缺少新功能的配置段时，从模板补上（已有内容不动）
"$DIR/.venv/bin/python" "$DIR/bot.py" --merge-config

# 普通微信通道：还没绑定时先扫码
set +e
"$DIR/.venv/bin/python" "$DIR/bot.py" --needs-login
NEEDS_LOGIN=$?
set -e
if [ "$NEEDS_LOGIN" = 10 ]; then
  if [ -t 0 ]; then
    echo "② 绑定微信：请用手机微信扫码"
    "$DIR/.venv/bin/python" "$DIR/bot.py" --login
  else
    echo
    echo "② 还没有绑定微信。请在“终端”App 里亲自运行下面这行，用手机微信扫码："
    echo "   cd \"$DIR\" && .venv/bin/python bot.py --login"
    echo "   绑定成功后再运行一次 ./install.sh"
    exit 0
  fi
fi

echo "③ 检查配置…"
"$DIR/.venv/bin/python" "$DIR/bot.py" --check

echo "④ 设置开机自启…"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/bot.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 巡检、告警、日报、备份：每 30 分钟运行一次
MLABEL="com.kb.monitor"
MPLIST="$HOME/Library/LaunchAgents/$MLABEL.plist"
MLOG="$HOME/Library/Logs/kb-monitor.log"
cat > "$MPLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$MLABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DIR/.venv/bin/python</string>
    <string>$DIR/monitor.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>1800</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>$MLOG</string>
  <key>StandardErrorPath</key><string>$MLOG</string>
</dict>
</plist>
EOF
launchctl bootout "gui/$(id -u)/$MLABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$MPLIST"

sleep 3
echo
echo "✅ 安装完成，机器人已在后台运行，开机会自动启动。"
if grep -q "填你的QQ邮箱" "$DIR/config.toml" 2>/dev/null || ! grep -q "^\[alert\]" "$DIR/config.toml"; then
  echo "⚠️  还没有配置告警邮箱：没有告警、日报，微信会话失效时也没法把二维码发给你。"
  echo "   在 config.toml 的 [alert] 里填好 email 和 auth_code，然后运行：.venv/bin/python monitor.py --test-email"
fi
echo "最近日志："
tail -n 5 "$LOG" 2>/dev/null || true
echo
echo "查看实时日志：tail -f \"$LOG\""
