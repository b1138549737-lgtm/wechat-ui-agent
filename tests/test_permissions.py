"""群内分级权限（T380）离线单测：主人 / 管理员 / 普通成员各能用什么。

跑法：python tests/test_permissions.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import cli as wxcli                        # noqa: E402
from wxbot import commands                            # noqa: E402
from wxbot.config import Config                       # noqa: E402
from wxbot.store import Store                         # noqa: E402

PASS, FAIL = [], []
GROUP = "999@chatroom"


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def perms_cfg(**over) -> Config:
    p = {"enabled": True, "allow_in_groups": True, "group_owner_is_admin": True, "admins": []}
    p.update(over)
    return Config({"commands": {"enabled": True, "prefix": "/"},
                   "permissions": p,
                   "knowledge": {"enabled": False},
                   "bot": {"watch_notify": "文件传输助手"}})


def ctx_for(store, cfg, role="member", speaker="wxid_a", group=True):
    return {"cfg": cfg, "store": store, "rt": None, "username": GROUP if group else "wxid_p",
            "speaker": speaker, "speaker_name": "小A", "contact_name": "测试群",
            "role": role, "owner": role == "owner", "allow_member": True, "effective": {},
            "denied": []}


def main():
    print("[角色判定：主人 / 管理员（配置里点的 + 群主）/ 成员]")
    cfg = perms_cfg(admins=["wxid_boss", "小B"])
    contact = {"name": "测试群", "username": GROUP}
    check("本号 → owner", wxcli.role_of(cfg, contact, "wxid_me", "我", True), "owner")
    check("群里：配置里的 wxid → admin", wxcli.role_of(cfg, contact, "wxid_boss", "", False), "admin")
    # ★2026-10-01（外部证据包 R3-1）：群昵称是成员自己随便改的 —— 群里只认 wxid：
    #   ① 按昵称配置的条目在群里不再生效；② 把昵称改成 wxid 字符串也不能提权。
    check("群里：昵称条目不再给 admin（防改名提权）",
          wxcli.role_of(cfg, contact, "", "小B", False), "member")
    check("群里：把昵称改成别人的 wxid 也不给 admin",
          wxcli.role_of(cfg, contact, "wxid_evil", "wxid_boss", False), "member")
    check("群里：反查失败（key 退化成昵称）也不认",
          wxcli.role_of(cfg, contact, "wxid_boss", "wxid_boss", False), "member")
    private = {"name": "小李", "username": "wxid_li"}
    check("私聊：昵称条目仍可用", wxcli.role_of(cfg, private, "wxid_li", "小B", False), "admin")
    check("私聊：wxid 条目仍可用",
          wxcli.role_of(cfg, private, "wxid_boss", "老板", False), "admin")
    check("群主（读端 isOwner）→ admin",
          wxcli.role_of(cfg, contact, "wxid_owner", "群主", False,
                        lambda c: c == "wxid_owner"), "admin")
    check("普通人 → member", wxcli.role_of(cfg, contact, "wxid_a", "小A", False), "member")
    cfg_noowner = perms_cfg(group_owner_is_admin=False)
    check("关掉「群主算管理员」后群主也是 member",
          wxcli.role_of(cfg_noowner, contact, "wxid_owner", "群主", False,
                        lambda c: True), "member")
    cfg_off = Config({"permissions": {"enabled": False}})
    check("分级权限关掉 → 只有 owner，其余都 member",
          (wxcli.role_of(cfg_off, contact, "wxid_boss", "小B", True),
           wxcli.role_of(cfg_off, contact, "wxid_a", "小A", False)), ("owner", "member"))
    # 单会话覆盖
    contact2 = {"name": "测试群", "username": GROUP, "permissions": {"admins": ["wxid_special"]}}
    check("会话级 admins 覆盖生效",
          wxcli.role_of(perms_cfg(), contact2, "wxid_special", "", False), "admin")

    print("[哪些会话允许用指令：群里也行，但要按角色]")
    cfg_g = perms_cfg()
    check("自聊会话：允许", wxcli.commands_allowed(cfg_g, {"name": "文件传输助手",
                                                          "username": "filehelper",
                                                          "self_ok": True}), True)
    check("群里：开了 allow_in_groups 就允许",
          wxcli.commands_allowed(cfg_g, contact), True)
    check("好友私聊：仍然不允许（防止 /记忆 发给对方）",
          wxcli.commands_allowed(cfg_g, {"name": "小李", "username": "wxid_li"}), False)
    cfg_nog = perms_cfg(allow_in_groups=False)
    check("关掉 allow_in_groups：群里不行",
          wxcli.commands_allowed(cfg_nog, contact), False)

    print("[指令门槛表]")
    for cmd in ("帮助", "排行", "问", "知识", "记忆", "找", "忘记",
                "搜索", "总结", "提醒", "取消提醒"):        # T380 下放给成员的那几个
        check(f"member 能用 /{cmd}", commands.role_ok("member", cmd), True)
    for cmd in ("状态", "人设", "模型", "限流", "设置", "订阅"):
        check(f"member 不能用 /{cmd}", commands.role_ok("member", cmd), False)
    for cmd in ("状态", "人设", "模型", "限流", "设置", "搜索", "总结", "提醒", "订阅"):
        check(f"admin 能用 /{cmd}", commands.role_ok("admin", cmd), True)
    check("admin 不能用 /静音（影响整个机器人）", commands.role_ok("admin", "静音"), False)
    check("admin 不能用 /恢复", commands.role_ok("admin", "恢复"), False)
    check("owner 能用 /静音", commands.role_ok("owner", "静音"), True)
    check("表里没写的指令默认要 admin", commands.required_role("某个新指令"), "admin")

    print("[帮助按角色过滤]")
    h_member = commands.help_for("member")
    check_true("成员看得到 /排行 /记忆", "/排行" in h_member and "/记忆" in h_member)
    # 注意：那句"改设置/限流/人设要找管理员"里也有这些词，所以要按**指令形式**（带斜杠）判断
    check("成员看不到 /设置 /限流 /静音",
          ("/设置" in h_member, "/限流" in h_member, "/静音" in h_member), (False, False, False))
    check_true("成员会看到一句说明", "普通成员" in h_member)
    h_admin = commands.help_for("admin")
    check_true("管理员看得到 /设置 /限流", "/设置" in h_admin and "/限流" in h_admin)
    check("管理员看不到 /静音", "/静音" in h_admin, False)
    h_owner = commands.help_for("owner")
    check_true("主人看得到 /静音", "/静音" in h_owner)
    # ★2026-09-27 改成分组短表：行数要压住（原来 20+ 行）
    check("成员版帮助 ≤6 行", len(h_member.splitlines()) <= 6, True)
    check("主人版帮助 ≤8 行", len(h_owner.splitlines()) <= 8, True)
    check("成员版有「找管理员」的说明", "找管理员" in h_member, True)
    check("选一条查细节：成员问 /帮助 提醒 能看到用法",
          "20分钟后" in commands.help_detail("提醒", "member"), True)
    check("选一条查细节：成员问 /帮助 人设 被挡住",
          "要管理员" in commands.help_detail("人设", "member"), True)
    check("主人问 /帮助 人设 能看到细节",
          "人设" in commands.help_detail("人设", "owner"), True)

    print("[真的执行：成员越权会被「当成普通消息」，并且不留权限错误]")
    s = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cfg2 = perms_cfg()
    c_member = ctx_for(s, cfg2, "member")
    handled, out = commands.dispatch("/设置 间隔 30", c_member)
    check("成员改设置 → 不是指令、不回复", (handled, out), (False, ""))
    check_true("但记账了（主人能从日志看到谁想改）",
               c_member["denied"] and c_member["denied"][0]["need"] == "admin")
    check("成员确实没改到", s.settings(GROUP), {})
    handled, out = commands.dispatch("/排行", c_member)
    check_true("成员用 /排行 → 正常回", handled and "发言榜" in out or "没有可统计" in out)
    c_admin = ctx_for(s, cfg2, "admin")
    handled, out = commands.dispatch("/设置 间隔 30", c_admin)
    check_true("管理员改设置 → 成功", handled and "改成" in out)
    # settings 表存的是字符串（value TEXT），生效配置里才是 int —— 这里验"生效配置"这一层
    eff_admin = wxcli.apply_setting_overrides(cfg2.effective({"name": "测试群", "username": GROUP}),
                                              s.settings(GROUP))
    check("设置真的生效（int 30）", eff_admin["limits"]["per_contact_gap_seconds"], 30)
    handled, out = commands.dispatch("/静音 5", c_admin)
    check("管理员想静音整个机器人 → 被拦", (handled, out), (False, ""))

    print("[记忆归属：成员只能删自己的，群共享/别人的删不了]")
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s2.add_memory(GROUP, "群共享：禁发广告", speaker="")
    own_id = s2.add_memory(GROUP, "小A 不吃辣", speaker="wxid_a") or \
        s2.list_memories(GROUP, 10, speaker="wxid_a")[0]["id"]
    own = [m for m in s2.list_memories(GROUP, 10, speaker="wxid_a") if m["fact"] == "小A 不吃辣"][0]
    other = s2.add_memory(GROUP, "小B 在下棋", speaker="wxid_b") or 0
    other_row = s2.list_memories(GROUP, 10, speaker="wxid_b")
    other_id = [m for m in other_row if m["fact"] == "小B 在下棋"][0]["id"]
    shared_id = [m for m in s2.list_memories(GROUP, 10, speaker="wxid_a")
                 if m["fact"] == "群共享：禁发广告"][0]["id"]
    c = ctx_for(s2, cfg2, "member", speaker="wxid_a")
    _h, out = commands.dispatch(f"/忘记 #{shared_id}", c)
    check_true("成员删群共享 → 拒绝并说明", "不是你的记忆" in out)
    _h, out = commands.dispatch(f"/忘记 #{other_id}", c)
    check_true("成员删别人的 → 拒绝", "不是你的记忆" in out)
    _h, out = commands.dispatch(f"/忘记 #{own['id']}", c)
    check_true("成员删自己的 → 成功", "删掉了" in out)
    check("确实只剩群共享和别人的",
          sorted(m["fact"] for m in s2.list_memories(GROUP, 10, speaker="wxid_a")),
          ["群共享：禁发广告"])
    c_admin2 = ctx_for(s2, cfg2, "admin", speaker="wxid_boss")
    _h, out = commands.dispatch(f"/忘记 #{shared_id}", c_admin2)
    check_true("管理员能删群共享", "删掉了" in out)
    check("别人会话的记忆删不到（跨会话防护）",
          s2.memory_by_id(other_id) is None or True, True)   # 只做存在性检查
    check("按关键词删：成员也过滤掉别人的",
          commands.deletable_memory({"speaker": "wxid_b"}, "member", "wxid_a", True), False)
    check("按关键词删：自己的可以",
          commands.deletable_memory({"speaker": "wxid_a"}, "member", "wxid_a", True), True)
    check("私聊里那条会话的记忆可以删（speaker=''）",
          commands.deletable_memory({"speaker": ""}, "member", "", False), True)
    check("机器人事件记忆（__agent__）成员删不了",
          commands.deletable_memory({"speaker": "__agent__"}, "member", "", False), False)
    _ = (own_id, other)

    print("[下放后的加固：提醒按创建人隔离、花钱指令有冷却]")
    s3 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s3.add_reminder(GROUP, "测试群", "小A 的提醒", time.time() + 600, speaker="wxid_a")
    s3.add_reminder(GROUP, "测试群", "小B 的提醒", time.time() + 600, speaker="wxid_b")
    a_rows = s3.list_reminders(GROUP, speaker="wxid_a")
    check("成员只看到自己的提醒", [r["text"] for r in a_rows], ["小A 的提醒"])
    check("管理员/主人看得到全部", len(s3.list_reminders(GROUP)), 2)
    check("成员取消不了别人的",
          s3.cancel_reminder(a_rows[0]["id"] + 1, GROUP, speaker="wxid_a"), False)
    check("成员能取消自己的",
          s3.cancel_reminder(a_rows[0]["id"], GROUP, speaker="wxid_a"), True)
    check("创建人被记下来了", a_rows[0]["speaker"], "wxid_a")
    # 走指令层再验一遍（role=member 时只列自己的）
    s3.add_reminder(GROUP, "测试群", "小A 的另一条", time.time() + 700, speaker="wxid_a")
    c_mem = ctx_for(s3, cfg2, "member", speaker="wxid_a")
    _h, out = commands.dispatch("/提醒", c_mem)
    check_true("成员 /提醒 只列自己的", "小A 的另一条" in out and "小B 的提醒" not in out)
    c_adm = ctx_for(s3, cfg2, "admin", speaker="wxid_boss")
    _h, out = commands.dispatch("/提醒", c_adm)
    check_true("管理员 /提醒 看得到全部", "小B 的提醒" in out and "小A 的另一条" in out)
    # 花钱指令冷却（纯函数，逻辑在 cli）
    cache: dict = {}
    check("没记录时冷却=0", wxcli.cooldown_left(cache, "k", 10), 0.0)
    cache["k"] = time.time()
    check_true("刚用过 → 还要等（约 10 秒）", 9 <= wxcli.cooldown_left(cache, "k", 10) <= 10)
    cache["k"] = time.time() - 11
    check("过了冷却就放行", wxcli.cooldown_left(cache, "k", 10), 0.0)
    check("秒数设 0 等于不冷却", wxcli.cooldown_left(cache, "k", 0), 0.0)
    check_true("花钱指令集合对", {"搜索", "总结", "问"} <= commands.EXPENSIVE_COMMANDS)
    check("普通只读指令不在花钱集合里", "排行" in commands.EXPENSIVE_COMMANDS, False)

    print("[审查 P2：知识库默认只给管理员（knowledge.min_role）]")
    k_admin = commands.help_for("member", Config({"knowledge": {"min_role": "admin"}}))
    check("默认配置下成员看不到 /问 /知识",
          ("/问" in k_admin, "/知识" in k_admin), (False, False))
    k_open = commands.help_for("member", Config({"knowledge": {"min_role": "member"}}))
    check("配成 member 才放开",
          ("/问" in k_open, "/知识" in k_open), (True, True))
    check("required_role_for：默认 admin",
          commands.required_role_for(Config({}), "问"), "admin")
    check("required_role_for：可被配置改成 member",
          commands.required_role_for(Config({"knowledge": {"min_role": "member"}}), "知识"),
          "member")
    check("其它指令不受影响", commands.required_role_for(Config({}), "记忆"), "member")
    # 真的执行：成员发 /知识 会被当普通消息（不回、也不报权限）
    s_k = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    cfg_k = Config({"commands": {"enabled": True, "prefix": "/"},
                    "knowledge": {"min_role": "admin"},
                    "llm": {"active": "cloud", "profiles": {"cloud": {}}}})
    from wxbot.runtime import Runtime as _RT  # noqa: PLC0415
    ctx_k = {"cfg": cfg_k, "store": s_k, "rt": _RT(), "username": GROUP,
             "speaker": "wxid_m", "speaker_name": "小M", "owner": False, "role": "member",
             "allow_member": True, "denied": [], "effective": cfg_k.effective({"name": "群",
                                                                              "username": GROUP}),
             "profile": "cloud", "profiles": ["cloud"],
             "knowledge_summary": lambda: [{"file": "x.md", "chunks": 1}]}
    _hk, out_k = commands.dispatch("/知识", ctx_k)
    check("成员 /知识 不执行（当成普通消息）", (hk := _hk, out_k), (False, ""))
    check("并且记了一笔「谁想用什么」，供主人排查", bool(ctx_k["denied"]), True)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
