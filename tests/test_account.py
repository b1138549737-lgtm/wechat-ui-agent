"""认当前微信账号（T360）离线单测：换号后"自动用新的"这套逻辑。

跑法：python tests/test_account.py
"""
import json
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import account                            # noqa: E402
from wxbot.config import Config                      # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def env(tmp: pathlib.Path, wf: dict | None = None, dirs: list[str] | None = None) -> Config:
    """造一个"假 WeFlow 配置 + 假微信数据目录"的环境。"""
    cfg_file = tmp / "WeFlow-config.json"
    if wf is not None:
        cfg_file.write_text(json.dumps(wf, ensure_ascii=False), encoding="utf-8")
    data_root = tmp / "xwechat_files"
    data_root.mkdir(exist_ok=True)
    for i, name in enumerate(dirs or []):
        d = data_root / name
        d.mkdir(exist_ok=True)
        # 让最后一个（列表末尾）看起来最新
        import os
        os.utime(d, (time.time() + i, time.time() + i))
    return Config({"ingest": {"weflow": {"config_file": str(cfg_file)}},
                   "bot": {"wechat_data_dir": str(data_root),
                           # 离线测试只看我们造的假目录，不去扫本机真实的 <WECHAT_DATA_DIR>
                           "scan_default_dirs": False}})


def add_login(cfg: Config, wxid: str, when: float) -> None:
    """造一条"微信登录记录"：`all_users/login/<wxid>/key_info.db`（换号判据）。"""
    root = cfg.path_of("bot.wechat_data_dir")
    d = root / "all_users" / "login" / wxid
    d.mkdir(parents=True, exist_ok=True)
    f = d / "key_info.db"
    f.write_text("x", encoding="utf-8")
    import os
    os.utime(f, (when, when))


def main():
    print("[账号目录名 → wxid]")
    check("带 4 位后缀", account.parse_account_dir("wxid_exampleowner_1f4c"), "wxid_exampleowner")
    check("不带后缀", account.parse_account_dir("wxid_abc123"), "wxid_abc123")
    check("长后缀也认", account.parse_account_dir("wxid_abc123_deadbeef"), "wxid_abc123")
    check("空值不炸", account.parse_account_dir(""), "")
    check("奇怪的名字原样返回", account.parse_account_dir("not-an-account"), "not-an-account")

    print("[来源① WeFlow 配置 myWxid（最准）]")
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = env(tmp, wf={"myWxid": "wxid_new_1a2b", "dbPath": str(tmp / "xwechat_files")},
              dirs=["wxid_old_9999", "wxid_new_1a2b"])       # 新号目录确实存在，才信 WeFlow
    wxid, why = account.detect_wxid(cfg)
    check("认到 myWxid", wxid, "wxid_new")
    check_true("来源说明里提到 myWxid", "myWxid" in why)

    print("[★换号场景：WeFlow 还指着老号，但登录记录已经是新号]")
    tmp_x = pathlib.Path(tempfile.mkdtemp())
    cfg_x = env(tmp_x, wf={"myWxid": "wxid_old_1a2b"},
                dirs=["wxid_old_1a2b", "wxid_new_9a8b"])
    now = time.time()
    add_login(cfg_x, "wxid_old", now - 4 * 3600)     # 老号：4 小时前登录过
    add_login(cfg_x, "wxid_new", now - 10)           # 新号：刚刚登录
    wxid_x, why_x = account.detect_wxid(cfg_x)
    check("用登录记录里的新号", wxid_x, "wxid_new")
    check_true("说明里点出 WeFlow 没跟着切", "没跟着切" in why_x and "wxid_old" in why_x)
    # 关掉登录记录判据时，退回 WeFlow + 数据目录逻辑
    cfg_x2 = env(pathlib.Path(tempfile.mkdtemp()), wf={"myWxid": "wxid_old_1a2b"},
                 dirs=["wxid_old_1a2b", "wxid_new_9a8b"])
    check("没有登录记录时按 WeFlow（它指的账号存在）",
          account.detect_wxid(cfg_x2)[0], "wxid_old")

    print("[WeFlow 指着不存在的账号 → 用数据目录里真有的那个]")
    tmp_y = pathlib.Path(tempfile.mkdtemp())
    cfg_y = env(tmp_y, wf={"myWxid": "wxid_ghost_1a2b"}, dirs=["wxid_real_9a8b"])
    got_y, why_y = account.detect_wxid(cfg_y)
    check("不用幽灵账号", got_y, "wxid_real")
    check_true("说明里解释换了哪个", "找不到这个账号" in why_y)

    print("[来源② wxidConfigs 只有一个账号]")
    tmp2 = pathlib.Path(tempfile.mkdtemp())
    cfg2 = env(tmp2, wf={"wxidConfigs": {"wxid_only_1f4c": {}}}, dirs=[])
    check("用唯一的那个", account.detect_wxid(cfg2)[0], "wxid_only")
    tmp2b = pathlib.Path(tempfile.mkdtemp())
    cfg2b = env(tmp2b, wf={"wxidConfigs": {"wxid_a_1f4c": {}, "wxid_b_1f4c": {}}},
                dirs=["wxid_c_aaaa"])
    check("两个账号时不用它、去扫目录", account.detect_wxid(cfg2b)[0], "wxid_c")

    print("[来源③ 扫微信数据目录，取最新的那个]")
    tmp3 = pathlib.Path(tempfile.mkdtemp())
    cfg3 = env(tmp3, wf={}, dirs=["wxid_older_1111", "wxid_newer_2222"])
    check("取 mtime 最新的", account.detect_wxid(cfg3)[0], "wxid_newer")
    check_true("来源里写明是数据目录", "数据目录" in account.detect_wxid(cfg3)[1])

    print("[认不出来时：不报错、退回配置]")
    tmp4 = pathlib.Path(tempfile.mkdtemp())
    cfg4 = env(tmp4, wf={}, dirs=[])
    check("认不到 → 空", account.detect_wxid(cfg4)[0], "")
    check_true("给个原因", "没读到" in account.detect_wxid(cfg4)[1])

    print("[resolve：自动用新的（用户口径）]")
    logs = []
    tmp5 = pathlib.Path(tempfile.mkdtemp())
    cfg5 = env(tmp5, wf={"myWxid": "wxid_new_1a2b"})
    cfg5.data["bot"]["username"] = "wxid_old"
    check("配置是老的 → 自动用新认到的", account.resolve_bot_wxid(cfg5, logs.append), "wxid_new")
    check_true("并且打了日志（换号不能悄悄发生）",
               logs and "wxid_new" in logs[0] and "wxid_old" in logs[0])
    cfg5.data["bot"]["username"] = "wxid_new"
    check("配置一致时不重复唠叨", account.resolve_bot_wxid(cfg5, logs.append), "wxid_new")
    check("只打了一条日志", len(logs), 1)
    cfg5.data["bot"]["username"] = ""
    check("配置为空也能自动补上", account.resolve_bot_wxid(cfg5, lambda m: None), "wxid_new")
    cfg5.data["bot"]["auto_detect"] = False
    cfg5.data["bot"]["username"] = "wxid_old"
    check("bot.auto_detect=false → 完全按配置", account.resolve_bot_wxid(cfg5, lambda m: None),
          "wxid_old")
    cfg6 = env(pathlib.Path(tempfile.mkdtemp()), wf={}, dirs=[])
    cfg6.data["bot"]["username"] = "wxid_keep"
    check("认不到 → 原样用配置（不倒退）", account.resolve_bot_wxid(cfg6, lambda m: None),
          "wxid_keep")

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
