"""环境自检与**自动修复**：`doctor --fix`（2026-09-28 评审"产品化三件套"之二）。

把散落各处、反复踩过的探针收成一个闭环——出问题先跑它：
能修的自己修（WeFlow 没起/-105、Ollama 没起、依赖缺没缺），修不了的给下一步命令。

设计原则：**只做安全、幂等的修复**；任何一步失败都保留现场（日志/提示），
绝不静默改用户的系统（不删注册表、不装依赖、不动微信）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


# ---------- 基础探测 ----------

def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _hostport(base: str, default_port: int = 5031) -> tuple[str, int]:
    try:
        from urllib.parse import urlparse
        u = urlparse(base or "")
        return (u.hostname or "127.0.0.1", int(u.port or default_port))
    except Exception:  # noqa: BLE001
        return "127.0.0.1", default_port


def find_weflow_exe(cfg=None) -> pathlib.Path | None:
    """找 WeFlow.exe：配置优先 → 常见安装路径。找不到返回 None（不猜）。"""
    cands: list[pathlib.Path] = []
    if cfg is not None:
        p = str(cfg.get("ingest.weflow.exe_path") or "").strip()
        if p:
            cands.append(pathlib.Path(p))
    la = os.environ.get("LOCALAPPDATA")
    if la:
        cands.append(pathlib.Path(la) / "Programs" / "WeFlow" / "WeFlow.exe")
    cands += [pathlib.Path(r"D:\tool2\WeFlow\WeFlow.exe"),
              pathlib.Path(r"C:\Program Files\WeFlow\WeFlow.exe")]
    for c in cands:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return None


def find_ollama_app() -> pathlib.Path | None:
    la = os.environ.get("LOCALAPPDATA")
    cands = []
    if la:
        cands += [pathlib.Path(la) / "Programs" / "Ollama" / "ollama app.exe",
                  pathlib.Path(la) / "Programs" / "Ollama" / "ollama.exe"]
    for c in cands:
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return None


def find_repair_script(root: pathlib.Path) -> pathlib.Path | None:
    """WeFlow -105 的一键修复脚本（交付包里在 工具/ 下）。"""
    for p in (root / "工具" / "repair-weflow.ps1",
              root.parent / "工具" / "repair-weflow.ps1",
              root.parent.parent / "工具" / "repair-weflow.ps1",
              root / "repair-weflow.ps1"):
        try:
            if p.exists():
                return p
        except OSError:
            continue
    return None


def _wf_probe(base: str, token: str, timeout: float = 6.0) -> tuple[bool, str]:
    """直接打一次 WeFlow REST（不依赖 Sources，避免循环导入）。"""
    url = (base.rstrip("/") or "http://127.0.0.1:5031") + "/api/v1/sessions?limit=1"
    req = urllib.request.Request(url, headers=(
        {"Authorization": f"Bearer {token}"} if token else {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        n = len(data.get("sessions") or [])
        return True, f"可用（{n} 个会话）"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "ignore")[:120]
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {exc.code} {body}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:120]


# ---------- 修复动作 ----------

def fix_weflow(cfg, log) -> tuple[bool, str]:
    """WeFlow 没起 → 启动它；起了但读不了（-105 等）→ 跑修复脚本 → 复验。"""
    wf = cfg.get("ingest.weflow", {}) or {}
    base = str(wf.get("base_url") or "http://127.0.0.1:5031")
    token = str(wf.get("access_token") or "")
    host, port = _hostport(base)

    if not port_open(host, port):
        exe = find_weflow_exe(cfg)
        if not exe:
            return False, "WeFlow 没在跑，也找不到 WeFlow.exe（配置 ingest.weflow.exe_path 指给它）"
        log(f"   · 启动 WeFlow：{exe}")
        try:
            subprocess.Popen([str(exe)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            return False, f"启动 WeFlow 失败：{exc}"
        for _ in range(25):
            time.sleep(1)
            if port_open(host, port):
                break
        else:
            return False, "启动了 WeFlow 但 25 秒内端口还是不通"

    ok, detail = _wf_probe(base, token)
    if ok:
        return True, f"WeFlow {detail}"

    script = find_repair_script(pathlib.Path(__file__).resolve().parent.parent)
    if script:
        log(f"   · 跑修复脚本：{script.name}（重启 WeFlow，不动注册表）")
        try:
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(script)],
                           capture_output=True, timeout=120,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as exc:  # noqa: BLE001
            log(f"   · 修复脚本异常（继续复验）：{str(exc)[:80]}")
        time.sleep(4)
        ok2, detail2 = _wf_probe(base, token)
        if ok2:
            return True, f"修复脚本跑完，WeFlow {detail2}"
        return False, f"修复脚本跑完仍不可用：{detail2}（原始错误：{detail}）"
    return False, f"WeFlow 接口不可用：{detail}（也没找到 repair-weflow.ps1）"


def fix_ollama(cfg, log) -> tuple[bool, str]:
    """本地档位在用、但 11434 不通 → 尝试拉起 Ollama。"""
    active = str(cfg.get("llm.active") or "")
    prof = ((cfg.get("llm.profiles") or {}).get(active) or {})
    if str(prof.get("type") or "") != "ollama":
        return True, f"当前档位 {active or '(未设)'} 不是 ollama，跳过"
    host, port = _hostport(str(prof.get("base_url") or "http://127.0.0.1:11434"), 11434)
    if port_open(host, port):
        return True, "Ollama 在跑"
    exe = find_ollama_app()
    if not exe:
        return False, "Ollama 没在跑，也找不到安装位置（装一个：https://ollama.com）"
    log(f"   · 启动 Ollama：{exe}")
    try:
        subprocess.Popen([str(exe)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:  # noqa: BLE001
        return False, f"启动 Ollama 失败：{exc}"
    for _ in range(15):
        time.sleep(1)
        if port_open(host, port):
            return True, "Ollama 已拉起"
    return False, "启动了 Ollama 但 15 秒内端口不通（也许要手动开一次）"


def check_deps(log) -> list[str]:
    """依赖体检：缺哪个报哪个 + 一条 pip 命令（不自动装，改环境要用户点头）。"""
    need = {"yaml": "pyyaml", "mcp": "mcp", "PIL": "pillow"}
    missing = []
    for mod, pkg in need.items():
        if importlib.util.find_spec(mod) is None:
            missing.append(pkg)
    if importlib.util.find_spec("ddgs") is None:
        log("· 可选依赖 ddgs 没装（联网搜索会少几个后端）：pip install ddgs")
    if importlib.util.find_spec("trafilatura") is None:
        log("· 可选依赖 trafilatura 没装（抓网页正文不可用）：pip install trafilatura")
    if missing:
        log(f"❌ 缺必需依赖：{'、'.join(missing)} → pip install {' '.join(missing)}")
    return missing


def run_fixes(cfg, log) -> int:
    """跑全部修复动作，返回新增失败数（0 = 全好或全修好）。"""
    failed = 0
    log("— 自动修复（--fix）：WeFlow / Ollama / 依赖 —")
    ok, why = fix_weflow(cfg, log)
    log(f"   {'✅' if ok else '❌'} WeFlow：{why}")
    failed += 0 if ok else 1
    ok, why = fix_ollama(cfg, log)
    log(f"   {'✅' if ok else '❌'} Ollama：{why}")
    failed += 0 if ok else 1
    if check_deps(log):
        failed += 1
    return failed
