"""安全门禁回归测试（B1 + 配置结构）。
跑法：python tests/test_safety.py
"""
import copy
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot.config import Config  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
PASS, FAIL = [], []


def check(name, ok):
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}")


def base():
    return Config.load(ROOT / "config.example.yaml")


def main():
    print("[结构校验]")
    check("示例配置结构无问题", not base().validate_structure())

    bad = base()
    bad.data.setdefault("defaults", {})["nonsense"] = 1
    check("defaults 里的未知键会被点名（写错层级 = 静默不生效）",
          any("defaults.nonsense" in p for p in bad.validate_structure()))

    # ★2026-09-29 评审"方向 1：档位 / 工具 两张表自洽"
    toolbad = base()
    toolbad.data.setdefault("tools", {})["profiles"] = ["clould"]       # 故意拼错
    check("tools.profiles 里档位名拼错会被点名（否则静默变成'谁都不能调工具'）",
          any("tools.profiles" in p and "clould" in p for p in toolbad.validate_structure()))

    toolhint = base()
    toolhint.data.setdefault("tools", {})["profiles"] = ["local"]        # 合法名，但当前档位是 cloud
    toolhint.data.setdefault("llm", {})["active"] = "cloud"
    check("当前档位不在 tools.profiles 里 → 启动提示'它不会自己调工具'",
          any("不会自己调工具" in h for h in toolhint.hints()))

    localactive = base()                                                 # 本地档不列进工具是故意的
    localactive.data.setdefault("llm", {})["active"] = "local"
    check("本地档（ollama）不提示这条（免得正常配置天天刷）",
          not any("不会自己调工具" in h for h in localactive.hints()))

    print("[风控体检（2026-09-28 评审：账号行为形态四条）]")
    from wxbot.cli import risk_audit  # noqa: PLC0415
    c = base()
    c.data["contacts"] = [{"name": "群A", "username": "1@chatroom", "enabled": True,
                           "trigger": {"mode": "always"}}]
    c.data["defaults"] = {"trigger": {"time_window": ["00:00", "23:59"]},
                          "limits": {"global_per_day": 0}}
    warns = risk_audit(c)
    check("群 always 模式被点名", any("群A" in w for w in warns))
    check("全天时间窗被点名", any("全天" in w for w in warns))
    check("总量闸为 0 被点名", any("global_per_day" in w for w in warns))
    c2 = base()
    c2.data["contacts"] = [{"name": "群B", "username": "2@chatroom", "enabled": True,
                            "trigger": {"mode": "mention"}}]
    c2.data["defaults"] = {"trigger": {"time_window": ["08:00", "23:30"]},
                           "limits": {"global_per_day": 150}}
    c2.data["bot"] = {"watch_notify": "文件传输助手"}
    c2.data["contacts"].append({"name": "文件传输助手", "username": "filehelper",
                                "enabled": True, "self_ok": True})
    _w2 = risk_audit(c2)
    check("配置合规时零警告", not _w2)
    if _w2:
        print("     实际警告:", _w2)

    print("[doctor --fix 的纯函数（2026-09-28 产品化三件套之二）]")
    import pathlib as _pl  # noqa: PLC0415
    from wxbot import doctor as doc  # noqa: PLC0415
    check("hostport 正常解析", doc._hostport("http://127.0.0.1:5031") == ("127.0.0.1", 5031))
    check("hostport 空串兜底", doc._hostport("") == ("127.0.0.1", 5031))
    check("找不到修复脚本时安全返回 None",
          doc.find_repair_script(_pl.Path("Z:/不存在的目录")) is None)
    _wf_exe = doc.find_weflow_exe(None)
    check("WeFlow.exe 探测：要么 None、要么是真实文件", _wf_exe is None or _wf_exe.exists())

    print("[标点归一化（2026-09-28 真机：全角？/半角? 把 B1 校验卡死）]")
    from wxbot.send_maa import MaaSender, punct_norm  # noqa: PLC0415
    check("全角问号 → 半角", punct_norm("这是什么？") == "这是什么?")
    check("只动标点，不动汉字字母", punct_norm("A?a？b！") == "A?a?b!")
    s_ma = MaaSender.__new__(MaaSender)          # 只为测纯逻辑，不连 MaaMCP
    s_ma._win_size = (1400, 1000)
    s_ma._names = set()
    s_ma.verify_full_name = True
    _w_full, _ = s_ma._title_check(
        [{"text": "这是什么？", "box": [300, 40, 120, 24]}], "这是什么?")
    check("标题校验容忍全角/半角问号", _w_full)
    _w_bad, _ = s_ma._title_check(
        [{"text": "这是什么梗？", "box": [300, 40, 140, 24]}], "这是什么?")
    check("名字更长仍不许过（B1 精度不降）", not _w_bad)

    bad2 = base()
    bad2.data["safety"] = {"require_ack": False, "persona": {"system_prompt": "x"},
                           "reply": {"max_chars": 120}}
    probs2 = bad2.validate_structure()
    check("persona/reply 写到 safety 下面会被点名（模板原来就是这么错的）",
          any("safety.persona" in p for p in probs2)
          and any("safety.reply" in p for p in probs2))

    print("[B1：启用真实联系人必须完整名校验]")
    c = base()
    c.data["contacts"].append({"name": "", "username": "wxid_x", "enabled": True})
    problems = c.validate_safety()
    check("启用但缺完整显示名 → 被拦", any("缺少完整显示名" in p for p in problems))

    c2 = base()
    c2.data["send"]["verify_full_name"] = False
    check("关闭完整名校验 → 被拦", any("verify_full_name" in p for p in c2.validate_safety()))

    c3 = base()
    c3.data["send"]["verify_title"] = False
    check("关闭标题校验 → 被拦", any("verify_title" in p for p in c3.validate_safety()))

    c4 = base()
    c4.data["contacts"] = [{"name": "文件传输助手", "username": "filehelper",
                            "enabled": True, "self_ok": True}]
    check("只有自聊会话 → 放行", not c4.validate_safety())

    c5 = base()
    c5.data["contacts"].append({"name": "某人", "username": "wxid_y", "enabled": False})
    check("未启用的会话不参与门禁", not any("wxid_y" in p for p in c5.validate_safety()))

    print("[身份唯一性（发送前数据回读的判据）]")
    from wxbot.send_maa import MaaSender  # noqa: E402

    s = MaaSender("x", None)
    s.resolver = lambda name: []                       # 读端查不到
    check("读不到目标 → 拒绝", s._resolve_unique("某人")[0] is False)
    s.resolver = lambda name: [{"displayName": "示例机器人", "username": "a"},
                               {"displayName": "示例机", "username": "b"}]
    check("多个候选 → 拒绝", s._resolve_unique("示例机")[0] is False)
    s.resolver = lambda name: [{"displayName": "示例机器人", "username": "a"}]
    check("唯一候选 → 放行（第二轮口径；第三轮已收紧为必须完全同名，见下）",
          s._resolve_unique("示例机器人")[0] is True)

    print("[N1：同前缀歧义必须被挡住（发错人的最后一道）]")
    def band(text, x=600, y=30):
        # 带上窗口按钮与底部一条，让 _dims 推出真实窗口尺寸（否则标题条带判为空）
        return [{"text": text, "box": [x, y, 120, 26]},
                {"text": "凸", "box": [1400, 20, 30, 20]},
                {"text": "底部", "box": [700, 980, 60, 24]}]
    from wxbot.send_maa import MaaSender  # noqa: PLC0415
    s2 = MaaSender("maa_mcp", None)
    check("标题「文件传输助手」对目标「助手」→ 不认（旧版「包含」会误认）",
          s2._title_check(band("文件传输助手"), "助手")[0] is False)
    check("标题「文件传输助手」对目标「文件传输助手」→ 认",
          s2._title_check(band("文件传输助手"), "文件传输助手")[0] is True)
    check("标题「示例群C(8)」对目标「示例群C」→ 认",
          s2._title_check(band("示例群C(8)"), "示例群C")[0] is True)
    check("标题「小张三」对目标「小张」→ 认（同前缀 OCR 分不出来，靠数据侧拦）",
          s2._title_check(band("小张三"), "小张")[0] is False)     # 第三轮审查：前缀方向也要拦
    check("标题「示例机器人」对目标「示例机」→ 不认（第三轮审查给的那张表）",
          s2._title_check(band("示例机器人"), "示例机")[0] is False)
    check("标题「文件传输助手(2)」这种不会出现在私聊，但群名带人数后缀要认",
          s2._title_check(band("示例群C(8)"), "示例群C")[0] is True)
    s.resolver = lambda name: [{"displayName": "小张三", "username": "a"},
                               {"displayName": "小张", "username": "b"}]
    check("数据侧：同前缀两个候选 → 拒绝", s._resolve_unique("小张")[0] is False)
    s.resolver = lambda name: [{"displayName": "文件传输助手", "username": "a"}]
    check("数据侧：唯一候选但不是以配置名开头 → 拒绝",
          s._resolve_unique("助手")[0] is False)
    s.resolver = lambda name: [{"displayName": "示例机器人", "username": "a"}]
    check("数据侧：唯一候选但显示名更长（前缀方向）→ 拒绝（第三轮审查）",
          s._resolve_unique("示例机")[0] is False)
    s.resolver = lambda name: [{"displayName": "示例机器人", "username": "a"}]
    check("数据侧：配置名与显示名完全一致 → 放行",
          s._resolve_unique("示例机器人")[0] is True)
    s.resolver = lambda name: [{"displayName": "示例群C", "username": "g@chatroom"}]
    check("数据侧：群名（不含人数后缀）完全一致 → 放行",
          s._resolve_unique("示例群C")[0] is True)

    print("[窗口被拖窄：名字被截断时也要找得到会话（2026-09-26 真机）]")

    def win_items(rows, w=1400, h=1000):
        # 带上右上角窗口按钮与底部一条，让 _dims 推出真实窗口尺寸
        return [{"text": t, "box": list(b)} for t, b in rows] + [
            {"text": "凸", "box": [w - 30, 20, 30, 20]},
            {"text": "底部", "box": [w // 2, h - 20, 60, 24]}]

    s3 = MaaSender("maa_mcp", None)
    wide = win_items([("主人", [120, 260, 90, 26]), ("文件传输助手", [120, 320, 150, 26])])
    check("宽窗口：完整名命中列表行", len(s3._list_rows(wide, "主人")) == 1)
    check("宽窗口：右侧标题（不在列表列里）不算列表行",
          s3._list_rows(win_items([("主人", [0.35 * 1400, 120, 60, 26])]), "主人") == [])

    narrow = win_items([("示例一号…", [325, 250, 150, 26])], w=895, h=647)
    got_narrow = s3._list_rows(narrow, "示例一号训练营")
    check("窄窗口：名字被截断成「示例一号…」也能找到那一行", len(got_narrow) == 1)
    check("窄窗口：这条记成「截断命中」（日志里看得出是靠前缀认的）",
          s3._row_match_kind == "truncated")

    no_mark = win_items([("文件传输", [325, 320, 120, 26])], w=895, h=647)
    check("窄窗口：OCR 把省略号吃掉也能找到",
          len(s3._list_rows(no_mark, "文件传输助手")) == 1)

    two_same = win_items([("示例一号…", [325, 250, 150, 26]),
                          ("示例一号…", [325, 300, 150, 26])], w=895, h=647)
    check("窄窗口：两行同前缀 → 不猜（交给搜索兜底/报错）",
          s3._list_rows(two_same, "示例一号训练营") == [])

    short_name = win_items([("主人", [325, 350, 60, 26])], w=895, h=647)
    check("窄窗口：没被截断的短名也认（列表跑到窗口 36% 处）",
          len(s3._list_rows(short_name, "主人")) == 1)

    title_like = win_items([("示例一号训练营(10)", [530, 181, 220, 26])], w=895, h=647)
    check("窄窗口：右侧聊天标题不会被当成列表行",
          s3._list_rows(title_like, "示例一号训练营") == [])

    too_short = win_items([("示例机…", [120, 250, 90, 26])], w=1400, h=1000)
    check("配置名和目标名只差一个字时，截断行不会当成它（前缀方向依然拦）",
          s3._list_rows(too_short, "示例机") == [])

    s4 = MaaSender("maa_mcp", None)
    s4._names = {"主人", "ㅤㅤ", "liangun1346"}     # 同一个人的三套叫法（真机实测）
    multi = win_items([("liangu…", [325, 250, 120, 26])], w=895, h=647)
    check("窄窗口：三套叫法时按微信号的截断行也认",
          len(s4._list_rows(multi, "主人")) == 1)

    print("[第三轮审查建议：会话名体检（doctor 用，纯函数可单测）]")
    from wxbot.cli import check_contact_names
    thin = Config({"contacts": [{"name": "示例机", "username": "wxid_a", "enabled": True}]})
    probs = check_contact_names(thin, lambda n: [{"displayName": "示例机器人", "username": "wxid_a"}])
    check("配置名写短了 → 体检报错", bool(probs) and "示例机" in probs[0])
    good = Config({"contacts": [{"name": "示例机器人", "username": "wxid_a", "enabled": True}]})
    check("配置名完整 → 体检通过",
          check_contact_names(good, lambda n: [{"displayName": "示例机器人", "username": "wxid_a"}]) == [])
    check("自聊会话不参与体检",
          check_contact_names(Config({"contacts": [{"name": "文件传输助手", "username": "filehelper",
                                                    "enabled": True, "self_ok": True}]}),
                              lambda n: []) == [])

    print("[配置里缺环境变量必须报警（不能静默变空串）]")
    import os
    import tempfile
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    p = tmpdir / "c.yaml"
    p.write_text("llm:\n  profiles:\n    cloud:\n      api_key: ${CODEX_TEST_MISSING_VAR}\n"
                 "      note: ${CODEX_TEST_PRESENT_VAR}\n"
                 '      path: "%CODEX_TEST_MISSING_PCT%/x"\n', encoding="utf-8")
    os.environ["CODEX_TEST_PRESENT_VAR"] = "yes"
    warns = Config.load(p).warnings
    check("缺失的 ${VAR} 会被点名", any("CODEX_TEST_MISSING_VAR" in w for w in warns))
    check("设了值的 ${VAR} 不报警", not any("CODEX_TEST_PRESENT_VAR" in w for w in warns))
    check("缺失的 %VAR% 也会报警", any("CODEX_TEST_MISSING_PCT" in w for w in warns))
    tpl = Config.load(ROOT / "config.example.yaml").warnings
    check("示例模板里那个云端 key 占位符会被点名（而不是静默）",
          any("LLM_CLOUD_KEY" in w for w in tpl)
          or bool(os.environ.get("LLM_CLOUD_KEY")))

    print("[推理模型没关思考 → 必须告警（否则正文静默为空）]")
    def cfg_with(model, think=None):
        prof = {"type": "ollama", "model": model}
        if think is not None:
            prof["think"] = think
        return Config({"llm": {"active": "local", "profiles": {"local": prof}}})
    check("qwen3.5 没写 think → 告警", bool(cfg_with("qwen3.5:9b").reasoning_model_warnings()))
    check("qwen3.5 写了 think: false → 不告警",
          not cfg_with("qwen3.5:9b", False).reasoning_model_warnings())
    check("普通模型（llama3）→ 不告警", not cfg_with("llama3:8b").reasoning_model_warnings())
    check("示例配置本身不踩这个坑",
          not Config.load(ROOT / "config.example.yaml").reasoning_model_warnings())

    print("[N5：install.ps1 建的 .venv 也要在 maa_mcp 的查找范围里]")
    import sys as _sys
    from wxbot.cli import maa_exe_candidates
    cands = [c.replace("\\", "/") for c in maa_exe_candidates(base()) if c]
    here_py = str(pathlib.Path(_sys.executable).parent).replace("\\", "/")
    check("候选里包含「正在跑的解释器所在目录」", any(here_py in c for c in cands))
    check("候选里包含工程内 .venv", any("/.venv/Scripts/maa_mcp.exe" in c for c in cands))
    check("候选里包含工程内 .venv312", any("/.venv312/Scripts/maa_mcp.exe" in c for c in cands))

    print("[N6：allow_context=false 时，摘要/记忆/风格示例也不能外发]")
    from wxbot.llm import LLM
    seen = {}

    class Capture(LLM):
        def _call(self, cfg, system, hist, user_text):
            seen["system"] = system
            seen["hist"] = hist
            # ★2026-09-27：摘要/记忆/风格这些"每轮都变"的段改拼在**用户消息末尾**（为了前缀缓存），
            # 所以断言要看 system + user 合起来的内容
            seen["user"] = user_text
            return "ok"

    def blob():
        return (seen["system"] or "") + "\n" + (seen["user"] or "")

    parts = {"summary": "摘要X对方不吃香菜", "memory": ["记忆Y住示例市"], "style": ["风格Z简短"]}
    hist = [{"role": "user", "content": "历史W"}]
    cloud = Capture({"cloud": {"type": "openai", "allow_context": False}}, "cloud")
    cloud.generate("人设", hist, "这句", None, parts)
    check("云端 profile 收不到 history", seen["hist"] == [])
    check("云端 profile 收不到摘要", "摘要X" not in blob())
    check("云端 profile 收不到长期记忆", "记忆Y" not in blob())
    check("云端 profile 收不到风格示例", "风格Z" not in blob())
    local = Capture({"local": {"type": "ollama", "allow_context": True}}, "local")
    local.generate("人设", hist, "这句", None, parts)
    check("本地 profile 三种成分都收得到",
          all(k in blob() for k in ("摘要X", "记忆Y", "风格Z")))

    print("[档位差异：云端要求多（长上下文/多记忆）、本地要求少（短/便宜）]")
    tier = Capture({
        "cloud": {"type": "openai", "allow_context": True, "max_context_turns": 12,
                  "max_memory": 12, "max_style": 5,
                  "persona_extra": "（云端）你更强，可以更细致。"},
        "local": {"type": "ollama", "allow_context": True, "max_context_turns": 3,
                  "max_memory": 3, "max_style": 2,
                  "persona_extra": "（本地）短、简单，别编实时信息。"},
    }, "cloud", ["local"])
    long_hist = [{"role": "user", "content": f"历史{i}"} for i in range(20)]
    rich_parts = {"summary": "摘要X", "memory": [f"记忆{i}" for i in range(10)],
                  "style": [f"风格{i}" for i in range(5)]}
    got = {}
    for name in ("cloud", "local"):
        tier.generate("人设", long_hist, "这句", name, rich_parts)
        got[name] = (len(seen["hist"]), blob().count("记忆"),
                     blob().count("风格"), blob())
    check("云端看到更多历史", got["cloud"][0] > got["local"][0])
    check("本地只保留 6 条（3 轮）", got["local"][0] == 6)
    check("云端记忆更多", got["cloud"][1] > got["local"][1])
    check("云端风格示例更多", got["cloud"][2] > got["local"][2])
    check("两档各有自己的额外要求",
          ("（云端）" in got["cloud"][3]) and ("（本地）" in got["local"][3]))

    print("[N7：黑帧也必须被判为坏帧（旧公式会把全黑算成「内容满满」）]")
    import tempfile as _tf
    from PIL import Image as _Image
    d = pathlib.Path(_tf.mkdtemp())
    stats = {}
    for name, color in (("white", 255), ("black", 0), ("normal", 128)):
        p = d / f"{name}.png"
        _Image.new("L", (60, 40), color).save(p)
        stats[name] = s2._frame_stats(p)
    check("全白帧 非白≈0（判坏）", stats["white"][0] < 0.02)
    check("全黑帧 非黑≈0（判坏，旧公式这里是 1.0）", stats["black"][1] < 0.02)
    check("正常帧两个指标都高（判好）",
          stats["normal"][0] > 0.02 and stats["normal"][1] > 0.02)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
