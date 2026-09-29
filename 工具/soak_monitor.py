"""长跑验收监控（2026-09-28，M3 验收：连续 24h 无人干预 + 延迟中位数 ≤10s）。

它干三件事：
  1) 每 `--interval` 秒采样一次：面板/WeFlow 端口、面板进程内存、DB 计数、
     web.log 增量（回复耗时/失败行/SSE 掉线）；
  2) `--auto-restart`：面板进程真的死了（端口没人听）就把它拉起来（10 分钟冷却），
     并在采样里记一笔 `restarted`——长跑期间"挂了没人管"才是最大的假阴性来源；
  3) `--report`：把 jsonl 汇总成人看的报告（存活率、回复数、延迟中位数/p95、
     失败与"该回但错过了"、内存与磁盘趋势）。

用法（示例路径按需改）：
  python 工具\\soak_monitor.py --root <工程目录> --log <tmp\\web.log> --auto-restart
  python 工具\\soak_monitor.py --root <工程目录> --report

产物：`<root>/data/soak/soak.jsonl`（每行一个采样）+ `soak.log`（人类可读摘要）。
只读：本脚本只读 DB/日志，唯一的写动作是 `--auto-restart` 时拉起面板进程。
"""
import argparse
import ctypes
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ELAPSED_RE = re.compile(r"总耗时 ([\d.]+)s")
PORT_RE = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", re.M)
SOCK = socket.socket


# ---------- 基础探针 ----------

def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.5) -> bool:
    try:
        with SOCK() as s:
            s.settimeout(timeout)
            s.connect((host, port))
        return True
    except Exception:  # noqa: BLE001
        return False


def listen_pids() -> dict[int, int]:
    """{端口: PID}，解析 netstat -ano（不依赖 psutil）。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="ignore", timeout=20).stdout
    except Exception:  # noqa: BLE001
        return {}
    return {int(m.group(1)): int(m.group(2)) for m in PORT_RE.finditer(out)}


class _PMC(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def rss_mb(pid: int | None) -> float | None:
    if not pid:
        return None
    try:
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        h = k32.OpenProcess(0x1000, False, int(pid))          # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            pmc = _PMC()
            pmc.cb = ctypes.sizeof(pmc)
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return round(pmc.WorkingSetSize / 1048576, 1)
        finally:
            k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        pass
    return None


def dir_size_mb(path: Path) -> float:
    total = 0
    if path.exists():
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return round(total / 1048576, 1)


# ---------- 增量日志读 ----------

class Tail:
    """按 offset 增量读日志（只消费完整行；文件被轮转/截断时自动从头）。"""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0

    def read_new(self) -> list[str]:
        if not self.path.exists():
            return []
        size = self.path.stat().st_size
        if size < self.offset:                      # 被轮转/截断
            self.offset = 0
        if size == self.offset:
            return []
        with self.path.open("r", encoding="utf-8", errors="ignore") as f:
            f.seek(self.offset)
            data = f.read()
        last_nl = data.rfind("\n")
        if last_nl < 0:                             # 还没换行，下次再读
            return []
        keep = data[:last_nl + 1]
        # 按"只消费完整行"推进 offset（用字节数换算，避免半行被吃掉）
        self.offset = size - len(data.encode("utf-8", "ignore")) + \
            len(keep.encode("utf-8", "ignore"))
        return keep.splitlines()


# ---------- DB 统计 ----------

def db_stats(db_path: Path, since: float) -> dict:
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=5)
    except Exception:  # noqa: BLE001
        return {"error": "db-open-failed"}
    cur = con.cursor()

    def one(sql: str, args: tuple = ()):
        try:
            row = cur.execute(sql, args).fetchone()
            return row[0] if row and row[0] is not None else 0
        except Exception:  # noqa: BLE001
            return None

    st = {
        "replies_since": one("SELECT COUNT(*) FROM replies WHERE ts>=?", (since,)),
        "replies_ok_since": one("SELECT COUNT(*) FROM replies WHERE ts>=? AND ok=1", (since,)),
        "messages_new": one("SELECT COUNT(*) FROM messages WHERE status='new'"),
        "messages_claimed": one("SELECT COUNT(*) FROM messages WHERE status='claimed'"),
        "messages_expired": one("SELECT COUNT(*) FROM messages WHERE status='expired'"),
        "missed_should_reply": one(
            "SELECT COUNT(*) FROM messages WHERE status='expired'"
            " AND note LIKE '%该回但错过了%'"),
        "weird_keys": one("SELECT COUNT(*) FROM messages WHERE note LIKE '%数据异常%'"),
        # ★2026-09-28 评审（失败分类）：长跑要知道"失败死在哪一段"——发送/核对/模型/准备
        "fail_deliver": one("SELECT COUNT(*) FROM messages WHERE note LIKE 'deliver:%'"),
        "fail_verify": one("SELECT COUNT(*) FROM messages WHERE note LIKE 'verify:%'"),
        "fail_llm": one("SELECT COUNT(*) FROM messages WHERE note LIKE 'llm:%'"),
        "fail_prepare": one("SELECT COUNT(*) FROM messages WHERE note LIKE 'prepare:%'"),
        "memories": one("SELECT COUNT(*) FROM memories"),
        "replies_total": one("SELECT COUNT(*) FROM replies"),
    }
    con.close()
    return st


# ---------- 采样 + 自愈 ----------

def panel_cmd(root: Path) -> list[str]:
    """面板启动命令：先找工程内 venv，再找上一级（本机把 venv 放在任务目录的 tmp 下），
    都没有就用监控自己的解释器（它一定跑在能 import wxbot 的环境里）。"""
    for cand in (root / ".venv312" / "Scripts" / "python.exe",
                 root / ".venv" / "Scripts" / "python.exe",
                 root.parent / ".venv312" / "Scripts" / "python.exe",
                 root.parent / ".venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return [str(cand), "-m", "wxbot.cli", "web", "--port", "8765", "--seconds", "0"]
    return [sys.executable, "-m", "wxbot.cli", "web", "--port", "8765", "--seconds", "0"]


def user_env(name: str) -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v)
    except Exception:  # noqa: BLE001
        return ""


def restart_panel(root: Path, log_path: Path) -> str:
    env = dict(os.environ)
    if not env.get("LLM_CLOUD_KEY"):
        env["LLM_CLOUD_KEY"] = user_env("LLM_CLOUD_KEY")
    try:
        with log_path.open("a", encoding="utf-8", errors="ignore") as fh:
            subprocess.Popen(panel_cmd(root), cwd=str(root), env=env,
                             stdout=fh, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        return "spawned"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {type(exc).__name__}: {exc}"


def now_str() -> str:
    return datetime.now().strftime("%m-%d %H:%M:%S")


# ---------- 报告 ----------

def report(root: Path) -> int:
    jl = root / "data" / "soak" / "soak.jsonl"
    if not jl.exists():
        print(f"没有采样文件：{jl}（先跑一次监控）")
        return 1
    rows = []
    for line in jl.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:  # noqa: BLE001
            pass
    starts = sum(1 for r in rows if r.get("event") == "soak_start")
    rows = [r for r in rows if "ts" in r and "panel_listen" in r]
    if not rows:
        print("采样文件是空的")
        return 1
    first, last = rows[0], rows[-1]
    hours = (last["ts"] - first["ts"]) / 3600
    samples = len(rows)
    up = sum(1 for r in rows if r.get("panel_listen"))
    restarts = sum(1 for r in rows if r.get("restarted"))
    elapsed = [e for r in rows for e in (r.get("elapsed_s") or [])]
    elapsed.sort()

    def pct(p):
        if not elapsed:
            return None
        return elapsed[min(len(elapsed) - 1, int(len(elapsed) * p))]

    first_st, last_st = first.get("db") or {}, last.get("db") or {}
    # 统计口径：回复总数用库总计差（跨监控重启也准）；失败数用"当前监控段"的
    # （replies_since - replies_ok_since），因为 since 基线只在段内可比。
    total_delta = ((last_st.get("replies_total") or 0) - (first_st.get("replies_total") or 0))
    fails = ((last_st.get("replies_since") or 0) - (last_st.get("replies_ok_since") or 0))

    def delta(key):
        return (last_st.get(key) or 0) - (first_st.get(key) or 0)

    db_mb = [r.get("db_mb") for r in rows if r.get("db_mb")]
    # ★2026-09-29：内存必须**按 panel_pid 分段**看 —— 跨进程比较会算出假的"增长"
    # （实测踩过：起始值来自旧进程、结束值来自另一个进程，看起来涨了 68%，其实是重启后的两个进程）
    runs: list[list[dict]] = []
    for r in rows:
        if r.get("panel_rss_mb") is None:
            continue
        if runs and runs[-1][-1].get("panel_pid") == r.get("panel_pid"):
            runs[-1].append(r)
        else:
            runs.append([r])

    print("=" * 62)
    print(f"长跑报告　{first.get('iso')} → {last.get('iso')}　({hours:.1f} 小时 / {samples} 个采样)")
    print("=" * 62)
    print(f"面板存活率      ：{up}/{samples} = {100.0 * up / max(1, samples):.1f}%"
          f"　（自动拉起 {restarts} 次）")
    print(f"回复（长跑期间）：{total_delta} 条（按库总计差）"
          f"　当前监控段失败 {fails} 条　监控重启 {starts} 次")
    if elapsed:
        print(f"回复耗时        ：中位数 {pct(0.5):.1f}s　p90 {pct(0.9):.1f}s　最长 {elapsed[-1]:.1f}s"
              f"　（样本 {len(elapsed)} 条）")
    print(f"消息状态        ：待处理 {last_st.get('messages_new')}"
          f"／处理中 {last_st.get('messages_claimed')}"
          f"／过期 +{delta('messages_expired')}（当前存量 {last_st.get('messages_expired')}，"
          f"其中该回但错过 +{delta('missed_should_reply')}）")
    print(f"异常键/记忆     ：数据异常 +{delta('weird_keys')} 条"
          f"／记忆 {last_st.get('memories')} 条")
    print(f"失败分类（本段）  ：发送 +{delta('fail_deliver')}"
          f" ／ 核对 +{delta('fail_verify')}"
          f" ／ 模型 +{delta('fail_llm')}"
          f" ／ 准备 +{delta('fail_prepare')}"
          f"　（旧采样无此字段时按累计值算，偏保守）")
    if runs:
        cur = runs[-1]
        cur_rss = [r["panel_rss_mb"] for r in cur]
        peak_all = max(r["panel_rss_mb"] for r in rows if r.get("panel_rss_mb") is not None)
        span_h = (cur[-1]["ts"] - cur[0]["ts"]) / 3600.0
        if len(cur_rss) == 1:
            trend = f"当前 {cur_rss[0]} MB"
        else:
            trend = f"{cur_rss[0]} MB → {cur_rss[-1]} MB（本进程峰值 {max(cur_rss)} MB）"
        print(f"面板内存        ：本次进程（PID {cur[-1].get('panel_pid')}）{trend}"
              f"　已观测 {span_h:.1f} 小时 / {len(cur)} 个采样")
        if span_h < 2 or len(cur) < 5:
            print("                  ↳ 本次进程观测时长不够（<2 小时），还不能判断内存趋势")
        print(f"                  ↳ 历史峰值 {peak_all} MB（跨进程，只当上限参考，别拿来算增长率）")
    if db_mb:
        print(f"DB 大小         ：起始 {db_mb[0]} MB → 现在 {db_mb[-1]} MB")
    # 12 小时滚动段（看最近一段是否退化）
    recent = [r for r in rows if r["ts"] >= last["ts"] - 12 * 3600]
    r_up = sum(1 for r in recent if r.get("panel_listen"))
    r_el = [e for r in recent for e in (r.get("elapsed_s") or [])]
    if recent and r_el:
        r_el.sort()
        print(f"最近 12 小时    ：存活 {r_up}/{len(recent)}，回复 {len(r_el)} 条"
              f"，中位耗时 {r_el[len(r_el) // 2]:.1f}s")
    print("-" * 62)
    print("判定口径：存活率 ≥99%、中位耗时 ≤10s、失败与『该回但错过了』为 0 → 达标")
    print("内存只作观察：只看**同一进程内**的趋势（>=6 小时才有参考价值）")
    return 0


# ---------- 主循环 ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="工程目录（默认=本脚本上一级）")
    ap.add_argument("--log", default="", help="web.log 路径（默认 <root>/data/web.log）")
    ap.add_argument("--interval", type=float, default=300, help="采样间隔秒（默认 300）")
    ap.add_argument("--auto-restart", action="store_true", help="面板端口没人听时自动拉起")
    ap.add_argument("--include-backlog", action="store_true",
                    help="把启动前已有的日志也算进耗时统计（默认只统计启动后的新行）")
    ap.add_argument("--once", action="store_true", help="只采样一次（测试用）")
    ap.add_argument("--report", action="store_true", help="只出报告")
    args = ap.parse_args()

    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    if args.report:
        return report(root)
    log_path = Path(args.log) if args.log else root / "data" / "web.log"
    soak_dir = root / "data" / "soak"
    soak_dir.mkdir(parents=True, exist_ok=True)
    jsonl = soak_dir / "soak.jsonl"
    human = soak_dir / "soak.log"
    db_path = root / "data" / "wxbot.db"
    tail = Tail(log_path)
    if not args.include_backlog and log_path.exists():
        tail.offset = log_path.stat().st_size          # 长跑只统计"开始之后"的日志
    start = time.time()
    last_restart = 0.0

    print(f"[soak] 监控开始 root={root}")
    print(f"[soak] 日志={log_path}　采样间隔={args.interval}s　auto_restart={args.auto_restart}")
    print(f"[soak] 采样文件={jsonl}")
    first_line = json.dumps({"ts": start, "iso": datetime.fromtimestamp(start).strftime(
        "%Y-%m-%d %H:%M:%S"), "event": "soak_start", "root": str(root)}, ensure_ascii=False)
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(first_line + "\n")

    while True:
        ts = time.time()
        # 日志增量：先读，再采样（保证"本采样区间"的耗时都归到这一条）
        lines = tail.read_new()
        elapsed = []
        fails = sse_drops = paused = 0
        for ln in lines:
            m = ELAPSED_RE.search(ln)
            if m:
                elapsed.append(float(m.group(1)))
            if "❌" in ln:
                fails += 1
            if "SSE 断开，正在重试" in ln or "SSE 异常" in ln:
                sse_drops += 1
            if "看门狗：关键依赖不可用" in ln or "暂停发送" in ln:
                paused += 1

        pids = listen_pids()
        panel_up = 8765 in pids
        sample = {
            "ts": ts, "iso": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "panel_listen": panel_up, "weflow_listen": 5031 in pids,
            "panel_pid": pids.get(8765), "panel_rss_mb": rss_mb(pids.get(8765)),
            "elapsed_s": elapsed, "fail_lines": fails, "sse_drops": sse_drops,
            "watchdog_pauses": paused,
            "db": db_stats(db_path, start),
            "db_mb": round(db_path.stat().st_size / 1048576, 1) if db_path.exists() else None,
            "shots_mb": dir_size_mb(root / "data" / "shots"),
        }
        # 自愈：面板死了（端口没人听）就拉起来；10 分钟冷却，避免风暴
        if args.auto_restart and not panel_up and (ts - last_restart) > 600:
            result = restart_panel(root, log_path)
            sample["restarted"] = result
            last_restart = ts
            print(f"[soak] {now_str()} 面板不在，自动拉起：{result}")
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        st = sample["db"] or {}
        line = (f"{sample['iso']} 面板{'✅' if panel_up else '❌'} 内存{sample['panel_rss_mb']}MB "
                f"本次回复{st.get('replies_since')}(失败行{fails}) "
                f"新消息{st.get('messages_new')} 过期{st.get('messages_expired')} "
                f"耗时{elapsed} SSE掉线{sse_drops}")
        with human.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"[soak] {line}")
        if args.once:
            return 0
        time.sleep(max(30.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
