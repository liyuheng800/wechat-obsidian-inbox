#!/bin/bash
# 停止机器人并取消开机自启（不会删除你的知识库和配置）
LABEL="com.kb.wecom-bot"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/com.kb.monitor" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.kb.monitor.plist"
echo "已停止机器人和巡检，并取消开机自启。知识库和 config.toml 都保留着。"
