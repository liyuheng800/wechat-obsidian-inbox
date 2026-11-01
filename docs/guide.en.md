# Capture notes from WeChat, keep them at home: building a self-hosted Obsidian inbox on a Mac mini

[简体中文](guide.zh-CN.md) | English · [← Back to project home](../README.en.md)

> Send a line of text, a voice message or a photo in **regular (personal) WeChat**, and a few seconds later it becomes a Markdown note in the Obsidian vault on a Mac mini at home — synced in real time to my laptop. No third-party note service, end-to-end encrypted sync, no public IP, no domain name, no ICP filing.

This write-up covers the whole journey: requirements, the options I evaluated (and why most of them failed), the final architecture, every piece of software involved, step-by-step setup, and the pitfalls I hit along the way.

> **Note for non-Chinese readers:** the capture channel is WeChat, so this project is most useful if WeChat is part of your daily life. The bot's replies and the screenshots are in Chinese; all commands also have English aliases (see [Daily use](#7-daily-use)). The sync part (Obsidian + Self-hosted LiveSync + CouchDB + Tailscale on a Mac mini) works for anyone.

---

## Contents

1. [What it does](#1-what-it-does)
2. [Requirements](#2-requirements)
3. [Architecture](#3-architecture)
4. [Hardware and software](#4-hardware-and-software)
5. [Options evaluated](#5-options-evaluated)
6. [Setup](#6-setup)
7. [Daily use](#7-daily-use)
8. [Pitfalls](#8-pitfalls)
9. [Security and privacy](#9-security-and-privacy)
10. [Known limitations](#10-known-limitations)
11. [Cost](#11-cost)

---

## 1. What it does

**Capture (WeChat on the phone)**
- Send text or voice to the "WeChat ClawBot" chat; the bot replies "✓ saved to inbox". Voice is transcribed by WeChat itself
- Tags route notes automatically: `learned attention today #AI` goes to `01-学习/AI` (Learning/AI); a message starting with `工作：` (“Work:”) goes to the work log
- Photos and files (PDF etc.) are saved as attachments with a note linking to them

**Browse and edit inside WeChat**
- `/ls` lists the vault, reply with a number to open a folder or note; `/search keyword` does full-text search; `/recent` shows recently modified notes
- Once a note is open: append (voice works too), rewrite, find-and-replace, rename, move, delete (to trash)

**Organize (Obsidian on the computer)**
- The Mac mini and MacBook vaults stay in sync within seconds; long-form writing and cleanup happen on the laptop

**Runs unattended**
- A health check every 30 minutes self-heals what it can and emails me about what it can't (QQ Mail notifications pop up inside WeChat)
- A daily report every morning: notes captured yesterday, system status
- Daily backups, kept for 30 days
- If the WeChat session expires, the bot emails me a login QR code and resumes automatically once I scan it

<!-- Screenshot: wechat-capture.png — ClawBot chat: text, voice, a tagged message, and the bot's replies -->
<!-- Screenshot: wechat-browse.png — /ls → reply with a number → note opened in chat -->

<img src="../assets/screenshots/notes-arriving.png" alt="Notes sent from WeChat appear in the Obsidian inbox">

*Messages sent from WeChat show up in the Mac mini's Obsidian inbox within seconds and are pushed to the sync database (↑6, top right).*

---

## 2. Requirements

1. **Capture must be instant**: pull out the phone and type or speak into **regular WeChat** — the app I already have open all day. No separate notes app
2. **I own the data**: plain Markdown files stored at home, independent of any notes company
3. **Editable on multiple devices**, without one device silently overwriting another
4. **Family members may use it later**, so it can't be fragile
5. I had an **idle Mac mini (Apple silicon, 256 GB)** to use as an always-on host

---

## 3. Architecture

```
 ┌──────────────── Phone ────────────────┐
 │ Regular WeChat → "WeChat ClawBot" chat │
 └──────────────────┬────────────────────┘
                    │ Relayed by Tencent's servers
                    │ (the Mac mini polls outbound — no public IP needed)
                    ▼
 ┌────────────────── Mac mini at home (always on) ───────────────────┐
 │ WeChat bot (Python) ── writes ──▶ Obsidian vault (Markdown files)  │
 │     ▲ browse / view / edit             │                          │
 │                               Obsidian + LiveSync plugin           │
 │                                        │                          │
 │                                 CouchDB  ◀── health check / alerts │
 │                                              / backups             │
 └────────────────────────────────────────┬──────────────────────────┘
                                          │ Tailscale private network (HTTPS)
                                          ▼
                 Obsidian + LiveSync on the MacBook (real-time, two-way)
                 Obsidian on the Android phone (optional)
```

**Key design decisions**

- **Why WeChat ClawBot**: an official WeChat plugin released in 2026 that exposes a bot API for *personal* WeChat accounts. Messages are relayed by Tencent; the Mac mini long-polls over HTTPS, so the home machine **never needs to be reachable from the internet** — no public IP, domain, or ICP filing (the government registration China requires for publicly served domains)
- **The bot only creates notes, never edits existing ones**: each message becomes its own file, e.g. `2026-10-27 0915 attention is a weighted sum.md`. The bot never touches a file you're editing, so it can't conflict with sync. (Edit commands like `/append` first verify the note hasn't been changed elsewhere since you opened it, and refuse if it has.)
- **Why LiveSync instead of a cloud-drive sync**: LiveSync syncs in real time at the chunk level and asks you to resolve conflicts instead of silently creating "conflicted copy" files. The database runs on my own Mac mini, and content is end-to-end encrypted
- **Why Tailscale**: lets the MacBook and phone reach the Mac mini securely from any network, again without a public IP, and provides HTTPS certificates automatically

---

## 4. Hardware and software

### Hardware

| Device | Role |
|---|---|
| Mac mini (Apple silicon, 256 GB) | Host: vault, sync database, WeChat bot, health checks — runs 24/7 |
| MacBook | Organizing and writing in Obsidian |
| Android phone | Capture and quick lookups via WeChat; Obsidian is optional |

### Software

| Software | Role | Cost |
|---|---|---|
| [Obsidian](https://obsidian.md) | Notes app; the vault is just a folder of Markdown files | Free for personal use |
| [Self-hosted LiveSync](https://github.com/vrtmrz/obsidian-livesync) (Obsidian community plugin) | Real-time multi-device sync with end-to-end encryption | Free, open source |
| [Apache CouchDB](https://couchdb.apache.org) 3.5 (via Homebrew) | LiveSync's sync database, running on the Mac mini | Free, open source |
| [Tailscale](https://tailscale.com) | Private network between devices; `tailscale serve` provides HTTPS | Free for personal use |
| [Homebrew](https://brew.sh) | macOS package manager, used to install CouchDB | Free |
| WeChat ClawBot plugin | Official bot API for personal WeChat accounts (recent WeChat required; Me → Settings → Plugins) | Free |
| Python 3 + this project | WeChat bot, browse/edit, health checks, backups | — |
| Python libraries | `websockets`, `cryptography` (decrypting WeChat media), `certifi` (CA bundle), `qrcode` + `pypng` (login QR codes) | Free, open source |
| QQ Mail SMTP | Sends alerts and daily reports; with WeChat's QQ Mail notifications they pop up inside WeChat | Free |
| macOS launchd | Starts the bot and health check at login, restarts them if they crash | Built in |
| Clash Verge (optional) | If you run a proxy, Tailscale needs a direct-connection rule — see Pitfalls | — |

**This project** is about 2,600 lines of Python and shell:

| File | Purpose |
|---|---|
| `bot.py` | Main program: config, note storage, tag routing, commands |
| `weixin_channel.py` | WeChat ClawBot channel: QR login, messaging, media decryption, automatic re-login |
| `browse.py` | Browse, search, view and edit notes from the chat |
| `monitor.py` | Health checks, self-healing, alerts, daily report, backups |
| `notify.py` | Sending email via QQ Mail |
| `livesync/setup-livesync.sh` | One-shot CouchDB install/config plus Tailscale HTTPS |
| `install.sh` / `uninstall.sh` | Install/remove the background services |
| `tests/` | Four offline test suites with fake WeChat and SMTP servers — no real accounts needed |

---

## 5. Options evaluated

Probably the most useful section: many options look viable until you hit a hard requirement.

### Capture channel (the hard part: it had to work in personal WeChat)

| Option | Result | Why |
|---|---|---|
| Unofficial personal-WeChat bots (wechaty, itchat…) | ❌ Rejected | Risk of getting your main account banned |
| WeCom (WeChat Work) custom app + WeChat plugin | ❌ Blocked | Apps created after mid-2022 require a fixed "trusted IP"; home broadband IPs change |
| WeCom "WeChat Customer Service" | ❌ Blocked | Same trusted-IP rule, and the callback must be an ICP-filed domain owned by the same entity |
| WeCom "Smart Bot" (API mode, long-lived connection) | ⚠️ Worked technically, dropped | No public IP needed and fully implemented — but **smart bots only work inside the WeCom app, not in personal WeChat**. The channel is still in the code |
| Personal subscription (Official) account | ⚠️ Not recommended | Needs a public callback URL, passive replies only, buried in the subscriptions folder |
| Feishu (Lark) bot | ⚠️ Alternative | Long-lived connection, full-featured, but yet another app |
| Ready-made products (e.g. flomo) | ⚠️ Alternative | No code, but data lives with a third party first; import into Obsidian isn't real-time |
| WeChat Mini Program | ⚠️ Not now | Best UI, but the backend must be an ICP-filed HTTPS domain, the mini program itself must be filed, and you need a cloud server |
| **WeChat ClawBot (official plugin)** | ✅ **Chosen** | Official API, works in personal WeChat, scan-to-bind, no public IP or filing, supports text, voice (auto-transcribed), images and files |

<img src="../assets/screenshots/wecom-api-mode.png" alt="WeCom Smart Bot API mode">

*WeCom Smart Bot's API mode supports a long-lived connection with no domain or IP — technically viable, but the bot only works inside WeCom.*

> Lesson learned: I didn't verify "can a WeCom smart bot be used from personal WeChat?" before building and deploying, and it failed on the single most important requirement. **Validate the hardest constraint first, then build.**

### Multi-device sync

| Option | Result | Why |
|---|---|---|
| iCloud | ❌ | Android phone |
| Jianguoyun (Nutstore) WebDAV + Remotely Save plugin | ⚠️ First version, replaced | Easiest to set up, but data goes through a third party, syncs every few minutes, and conflicts overwrite whole files |
| Syncthing | ⚠️ Alternative | Peer-to-peer, no cloud — but the official Android app is no longer maintained |
| Obsidian Sync (official) | ⚠️ Alternative | Easiest of all, but a monthly subscription |
| **Self-hosted LiveSync + own CouchDB + Tailscale** | ✅ **Chosen** | Data stays on my devices, syncs in seconds, end-to-end encrypted, conflict resolution prompts |

---

## 6. Setup

> The Mac mini is the host. **Order matters: build the primary vault on the Mac mini first, then the bot, then other devices.**
> Replace `<device>.<tailnet>.ts.net` with your own Tailscale address.

### Step 0: Prepare the Mac mini

- **macOS 15 or later** (Homebrew's prebuilt CouchDB bottles require it)
- System Settings → Energy: enable "Prevent automatic sleeping" and "Start up automatically after a power failure"
- System Settings → Users & Groups: enable **automatic login** (if greyed out, turn off FileVault first). Otherwise after a reboot the Mac waits at the login screen and nothing starts
- System Settings → General → Sharing: enable **Screen Sharing** and **Remote Login** so you can manage it headless
- System Settings → General → Software Update: turn off automatic macOS installs
- Run `xcode-select --install`, then install Homebrew per brew.sh
- Put this repository in your home folder, e.g. `~/wechat-obsidian-inbox` (`git clone` or unzip). Not in Documents or Downloads

### Step 1: Tailscale

1. Install Tailscale on the Mac mini and sign in
2. In the admin console's **DNS** page, enable **MagicDNS** and **HTTPS Certificates**
3. Under **Machines**, find the Mac mini → `…` → **Disable key expiry** (otherwise the key expires after 180 days and sync breaks)

### Step 2: CouchDB (sync database)

```bash
cd ~/wechat-obsidian-inbox
./livesync/setup-livesync.sh
```

The script installs CouchDB, generates a random admin password (saved to `~/.kb-livesync/credentials.txt`), writes the config LiveSync needs (single node, CORS, listen on localhost only), enables start at login, creates the databases, and exposes HTTPS via `tailscale serve`. It ends by printing `https://<device>.<tailnet>.ts.net`.

### Step 3: Primary vault in Obsidian on the Mac mini

1. Create a vault in your home folder, e.g. `~/Obsidian/知识库`. ⚠️ Not in Documents, Desktop, Downloads or iCloud — macOS blocks background processes from those locations
2. Install the community plugin **Self-hosted LiveSync** and follow the wizard (screenshots show the plugin's Chinese UI):

   ① First device, no Setup URI yet → **enter server details manually**

   <img src="../assets/screenshots/livesync-01-connect.png" alt="Connection method" width="420">

   ② Enable **end-to-end encryption** with a passphrase (write it down — every device needs the same one) and tick **obfuscate properties** (混淆属性)

   <img src="../assets/screenshots/livesync-02-e2ee.png" alt="End-to-end encryption" width="380">

   ③ Server type: **CouchDB**

   <img src="../assets/screenshots/livesync-03-server-type.png" alt="Server type" width="420">

   ④ URL = the HTTPS address from step 2, username `admin`, password from `credentials.txt`, database `obsidian`. Leave "use internal API" **unticked**

   <img src="../assets/screenshots/livesync-04-couchdb.png" alt="CouchDB settings" width="380">

   ⑤ Click "check server requirements"; when everything passes, continue (if it fails, see Pitfall 5)

   <img src="../assets/screenshots/livesync-06-check-passed.png" alt="All checks passed" width="380">

   ⑥ **Initialise the server from this device** (the server is empty, nothing is lost)

   <img src="../assets/screenshots/livesync-07-rebuild.png" alt="Rebuild and initialise server" width="420">

3. In Sync settings, set sync mode to **LiveSync** and enable **Keep replication active in the background**

   <img src="../assets/screenshots/livesync-sync-mode-right.png" alt="Correct sync settings" width="560">

4. Add Obsidian to Login Items
5. Run "Copy current settings as a new setup URI" from the command palette and keep it for other devices

### Step 4: The WeChat bot

```bash
cd ~/wechat-obsidian-inbox
./install.sh          # first run creates config.toml and stops
open -e config.toml   # check that [vault] path points to your vault
./install.sh          # run again: a QR code appears — scan it with WeChat and confirm
```

<img src="../assets/screenshots/terminal-install.png" alt="install.sh output" width="600">

*`install.sh` validates the config, lists the routing rules, sets up autostart and shows the latest log — "✅ 已连接微信" means the bot is connected to WeChat. It's safe to re-run — do so after any config change*

<!-- Screenshot: wechat-plugin-entry.png — "WeChat ClawBot" in WeChat's Me → Settings → Plugins -->

Your WeChat must show **Me → Settings → Plugins → WeChat ClawBot**. After binding, a "WeChat ClawBot" chat appears — send `/help`.

<img src="../assets/screenshots/obsidian-note.png" alt="A note created from WeChat" width="640">

*A note created from one WeChat message: properties record when it was created, the source, the type and the sender*

> Scan the QR code yourself in the Terminal app; when run by automation the script skips the QR step.

### Step 5: Alert email

1. QQ Mail web → Settings → Account & Security → enable **POP3/SMTP** → generate an **authorization code** (an app password, *not* your QQ password)
2. Fill in the email and code in the `[alert]` section of `config.toml`
3. Run `.venv/bin/python monitor.py --test-email`
4. Enable QQ Mail notifications in WeChat so alerts pop up there

Any SMTP provider works — set `smtp_host` / `smtp_port` in `[alert]`.

<!-- Screenshot: daily-report.png — daily report email received in WeChat -->

### Step 6: Add the MacBook

1. Install Tailscale and sign in with the same account
2. Install Obsidian, create an empty vault, install Self-hosted LiveSync
3. When generating the Setup URI on the Mac mini you'll be asked about expiry. **Time-bound** is safer; you can always generate a new one

   <img src="../assets/screenshots/setup-uri-expiry.png" alt="Setup URI expiry" width="560">

4. In the MacBook's wizard choose **Use Setup URI**, paste it, enter its passphrase, then:

   ① **Join this device**. ⚠️ **Never pick "Initialise or overwrite the remote"** — it would overwrite every note on the server with this empty vault

   <img src="../assets/screenshots/join-this-device.png" alt="Join this device" width="460">

   ② **Overwrite all with remote files** (only overwrites this empty local vault; the server is untouched)

   <img src="../assets/screenshots/overwrite-local.png" alt="Overwrite local with remote" width="460">

   ③ **Keep local files even if not on remote**
5. Set sync mode to **LiveSync** and enable **Keep replication active in the background**

### Step 7: Android phone (optional)

Same as the MacBook. But **Android runs only one VPN at a time**: Tailscale and proxy apps like Clash can't be on together, so switch when you want to sync. Capturing through WeChat ClawBot isn't affected.

---

## 7. Daily use

### Capture

| Send | Result |
|---|---|
| Plain text or voice | Saved to `00-收件箱` (inbox), one note per message |
| `learned attention today #AI` | Saved to `01-学习/AI` (full-width `＃`, any case, and a tag followed directly by Chinese all work) |
| Message starting with `工作：` (Work:) / `踩坑：` (Pitfall:) | Routed to the work log / pitfall notes, tag added automatically |
| Photos, PDFs, other files | Saved as attachments, with a note linking to them |
| Long-press a message → Quote → add a thought | The note keeps your thought and the quoted text |

Folders, tags and prefixes are all configurable in `config.toml` (`[[routes]]`).

### Commands (Chinese / English)

| Command | Action |
|---|---|
| `/今天` · `/today` | What did I capture today |
| `/撤销` · `/undo` | Remove the note just saved (not if you've edited it since) |
| `/目录` · `/ls`, `/上级` · `/up` | Browse the vault; reply with a number to open |
| `/搜 keyword` · `/search keyword`, `/最近` · `/recent` | Find notes |
| `/下一页` · `/next`, `/上一页` · `/prev` | Page through lists and long notes |
| `/追加` · `/append`, `/改写` · `/rewrite`, `/替换 old=>new` · `/replace old=>new` | Edit the open note |
| `/重命名` · `/rename`, `/移动` · `/move`, `/删除` · `/delete` | Organize |
| `/帮助` · `/help`, `/help browse` | Help (replies are in Chinese) |

### Maintenance

| Task | Command (run in `~/wechat-obsidian-inbox`) |
|---|---|
| Bot log | `tail -f ~/Library/Logs/kb-wecom-bot.log` |
| Health check now (read-only) | `.venv/bin/python monitor.py --check` |
| Back up now | `.venv/bin/python monitor.py --backup` |
| Re-bind WeChat | `.venv/bin/python bot.py --login` (the running bot picks up the new credentials, no restart) |
| Offline tests | `.venv/bin/python tests/selftest_ops.py` etc. |

---

## 8. Pitfalls

The most time-consuming part. All are fixed in the code; documented here for reference.

**1. Python can't connect to WeCom: "python-socks" missing**
- Cause: a proxy app was running, and newer `websockets` **picks up the system proxy automatically**, trying to go through SOCKS
- Fix: WeChat/WeCom are domestic services, so the code **bypasses the system proxy by default**

**2. `CERTIFICATE_VERIFY_FAILED`**
- Cause: some Python builds on macOS (e.g. the python.org installer) ship without system root certificates
- Fix: use the `certifi` CA bundle. **Never "fix" this by disabling certificate verification**

**3. perl crashes in the install script: `Could not change LC_CTYPE locale to C.UTF-8`**
- Cause: the environment set `C.UTF-8`, which doesn't exist on macOS
- Fix: edit config files with Python instead, and pin a locale that always exists

**4. CouchDB has no admin and keeps restarting**
- Cause: Homebrew's `/opt/homebrew/etc/local.ini` is a **symlink**. Python's `os.replace()` replaced the link itself with a regular file, so the config went into a copy CouchDB never reads
- Fix: resolve the real path with `realpath` before editing, and after startup **query CouchDB for its live config** to confirm it took effect

**5. Obsidian can't reach `*.ts.net`**
- Cause: Obsidian uses the system proxy, and the proxy can't resolve Tailscale's private hostnames
- Fix: in Clash Verge, add `*.ts.net` and `100.64.0.0/10` to the system-proxy bypass list, plus prepend rules `DOMAIN-SUFFIX,ts.net,DIRECT` and `IP-CIDR,100.64.0.0/10,DIRECT`. If the bypass field is greyed out, turn off "use default bypass" first

<img src="../assets/screenshots/livesync-05-check-failed.png" alt="Server check failed" width="380">

*When the proxy intercepts, LiveSync only says "configuration check failed".*

<!-- Screenshot: clash-verge-bypass.png — Clash Verge system proxy bypass settings -->

**6. LiveSync says connected but nothing syncs**
- Symptom: the status bar shows a square icon and `↑0 ↓0`
- Cause: on a new device the sync mode defaulted to "on events" with every trigger switched off; and without background replication, sync pauses whenever Obsidian isn't the front window
- Fix: set sync mode to **LiveSync** and enable **Keep replication active in the background**. Healthy status shows `zzZ` (idle) and ⚡ while transferring

<img src="../assets/screenshots/livesync-sync-mode-wrong.png" alt="Wrong sync mode: on events" width="560">

*The default on a new device: "on events" with all triggers off — effectively no sync at all.*

**7. Messages tagged `#AI` weren't routed**
- Cause: the phone keyboard produced a **full-width `＃`**
- Fix: normalize full-width characters, match tags case-insensitively, and accept a tag followed directly by Chinese text (`#AI测试`)

<img src="../assets/screenshots/fullwidth-hash.png" alt="Full-width hash not routed" width="560">

*The two messages in the red box used a full-width "＃" and stayed in the inbox; the tag didn't render as an Obsidian tag either.*

**8. Empty folders don't appear on other devices**
- Cause: LiveSync syncs files, **not empty folders**
- Expected behaviour: a folder appears as soon as it contains a note

**9. Scanning the self-test's QR code said "invalid"**
- Cause: the offline test uses a fake QR code from a mock server
- Fix: the self-test no longer displays it; real logins print the link so you can tell them apart

**10. Tailscale and a proxy can't run together on Android**
- Cause: Android allows only one VPN at a time
- Workaround: switch when you need to sync, or use an app that does both (e.g. recent sing-box versions)

**11. Nothing came back after a reboot**
- Cause: the bot, database and Obsidian all start **after user login**
- Fix: enable automatic login (requires FileVault off), and set the screen to lock immediately — locking doesn't stop background services

**12. Sync is not backup**
- LiveSync faithfully propagates accidental deletions or corruption to every device. Hence the daily archive backups; ideally add Time Machine to an external drive too

**13. A long video showed up only on the Mac mini**
- Symptom: a 20-minute video sent from WeChat was on the Mac mini; the MacBook only had the note, and clicking the link created an empty `xxx.mp4.md`
- Cause: LiveSync **skips files over 50 MB** by default (the Maximum file size setting) — silently, in both directions
- Fix: on every device enable Settings → General → Enable advanced features, then raise 🚦 Selector → Normal Files → Maximum file size (e.g. 1024). Big videos are then stored on every device plus the database, so keep the limit if space is tight

---

## 9. Security and privacy

- **Where data lives**: note files on the Mac mini and your own devices; the sync database on the Mac mini, end-to-end encrypted, with property obfuscation so the server can't even see file names
- **Network exposure**: CouchDB listens on localhost only; other devices reach it over Tailscale HTTPS. Nothing is exposed to the public internet
- **WeChat bot**: only accepts messages from the WeChat account that scanned the QR code; browsing is confined to the vault and hides dot-folders; edits check that the note wasn't changed elsewhere; deletions go to trash
- **Secrets**: `config.toml` (mail authorization code), `weixin_token.json` (WeChat credentials) and `~/.kb-livesync/credentials.txt` (database password) are owner-readable only. **Never commit them** — the repo's `.gitignore` already excludes them
- **The WeChat ClawBot API** is provided by Tencent, which reserves the right to rate-limit, block or discontinue it. The protocol implementation follows the public source of Tencent's official plugin `@tencent-weixin/openclaw-weixin`. This project is not affiliated with Tencent

---

## 10. Known limitations

- WeChat ClawBot can **only reply, not push**, so alerts go by email
- WeChat has no clickable buttons; browsing uses numbered lists, and images/attachments can't be previewed in chat
- Obsidian must stay open on the Mac mini; it's what pushes the bot's new notes to other devices
- **Single user** for now. Multiple users need per-person folders and a ClawBot binding each — planned
- Backups live on the same disk as the vault, so they don't protect against disk failure on their own; an external drive or the MacBook's synced copy covers that

---

## 11. Cost

| Item | Cost |
|---|---|
| Hardware | An idle Mac mini — nothing extra. Idle power draw is very low |
| Software and services | Obsidian (free for personal use), LiveSync / CouchDB (open source), Tailscale (free for personal use), WeChat ClawBot (free), QQ Mail (free) |
| Time | About 2 days, mostly evaluating options and fighting pitfalls. Following this guide, **an afternoon** should do |

---

## What's next

- Family use: per-person folders plus a shared family area
- Resolve the Tailscale/proxy conflict on Android to use Obsidian on the phone directly
- More tags and auto-organizing rules

---

*Built with AI assistance: the design and code were written with Claude, and installation on the Mac mini was carried out by a local AI agent (Hermes) that paused for human confirmation at every critical step.*
