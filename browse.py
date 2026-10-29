"""
在聊天里浏览、查看、修改知识库。

微信 ClawBot 只能收发普通消息，没有可点击的按钮，所以用“编号列表 + 回复数字”来模拟点击：
  /目录         列出知识库根目录，回复数字进入文件夹或打开笔记
  /搜 关键词     按文件名和内容搜索
  /最近         最近修改的笔记
打开一篇笔记后：
  /追加 内容     在末尾追加（不写内容则下一条消息作为追加内容，可以用语音）
  /改写         下一条消息替换正文（保留笔记属性）
  /替换 旧=>新   把正文里的“旧”全部换成“新”
  /重命名 新名称  /移动  /删除（放进回收站，可恢复）

安全措施：
- 只能访问知识库文件夹内的内容，隐藏文件夹（.obsidian、.trash 等）不显示
- 打开笔记时记下内容指纹；修改前再核对一次，如果期间被其他设备改过，拒绝修改，避免覆盖
- 删除 = 移到知识库里的 .trash 回收站（Obsidian 的回收站位置），不真正删除
"""
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path

SESSION_TTL = 15 * 60      # 浏览状态 15 分钟无操作后失效
PENDING_TTL = 10 * 60      # 等待输入（追加/改写内容、确认删除）10 分钟内有效
LIST_PAGE = 20             # 列表每页条数
NOTE_PAGE = 1500           # 笔记每页字数
FRONTMATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
NOTE_TIPS = ("操作：/追加 内容　/改写　/替换 旧=>新\n"
             "　　　/重命名 新名称　/移动　/删除\n"
             "返回：/上级　/目录")

BROWSE_HELP = """📂 查看和修改笔记

【找笔记】
/目录　列出知识库，回复数字进入文件夹或打开笔记
/上级　回到上一层文件夹
/搜 关键词　按标题和内容搜索
/最近　最近修改的 20 篇
/下一页 /上一页　翻页（列表和长笔记都适用）

【打开笔记后】
/追加 内容　在末尾追加；只发 /追加 的话，下一条消息（文字或语音）就是追加内容
/改写　下一条消息替换整篇正文（笔记属性保留）
/替换 旧文字=>新文字　替换正文里的指定文字
/重命名 新名称
/移动　列出文件夹，回复数字移过去
/删除　回复“确认”后放进回收站（可在 Obsidian 里恢复）
/取消　放弃正在进行的操作

说明：
· 如果笔记在你打开后被电脑改过，修改会被拒绝，发 /看 重新打开即可
· 图片、文件请在 Obsidian 里查看"""


def _fingerprint(data: str) -> str:
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def _clean_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]\n\r]', " ", name)
    return re.sub(r"\s+", " ", name).strip().strip(".")[:80]


class Browser:
    def __init__(self, vault_root: Path):
        self.root = Path(vault_root).expanduser()
        self.sessions = {}

    # ───────── 基础 ─────────
    def _session(self, user):
        s = self.sessions.get(user)
        now = time.time()
        if not s or now - s["ts"] > SESSION_TTL:
            s = {"cwd": "", "items": [], "kind": None, "page": 0, "title": "",
                 "note": None, "pending": None, "ts": now}
            self.sessions[user] = s
        s["ts"] = now
        if s["pending"] and now - s["pending"]["ts"] > PENDING_TTL:
            s["pending"] = None
        return s

    def _abs(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        root = self.root.resolve()
        if p != root and root not in p.parents:
            raise ValueError("路径超出知识库范围")
        return p

    def _rel(self, p: Path) -> str:
        return p.resolve().relative_to(self.root.resolve()).as_posix()

    @staticmethod
    def _visible(name: str) -> bool:
        return not name.startswith(".")

    def _all_md(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if self._visible(d)]
            for f in filenames:
                if f.endswith(".md") and self._visible(f):
                    yield Path(dirpath) / f

    # ───────── 列表 ─────────
    def _set_list(self, s, items, title, kind="browse"):
        s.update(items=items, title=title, kind=kind, page=0)
        return self._render_list(s)

    def _render_list(self, s):
        items, page = s["items"], s["page"]
        if not items:
            return f"{s['title']}\n（空）\n\n/上级 返回　/目录 回到根目录"
        pages = (len(items) - 1) // LIST_PAGE + 1
        page = max(0, min(page, pages - 1))
        s["page"] = page
        start = page * LIST_PAGE
        lines = [s["title"]]
        for i, it in enumerate(items[start:start + LIST_PAGE], start + 1):
            lines.append(f"{i}. {it['label']}")
        foot = "回复数字" + ("移动到该文件夹" if s["kind"] == "move" else "打开")
        if pages > 1:
            foot += f"　第 {page + 1}/{pages} 页，/下一页 /上一页"
        if s["kind"] == "move":
            foot += "\n/取消 放弃移动"
        elif s["kind"] == "browse":
            foot += "\n/上级 返回　/搜 关键词"
        return "\n".join(lines + ["", foot])

    def list_dir(self, s, rel=""):
        d = self._abs(rel)
        if not d.is_dir():
            return f"找不到文件夹：{rel or '根目录'}"
        dirs, files = [], []
        for e in os.scandir(d):
            if not self._visible(e.name):
                continue
            if e.is_dir():
                n = sum(1 for x in os.scandir(e.path) if self._visible(x.name))
                dirs.append({"type": "dir", "rel": self._rel(Path(e.path)), "label": f"📁 {e.name}（{n}）"})
            elif e.is_file():
                icon = "📄" if e.name.endswith(".md") else ("🖼" if re.search(r"\.(png|jpe?g|gif|webp|heic)$", e.name, re.I) else "📎")
                name = e.name[:-3] if e.name.endswith(".md") else e.name
                files.append((e.stat().st_mtime, {"type": "file", "rel": self._rel(Path(e.path)),
                                                  "label": f"{icon} {name}"}))
        dirs.sort(key=lambda x: x["label"])
        files.sort(key=lambda x: -x[0])  # 最新的在前
        s["cwd"] = rel
        title = "📂 知识库" + (f" / {rel}" if rel else "")
        return self._set_list(s, dirs + [f for _, f in files], title)

    def search(self, s, kw):
        kw = kw.strip()
        if not kw:
            return "用法：/搜 关键词"
        hits = []
        low = kw.lower()
        for p in self._all_md():
            try:
                body = p.read_text("utf-8")
            except Exception:
                continue
            if low in p.stem.lower() or low in body.lower():
                hits.append((p.stat().st_mtime, p))
        hits.sort(key=lambda x: -x[0])
        items = [{"type": "file", "rel": self._rel(p), "label": f"📄 {p.stem}　· {self._rel(p.parent) if p.parent != self.root else '根目录'}"}
                 for _, p in hits[:60]]
        more = "（只显示最近的 60 条）" if len(hits) > 60 else ""
        return self._set_list(s, items, f"🔍 “{kw}” 找到 {len(hits)} 篇{more}", kind="search")

    def recent(self, s):
        files = sorted(self._all_md(), key=lambda p: -p.stat().st_mtime)[:LIST_PAGE]
        items = [{"type": "file", "rel": self._rel(p), "label": f"📄 {p.stem}"} for p in files]
        return self._set_list(s, items, "🕘 最近修改的笔记", kind="recent")

    # ───────── 查看 ─────────
    def open_note(self, s, rel, page=0):
        p = self._abs(rel)
        if not p.is_file():
            s["note"] = None
            return "这篇笔记已经不存在了（可能被移动或删除）。发 /目录 重新查看。"
        if not p.name.endswith(".md"):
            size = p.stat().st_size
            return f"📎 {p.name}（{size / 1024:.0f} KB）\n这是附件，请在 Obsidian 里查看。"
        raw = p.read_text("utf-8")
        s["note"] = {"rel": self._rel(p), "fp": _fingerprint(raw)}
        s["cwd"] = self._rel(p.parent) if p.parent.resolve() != self.root.resolve() else ""
        return self._render_note(s, raw, page)

    def _render_note(self, s, raw, page):
        rel = s["note"]["rel"]
        body = FRONTMATTER.sub("", raw).strip()
        body = re.sub(r"!\[\[([^\]|]+)(\|[^\]]*)?\]\]", lambda m: f"[图片：{Path(m.group(1)).name}]", body)
        body = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"[\2]", body)
        pages = max(1, (len(body) - 1) // NOTE_PAGE + 1)
        page = max(0, min(page, pages - 1))
        s["note"]["page"] = page
        s["kind"] = "note"
        chunk = body[page * NOTE_PAGE:(page + 1) * NOTE_PAGE] or "（正文为空）"
        mtime = datetime.fromtimestamp(self._abs(rel).stat().st_mtime).strftime("%m-%d %H:%M")
        folder = Path(rel).parent.as_posix()
        head = f"📄 {Path(rel).stem}\n📁 {'根目录' if folder == '.' else folder}　🕘 {mtime}"
        if pages > 1:
            head += f"　第 {page + 1}/{pages} 页"
        foot = NOTE_TIPS if page == pages - 1 else "/下一页 继续阅读"
        return f"{head}\n────────\n{chunk}\n────────\n{foot}"

    # ───────── 修改 ─────────
    def _current(self, s):
        """返回 (路径, 当前内容)；笔记不存在或被外部改动时返回 (None, 提示)。"""
        n = s.get("note")
        if not n:
            return None, "请先打开一篇笔记：发 /目录、/搜 关键词 或 /最近"
        p = self._abs(n["rel"])
        if not p.is_file():
            s["note"] = None
            return None, "这篇笔记已经不存在了（可能在电脑上被移动或删除）。"
        raw = p.read_text("utf-8")
        if _fingerprint(raw) != n["fp"]:
            return None, "⚠️ 这篇笔记在你打开后被其他设备修改过，为避免覆盖，本次没有改动。\n发 /看 重新打开最新版本后再操作。"
        return p, raw

    def _write(self, s, p: Path, raw: str):
        tmp = p.with_name(f".{p.name}.tmp")
        tmp.write_text(raw, "utf-8")
        tmp.replace(p)
        s["note"]["fp"] = _fingerprint(raw)

    def append(self, s, content):
        p, raw = self._current(s)
        if p is None:
            return raw
        content = content.strip()
        if not content:
            return "追加内容是空的，没有改动。"
        new = raw.rstrip("\n") + "\n\n" + content + "\n"
        self._write(s, p, new)
        preview = content if len(content) <= 60 else content[:60] + "…"
        return f"✓ 已追加到《{p.stem}》末尾：\n「{preview}」\n发 /看 查看全文"

    def rewrite(self, s, content):
        p, raw = self._current(s)
        if p is None:
            return raw
        content = content.strip()
        if not content:
            return "新内容是空的，没有改动。"
        m = FRONTMATTER.match(raw)
        fm = m.group(0) if m else ""
        self._write(s, p, fm + ("\n" if fm else "") + content + "\n")
        return f"✓ 已改写《{p.stem}》的正文（笔记属性保留）\n发 /看 查看"

    def replace(self, s, arg):
        parts = re.split(r"\s*(?:=>|＝>|->|→)\s*", arg, maxsplit=1)
        if len(parts) != 2 or not parts[0]:
            return "用法：/替换 旧文字=>新文字\n例：/替换 周三=>周四"
        old, new = parts
        p, raw = self._current(s)
        if p is None:
            return raw
        m = FRONTMATTER.match(raw)
        fm, body = (m.group(0), raw[m.end():]) if m else ("", raw)
        n = body.count(old)
        if n == 0:
            return f"正文里没有找到“{old}”，没有改动。"
        self._write(s, p, fm + body.replace(old, new))
        return f"✓ 已把 {n} 处“{old}”替换为“{new}”"

    def rename(self, s, name):
        name = _clean_name(name)
        if not name:
            return "用法：/重命名 新名称"
        p, raw = self._current(s)
        if p is None:
            return raw
        target = p.with_name(name + ".md")
        if target.exists():
            return f"同一文件夹里已经有《{name}》了，换个名字吧。"
        p.rename(target)
        s["note"]["rel"] = self._rel(target)
        return f"✓ 已重命名为《{name}》"

    def folders(self, s):
        p, raw = self._current(s)
        if p is None:
            return raw
        rels = []
        for dirpath, dirnames, _ in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if self._visible(d)]
            rels += [self._rel(Path(dirpath) / d) for d in dirnames]
        items = [{"type": "dir", "rel": "", "label": "📁 （根目录）"}]
        items += [{"type": "dir", "rel": r, "label": f"📁 {r}"} for r in sorted(rels)]
        return self._set_list(s, items, f"把《{p.stem}》移动到：", kind="move")

    def move(self, s, rel_dir):
        p, raw = self._current(s)
        if p is None:
            return raw
        d = self._abs(rel_dir)
        target = d / p.name
        if target.resolve() == p.resolve():
            return "笔记已经在这个文件夹里了。"
        if target.exists():
            return f"目标文件夹里已经有同名笔记《{p.stem}》，没有移动。"
        p.rename(target)
        s["note"]["rel"] = self._rel(target)
        s["kind"] = "note"
        return f"✓ 已移动到 {rel_dir or '根目录'}"

    def delete(self, s):
        p, raw = self._current(s)
        if p is None:
            return raw
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        target = trash / p.name
        i = 2
        while target.exists():
            target = trash / f"{p.stem} ({i}).md"
            i += 1
        p.rename(target)
        s["note"] = None
        return f"🗑 已把《{p.stem}》放进回收站\n需要恢复时，在 Mac mini 的知识库文件夹里找 .trash"

    # ───────── 入口 ─────────
    def handle(self, user: str, text: str, voice: bool = False):
        """处理浏览/编辑相关的消息。返回回复文字；不相关时返回 None（按普通笔记保存）。"""
        s = self._session(user)
        t = text.strip().replace("／", "/")
        pend = s["pending"]

        # 等待输入的内容（追加/改写），文字和语音都可以
        if pend and pend["action"] in ("append", "rewrite") and not t.startswith("/"):
            s["pending"] = None
            return self.append(s, text) if pend["action"] == "append" else self.rewrite(s, text)
        if pend and pend["action"] == "delete" and not t.startswith("/"):
            s["pending"] = None
            if t in ("确认", "确认删除", "是", "确定", "y", "yes"):
                return self.delete(s)
            return "已取消删除。"
        if voice:
            return None

        # 回复数字 = 选择列表里的一项
        if re.fullmatch(r"\d{1,4}", t) and s["items"] and s["kind"] in ("browse", "search", "recent", "move"):
            i = int(t) - 1
            if not 0 <= i < len(s["items"]):
                return f"没有第 {t} 项，请回复 1～{len(s['items'])} 之间的数字。"
            it = s["items"][i]
            if s["kind"] == "move":
                return self.move(s, it["rel"])
            if it["type"] == "dir":
                return self.list_dir(s, it["rel"])
            return self.open_note(s, it["rel"])

        if not t.startswith("/"):
            return None
        cmd, _, arg = t[1:].strip().partition(" ")
        arg = arg.strip()
        if cmd in ("目录", "文件", "ls", "tree"):
            return self.list_dir(s, "")
        if cmd in ("上级", "返回", "..", "up"):
            cwd = s["cwd"]
            if s["kind"] == "note" and s["note"]:
                return self.list_dir(s, cwd)
            parent = str(Path(cwd).parent) if cwd else ""
            return self.list_dir(s, "" if parent == "." else parent)
        if cmd in ("搜", "搜索", "找", "search"):
            return self.search(s, arg)
        if cmd in ("最近", "recent"):
            return self.recent(s)
        if cmd in ("下一页", "下页", "next"):
            return self._turn(s, +1)
        if cmd in ("上一页", "上页", "prev"):
            return self._turn(s, -1)
        if cmd in ("看", "查看", "打开", "view", "open", "cat"):
            if not s["note"]:
                return "还没有打开的笔记。发 /目录、/搜 关键词 或 /最近"
            return self.open_note(s, s["note"]["rel"])
        if cmd in ("追加", "append"):
            if arg:
                return self.append(s, arg)
            p, msg = self._current(s)
            if p is None:
                return msg
            s["pending"] = {"action": "append", "ts": time.time()}
            return f"请发送要追加到《{p.stem}》末尾的内容（文字或语音都可以）\n/取消 放弃"
        if cmd in ("改写", "rewrite"):
            p, msg = self._current(s)
            if p is None:
                return msg
            if arg:
                return self.rewrite(s, arg)
            s["pending"] = {"action": "rewrite", "ts": time.time()}
            return (f"请发送《{p.stem}》的新正文，会替换原来的全部正文（笔记属性保留）\n"
                    "提示：可以先长按上面的笔记内容复制，改好后再发回来\n/取消 放弃")
        if cmd in ("替换", "replace"):
            return self.replace(s, arg)
        if cmd in ("重命名", "改名", "rename"):
            return self.rename(s, arg)
        if cmd in ("移动", "move", "mv"):
            return self.folders(s)
        if cmd in ("删除", "delete", "rm"):
            p, msg = self._current(s)
            if p is None:
                return msg
            s["pending"] = {"action": "delete", "ts": time.time()}
            return f"确定要删除《{p.stem}》吗？\n回复“确认”放进回收站，回复其他内容取消"
        if cmd in ("取消", "cancel"):
            had = s["pending"] or s["kind"] == "move"
            s["pending"] = None
            if s["kind"] == "move":
                s["kind"] = "note"
            return "已取消。" if had else "当前没有进行中的操作。"
        if cmd in ("退出", "exit"):
            self.sessions.pop(user, None)
            return "已退出浏览。"
        if cmd in ("帮助", "help") and arg in ("浏览", "查看", "修改", "编辑", "browse"):
            return BROWSE_HELP
        return None

    def _turn(self, s, delta):
        if s["kind"] == "note" and s["note"]:
            p = self._abs(s["note"]["rel"])
            if not p.is_file():
                return "这篇笔记已经不存在了。"
            return self._render_note(s, p.read_text("utf-8"), s["note"].get("page", 0) + delta)
        if s["items"]:
            s["page"] += delta
            return self._render_list(s)
        return "没有可以翻页的内容。"
