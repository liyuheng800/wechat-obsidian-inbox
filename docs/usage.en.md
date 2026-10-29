# Usage

[简体中文](usage.zh-CN.md) | English · [← Back to project home](../README.en.md)

> Setting up for the first time? Start with the [full guide](guide.en.md). This page is the day-to-day reference once everything is running.

## Capturing

In the **"WeChat ClawBot"** chat (pin it to the top):

- **Text / voice**: each message becomes its own note, e.g. `00-收件箱/2026-10-27 0915 attention is a weighted sum.md`. Voice is transcribed by WeChat; the bot's reply includes the transcript so you can check it
- **Tags route notes**: `#工作` (work), `#英语` (English), `#AI`, `#踩坑` (pitfall), or messages starting with `工作：` / `踩坑：`. Full-width `＃`, any letter case, and a tag followed directly by Chinese all work. Tags, prefixes and folders are configurable
- **Photos / files**: saved to `00-收件箱/附件/` (inbox/attachments) with a note linking to them, viewable in Obsidian
- **Quotes**: long-press an earlier message → Quote → add a thought; the note keeps both
- Only the WeChat account that scanned the QR code can write; messages from anyone else are ignored

## Commands

WeChat has no clickable buttons, so browsing uses numbered lists — reply with a number to open an item. Every command has a Chinese name and an English alias:

| Chinese | English | Action |
|---|---|---|
| `/目录` | `/ls` | List the vault; reply with a number to open |
| `/上级` | `/up` | Go up one folder |
| `/搜 关键词` | `/search keyword` | Search titles and content |
| `/最近` | `/recent` | 20 most recently modified notes |
| `/下一页` · `/上一页` | `/next` · `/prev` | Page through lists and long notes |
| `/看` | `/view` | Re-open the current note |
| `/追加 内容` | `/append text` | Append to the open note; send just `/append` and your next message (text or voice) is appended |
| `/改写` | `/rewrite` | Your next message replaces the whole body (properties kept) |
| `/替换 旧=>新` | `/replace old=>new` | Find and replace in the body |
| `/重命名 新名称` | `/rename new name` | Rename |
| `/移动` | `/move` | Pick a destination folder by number |
| `/删除` | `/delete` | Move to the vault's `.trash` after replying "确认" or "yes" |
| `/取消` | `/cancel` | Cancel the pending action |
| `/今天` | `/today` | What did I capture today |
| `/撤销` | `/undo` | Remove the note just saved (not if you've edited it since) |
| `/帮助` | `/help`, `/help browse` | Help (replies are in Chinese) |

**Safety**: if a note was changed on another device after you opened it, edits are refused (send `/view` to reload); deleted notes go to the vault's `.trash`.

## Working alongside sync

Devices sync through Self-hosted LiveSync with the database on the Mac mini (see the [guide](guide.en.md#6-setup)). Keep Obsidian running on the Mac mini — it pushes the bot's new notes to other devices.

**The bot only creates notes and never edits existing ones**, so you can edit, rename, move or merge inbox notes on any device at any time without conflicting with it. Just avoid editing the same note on two devices at once.

**Browse the inbox by day** with the Dataview community plugin — create `Inbox overview.md`:

````markdown
```dataview
TABLE WITHOUT ID file.link AS Note, author AS From, type AS Type
FROM "00-收件箱"
SORT created DESC
GROUP BY dateformat(created, "yyyy-MM-dd") AS Day
```
````

## Custom routing rules

Edit `[[routes]]` in `config.toml`, e.g. a reading list:

```toml
[[routes]]
name = "Reading"
tag = "#reading"
dir = "01-Learning/Reading"
```

Run `./install.sh` to restart.

## Backups, health checks, alerts

- **Alert email**: fill `email` and `auth_code` under `[alert]` (for QQ Mail, the SMTP authorization code — not your QQ password), then run `.venv/bin/python monitor.py --test-email`. Other providers work via `smtp_host` / `smtp_port`
- **Health check** (every 30 min): bot, sync database, Tailscale (incl. key expiry), Obsidian, disk space, backups. Self-heals where possible, emails otherwise; the same issue isn't re-sent within 12 hours, and you get a "recovered" email when it clears
- **Daily report** after 8:00. No report means the Mac mini may be down or offline
- **Backups** after 3:00 to `~/kb-backups`, kept 30 days. Restore by extracting the archive for the day you need
- **Expired WeChat session**: pauses 1 hour and retries (it often recovers on its own); if still expired, emails you a login QR code — scan it from a computer, or long-press it on the phone → "scan QR code in image". `bot.py --login` over Screen Sharing works too; the running bot picks up new credentials

Times, retention and thresholds are configurable under `[backup]` and `[monitor]`.

## Useful commands

Run from the install folder (e.g. `~/wechat-obsidian-inbox`):

| Task | Command |
|---|---|
| Bot log | `tail -f ~/Library/Logs/kb-wecom-bot.log` |
| Health-check log | `tail -f ~/Library/Logs/kb-monitor.log` |
| Restart after config changes | `./install.sh` |
| Validate config | `.venv/bin/python bot.py --check` |
| Health check now (read-only) | `.venv/bin/python monitor.py --check` |
| Back up / send report now | `.venv/bin/python monitor.py --backup` / `--report` |
| Test alert email | `.venv/bin/python monitor.py --test-email` |
| Re-bind WeChat | `.venv/bin/python bot.py --login` |
| Stop and remove autostart | `./uninstall.sh` |
| Offline tests (no accounts needed) | `.venv/bin/python tests/selftest_ops.py`, plus `selftest_browse.py`, `selftest_weixin.py`, `selftest.py` (the ops test needs `pip install aiosmtpd`) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot doesn't reply | Check the log for "✅ 已连接微信" (connected to WeChat) |
| Log says session expired (errcode -14) | Wait — it retries automatically; a QR code is emailed if a re-scan is needed. Or run `bot.py --login`, no restart needed |
| Certificate verification failed | A proxy/VPN may be intercepting HTTPS; route `*.weixin.qq.com` directly |
| "Vault not writable" | The vault is in Documents/Desktop/iCloud; move it to your home folder |
| Photo/file failed to save | Download links expire after 5 minutes; resend |
| Obsidian can't sync while a proxy runs | See [Pitfall 5](guide.en.md#8-pitfalls) |
| (WeCom channel) subscribe failed | Wrong Bot ID/Secret, or the bot isn't in long-connection mode |
| (WeCom channel) repeatedly kicked | One connection per bot — is another machine running this too? |

## WeCom channel (optional)

WeCom (WeChat Work) "Smart Bot" in API long-connection mode is also supported: set `channel = "wecom"` in `config.toml` and fill Bot ID and Secret under `[wecom]`. Note that WeCom smart bots **only work inside the WeCom app**.
