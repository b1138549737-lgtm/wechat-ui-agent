"""本地知识库（T340）离线单测：分段 / 检索排序 / 三种指令出口 / 模型工具。

跑法：python tests/test_knowledge.py
"""
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import commands                           # noqa: E402
from wxbot import knowledge                          # noqa: E402
from wxbot.config import Config                      # noqa: E402
from wxbot.store import Store                        # noqa: E402
from wxbot.tools import ToolBox                      # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def make_kb(tmp: pathlib.Path) -> Config:
    d = tmp / "knowledge"
    d.mkdir(parents=True, exist_ok=True)
    (d / "羽毛球.md").write_text(
        "# 羽毛球\n\n每周五晚上七点，东门那家球馆，场地已经订好（3 号场）。\n\n"
        "球拍在门口柜子里，记得带两桶球。\n", encoding="utf-8")
    (d / "快递.md").write_text(
        "# 快递\n\n菜鸟驿站晚上九点关门，取件码发到手机短信里。\n", encoding="utf-8")
    (d / "忽略我.json").write_text('{"不该被读": true}', encoding="utf-8")
    return Config({"knowledge": {"enabled": True, "dir": str(d), "max_snippets": 3}})


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = make_kb(tmp)

    print("[分段 / 索引 / 只认 md、txt]")
    chunks = knowledge.load_chunks(cfg)
    # 标题行（"# 羽毛球"）本身太短会被丢掉，只留正文段落，所以是 3 块而不是 4-5 块
    check_true("切出了多个段落", len(chunks) >= 3)
    check("太短的行不成块", all(len(c["text"]) >= 8 for c in chunks), True)
    check("只索引两个文件", sorted({c["file"] for c in chunks}), ["快递.md", "羽毛球.md"])
    summary = knowledge.index_summary(cfg)
    check("索引摘要文件数", len(summary), 2)
    check_true("摘要里有块数", all(r["chunks"] >= 1 for r in summary))

    print("[检索：按相关度挑对文件]")
    hits = knowledge.retrieve(cfg, "周五打球是几点？")
    check_true("命中了羽毛球", hits and hits[0]["file"] == "羽毛球.md")
    check_true("段落里带关键信息", "晚上七点" in hits[0]["text"])
    hits2 = knowledge.retrieve(cfg, "菜鸟驿站几点关门")
    check_true("命中了快递", hits2 and hits2[0]["file"] == "快递.md")
    check("毫不相关的词 → 空（不瞎给）", knowledge.retrieve(cfg, "量子力学 拓扑绝缘体"), [])
    check("空问题 → 空", knowledge.retrieve(cfg, "   "), [])
    # limit=1 时也可能带 1 个"相邻段"（命中段 + 紧随其后的一段），所以是 1~2
    one = knowledge.retrieve(cfg, "羽毛球 球拍 球馆", limit=1)
    check_true("max_snippets 生效（最多 1 命中 + 1 相邻）", 1 <= len(one) <= 2)
    check_true("相邻段仍是同一个文件", all(h["file"] == one[0]["file"] for h in one))

    print("[相邻扩展：命中小标题时，答案在下一段也要带上]")
    d = cfg.path_of("knowledge.dir")
    (d / "清单.md").write_text(
        "# 周末清单\n\n下面是要带的东西：\n\n- 球拍两把\n- 羽毛球两桶\n- 换洗衣服\n",
        encoding="utf-8")
    hits4 = knowledge.retrieve(cfg, "周末清单要带什么", limit=1)
    joined = " ".join(h["text"] for h in hits4)
    check_true("答案段被一起带上", "球拍两把" in joined or "羽毛球两桶" in joined)

    print("[缓存：目录没变就用缓存，改了要重读]")
    sig_before = knowledge._CACHE[str(cfg.path_of("knowledge.dir"))][1]
    _ = knowledge.load_chunks(cfg)
    check("没改 → 签名一致", knowledge._CACHE[str(cfg.path_of("knowledge.dir"))][1], sig_before)
    p = cfg.path_of("knowledge.dir") / "新增.md"
    p.write_text("# 新增\n\n这是后来加的资料：备份脚本在 D 盘 scripts 目录。\n", encoding="utf-8")
    hits3 = knowledge.retrieve(cfg, "备份脚本在哪")
    check_true("新增文件立刻能被搜到", hits3 and hits3[0]["file"] == "新增.md")

    print("[指令出口：/知识 列表、/问 接上回答、关掉开关要给说明]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    ctx = {"cfg": cfg, "store": s, "rt": None, "username": "filehelper",
           "speaker": "", "speaker_name": "", "contact_name": "文件传输助手",
           "owner": True, "effective": {},
           "knowledge_summary": lambda: knowledge.index_summary(cfg),
           "knowledge_answer": lambda q: "（模型回答）" + q,
           "knowledge_dir": str(cfg.path_of("knowledge.dir"))}
    _h, out = commands.dispatch("/知识", ctx)
    check_true("列出文件", "羽毛球.md" in out and "快递.md" in out)
    _h, out = commands.dispatch("/问 周五打球几点", ctx)
    check_true("/问 走到回答函数", out.startswith("（模型回答）"))
    _h, out = commands.dispatch("/问", ctx)
    check_true("不带问题给用法", "用法" in out)
    cfg_off = Config({"knowledge": {"enabled": False}, "commands": {"enabled": True}})
    _h, out = commands.dispatch("/问 任意问题", dict(ctx, cfg=cfg_off, knowledge_answer=None))
    check_true("关掉开关时说明清楚", "没开" in out)
    _h, out = commands.dispatch("/知识", dict(ctx, knowledge_summary=None))
    check_true("没接知识库时也有说明", "没接知识库" in out)
    check_true("帮助里有这两条", "/问" in commands.HELP and "/知识" in commands.HELP)

    print("[模型工具：lookup_notes]")
    cfg_tools = Config({"tools": {"profiles": ["cloud"], "assistant": {"enabled": True}},
                        "knowledge": {"enabled": True, "dir": str(cfg.path_of("knowledge.dir"))}})
    b = ToolBox(cfg_tools, log=lambda _m: None, store=s,
                context={"username": "filehelper", "is_owner": True})
    names = [t["function"]["name"] for t in b.schemas()]
    check_true("工具声明里有 lookup_notes", "lookup_notes" in names)
    out = b.call("lookup_notes", {"question": "球馆在哪里"})
    check_true("查到带文件名的段落", "✅ 成功" in out and "羽毛球.md" in out)
    out2 = b.call("lookup_notes", {"question": "银河系悬臂结构"})
    check_true("查不到就明说别编", "❌ 失败" in out2 and "查不到" in out2)
    check_true("缺参数不抛", "❌ 失败" in b.call("lookup_notes", {}))
    cfg_no_kb = Config({"tools": {"assistant": {"enabled": True}},
                        "knowledge": {"enabled": False}})
    b2 = ToolBox(cfg_no_kb, log=lambda _m: None, store=s,
                 context={"username": "filehelper", "is_owner": True})
    check_true("知识库关掉时工具也拒", "❌ 失败" in b2.call("lookup_notes", {"question": "x"}))

    print("[审查 P2：知识库要有大小上限（单文件 1MB / 总块数）]")
    import tempfile as _tf  # noqa: PLC0415
    from wxbot.knowledge import load_chunks  # noqa: PLC0415
    kb = pathlib.Path(_tf.mkdtemp()) / "kb"
    kb.mkdir()
    (kb / "小文件.md").write_text("这是一段正常的知识内容，用来测试大小上限。\n" * 5,
                                   encoding="utf-8")
    (kb / "巨大文件.md").write_text("把文件撑大一点。\n" * 200000, encoding="utf-8")
    cfg_kb = Config({"knowledge": {"dir": str(kb)}})
    chunks_kb = load_chunks(cfg_kb)
    check("小文件会被读到", any(c["file"] == "小文件.md" for c in chunks_kb), True)
    check("超过 1MB 的文件被跳过", any("巨大文件" in c["file"] for c in chunks_kb), False)
    cfg_kb2 = Config({"knowledge": {"dir": str(kb), "max_chunks": 2}})
    check("总块数上限生效", len(load_chunks(cfg_kb2)) <= 2, True)
    cfg_kb3 = Config({"knowledge": {"dir": str(kb), "max_file_mb": 100}})
    check("把上限调大就能读（不是写死）",
          any("巨大文件" in c["file"] for c in load_chunks(cfg_kb3)), True)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
