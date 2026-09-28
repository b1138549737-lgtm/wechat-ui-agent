"""风控相关（T360/T370）离线单测：风险提示与一次性确认、统一写闸门、间隔抖动等待。

跑法：python tests/test_risk.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from wxbot import cli as wxcli                        # noqa: E402
from wxbot.config import Config                       # noqa: E402
from wxbot.rules import gap_remaining, is_gap_reason, should_reply   # noqa: E402
from wxbot.store import Store                         # noqa: E402

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append(name)
    mark = "ok  " if got == want else "FAIL"
    extra = "" if got == want else f"  期望 {want!r} 实际 {got!r}"
    print(f"  {mark} {name}{extra}")


def check_true(name, got):
    check(name, bool(got), True)


def cfg_for(tmp: pathlib.Path, **limits) -> Config:
    base = {"per_contact_gap_seconds": 30, "jitter_ratio": 0.3, "burst_window": 120,
            "burst_max": 3, "per_contact_hourly": 10, "per_contact_daily": 60,
            "global_per_hour": 60, "global_per_day": 150, "proactive_gap_seconds": 15,
            "proactive_hourly": 20, "min_write_gap_seconds": 3, "write_wait_max_seconds": 8,
            "gap_wait_max_seconds": 8}
    base.update(limits)
    return Config({"app": {"data_dir": str(tmp / "data")}, "defaults": {"limits": base}})


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    cfg = cfg_for(tmp)

    print("[① 风险提示：文案带限额、带「自担风险」字样]")
    text = wxcli.risk_banner(cfg)
    check_true("说明是非官方手段", "非官方" in text)
    check_true("明确写了可能被封/下线", "封" in text or "限制" in text)
    check_true("把当前限额列出来（间隔 30s）", "≥30s" in text)
    check_true("提到主动消息限额", "主动消息" in text)
    check("还没确认时 is_risk_acked=False", wxcli.is_risk_acked(cfg), False)
    path = wxcli.ack_risk(cfg)
    check_true("确认文件写出来了", path.exists() and path.name == "ack.json")
    check("确认后 is_risk_acked=True", wxcli.is_risk_acked(cfg), True)
    # require_ack=true 且没确认 → 门禁要拦住
    tmp2 = pathlib.Path(tempfile.mkdtemp())
    cfg_req = cfg_for(tmp2)
    cfg_req.data["safety"] = {"require_ack": True}
    check("require_ack 且未确认 → 拦住", wxcli.pass_safety_gate(cfg_req), False)
    wxcli.ack_risk(cfg_req)
    check_true("确认后放行（结构/安全都过）", wxcli.pass_safety_gate(cfg_req))

    print("[② 统一写闸门：所有对外写动作都间隔开]")
    s = Store(tmp / "t.db")
    contact = {"name": "文件传输助手", "username": "filehelper", "self_ok": True}
    ok1, waited1 = wxcli.write_gate(cfg, s, contact, "指令回复")
    check("第一次直接放行", (ok1, waited1), (True, 0.0))
    s.add_reply("filehelper", "req", "刚发过的话", "command", True)
    t0 = time.time()
    ok2, waited2 = wxcli.write_gate(cfg, s, contact, "指令回复")
    took = time.time() - t0
    check_true("刚写完 → 等一小会儿再放行", ok2 and waited2 > 0)
    check_true("确实等了（≥2s，说明真的 sleep 了）", took >= 2.0)
    # 差太多就交给调用方，不在这里死等
    cfg_slow = cfg_for(pathlib.Path(tempfile.mkdtemp()),
                       min_write_gap_seconds=3600, write_wait_max_seconds=8)
    s2 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    s2.add_reply("filehelper", "r", "x", "command", True)
    t1 = time.time()
    ok3, waited3 = wxcli.write_gate(cfg_slow, s2, contact, "指令回复")
    check("差太多 → 不等待、直接拒绝", (ok3, round(time.time() - t1, 1)), (False, 0.0))
    check_true("并告诉调用方还差多久", waited3 > 3000)
    cfg_off = cfg_for(pathlib.Path(tempfile.mkdtemp()), min_write_gap_seconds=0)
    check("闸门关掉(0) → 永远放行",
          wxcli.write_gate(cfg_off, s2, contact, "x"), (True, 0.0))

    print("[③ 间隔抖动：只是「还差一点」时等一等，不再一律跳过]")
    s3 = Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")
    lim_small = {"per_contact_gap_seconds": 2, "jitter_ratio": 0, "burst_window": 0,
                 "burst_max": 0, "per_contact_hourly": 0, "per_contact_daily": 0,
                 "global_per_hour": 0, "global_per_day": 0}
    rule = {"trigger": {"mode": "always"}, "limits": lim_small}
    s3.add_reply("filehelper", "req", "刚回过", "local", True)
    ok, why = should_reply(s3, "filehelper", "文件传输助手", "在吗", False, rule,
                           is_self_chat=True)
    check("间隔不够 → 先判成不该回", (ok, is_gap_reason(why)), (False, True))
    rem = gap_remaining(s3, "filehelper", lim_small)
    check_true("算得出还差多久（0<w≤2）", 0 < rem <= 2.0)
    time.sleep(rem)
    ok2b, why2 = should_reply(s3, "filehelper", "文件传输助手", "在吗", False, rule,
                              is_self_chat=True)
    check("等够之后就能回", (ok2b, is_gap_reason(why2)), (True, False))
    check("别的限流原因不算「间隔不够」", is_gap_reason("限流：该会话每小时已达上限 10"), False)
    check("非限流原因也不算", is_gap_reason("没被 @"), False)
    check("间隔为 0 时 gap_remaining=0",
          gap_remaining(s3, "filehelper", {"per_contact_gap_seconds": 0}), 0.0)

    print(f"\n结果：通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
