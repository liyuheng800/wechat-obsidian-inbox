#!/usr/bin/env python3
"""浏览 / 查看 / 修改功能的离线自测。运行：.venv/bin/python tests/selftest_browse.py"""
import sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import browse  # noqa: E402

v = Path(tempfile.mkdtemp()) / "知识库"
for d in ["00-收件箱/附件", "01-学习/AI", "01-学习/英语", "02-工作/工作日志", ".obsidian", ".trash"]:
    (v / d).mkdir(parents=True, exist_ok=True)
FM = "---\ncreated: 2026-10-28T10:00:00\nsource: 微信\n---\n\n"
(v / "01-学习/AI/注意力机制.md").write_text(FM + "注意力本质是加权求和。周三复习。\n", "utf-8")
(v / "01-学习/英语/practice 用法.md").write_text(FM + "practice doing sth\n", "utf-8")
(v / "00-收件箱/随手记.md").write_text(FM + "周三开会\n![[00-收件箱/附件/a.jpg]]\n", "utf-8")
(v / "00-收件箱/附件/a.jpg").write_bytes(b"\xff\xd8\xff")
(v / "00-收件箱/长文.md").write_text(FM + ("长文内容。" * 700), "utf-8")
(v / ".obsidian/app.json").write_text("{}")
time.sleep(0.01)
(v / "02-工作/工作日志/周报.md").write_text(FM + "本周完成接口联调\n", "utf-8")

b = browse.Browser(v)
U = "me"
say = lambda t, voice=False: b.handle(U, t, voice)
show = lambda t, r: print(f"\n>>> {t}\n{r}")

r = say("/目录"); show("/目录", r)
assert "📁 00-收件箱" in r and "📁 01-学习（2）" in r and ".obsidian" not in r and ".trash" not in r
r = say("2"); show("2", r)                       # 进入 01-学习
assert "01-学习" in r and "📁 AI（1）" in r
r = say("1"); show("1", r)                       # 进入 AI
r = say("1"); show("1", r)                       # 打开笔记
assert "加权求和" in r and "created" not in r and "/追加" in r

r = say("/追加 补充：缩放点积"); show("/追加 …", r)
assert "已追加" in r and "缩放点积" in (v / "01-学习/AI/注意力机制.md").read_text()
r = say("/追加"); assert "请发送" in r
r = say("这是语音追加的内容", voice=True); show("[语音]", r)
assert "语音追加" in (v / "01-学习/AI/注意力机制.md").read_text()
assert say("普通语音", voice=True) is None, "没有等待时语音不应被拦截"

r = say("/替换 周三=>周四"); show("/替换", r)
assert "1 处" in r and "周四复习" in (v / "01-学习/AI/注意力机制.md").read_text()
assert "没有找到" in say("/替换 不存在=>x")

# 被其他设备修改后，拒绝修改
(v / "01-学习/AI/注意力机制.md").write_text((v / "01-学习/AI/注意力机制.md").read_text() + "电脑上改的\n", "utf-8")
r = say("/追加 不该写入"); show("（电脑改动后）/追加", r)
assert "被其他设备修改过" in r and "不该写入" not in (v / "01-学习/AI/注意力机制.md").read_text()
r = say("/看"); assert "电脑上改的" in r
assert "已追加" in say("/追加 重新打开后可以写")

r = say("/改写"); assert "新正文" in r
r = say("全新的正文\n第二行"); show("[改写内容]", r)
txt = (v / "01-学习/AI/注意力机制.md").read_text()
assert txt.startswith("---\ncreated:") and "全新的正文\n第二行" in txt and "加权求和" not in txt

r = say("/重命名 Attention 笔记"); show("/重命名", r)
assert (v / "01-学习/AI/Attention 笔记.md").exists()
r = say("/移动"); show("/移动", r)
n = [l for l in r.splitlines() if l.endswith("📁 01-学习/英语")][0].split(".")[0]
r = say(n); show(n, r)
assert (v / "01-学习/英语/Attention 笔记.md").exists() and not (v / "01-学习/AI/Attention 笔记.md").exists()

r = say("/删除"); assert "确认" in r
assert say("算了") == "已取消删除。" and (v / "01-学习/英语/Attention 笔记.md").exists()
say("/删除"); r = say("确认"); show("确认删除", r)
assert not (v / "01-学习/英语/Attention 笔记.md").exists() and (v / ".trash/Attention 笔记.md").exists()

r = say("/搜 周三"); show("/搜 周三", r)
assert "随手记" in r and "Attention" not in r
r = say("1"); show("1", r)
assert "[图片：a.jpg]" in r

r = say("/最近"); show("/最近", r)
assert r.splitlines()[1].startswith("1. 📄 随手记") or "周报" in r

say("/目录"); say("1"); r = say("3")             # 收件箱 → 长文（列表里按时间排序，找出长文的编号）
lst = b.sessions[U]
idx = [i for i, it in enumerate(lst["items"]) if it["rel"].endswith("长文.md")]
if idx:
    say("/上级"); r = say(str(idx[0] + 1))
assert "第 1/" in r and "/下一页" in r
r2 = say("/下一页"); assert "第 2/" in r2

say("/目录"); say("1"); r = say("1")             # 附件文件夹
r = say("1"); show("打开图片", r); assert "附件" in r

assert "没有第 99 项" in (say("/目录") and say("99"))
assert say("随便一句话") is None, "普通消息应交给记录流程"
assert say("/今天") is None and say("/撤销") is None, "其他指令交给原有处理"
assert "查看和修改" in say("/帮助 浏览")

# 越界保护
try:
    b._abs("../../etc"); raise SystemExit("越界检查失效")
except ValueError:
    pass
print("\n✅ 浏览/修改功能测试全部通过")
