"""配置加载：YAML + 环境变量占位 + 全局默认/每会话覆盖。"""
from __future__ import annotations

import copy
import os
import re
import pathlib
from typing import Any

import yaml

ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
WINVAR_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


def _expand(value: Any) -> Any:
    """把 ${VAR} 替换成环境变量；同时支持 %APPDATA% 这类 Windows 变量。"""
    if isinstance(value, str):
        value = ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _scan_placeholders(value: Any, path: str = "", found: list | None = None) -> list[str]:
    """找出展开后仍残留的占位符（${VAR} 或 %VAR%），用于启动告警。"""
    found = found if found is not None else []
    if isinstance(value, str):
        for m in ENV_RE.finditer(value):
            found.append(f"{path}: ${{{m.group(1)}}} 未解析（环境变量没设置？）")
        for m in WINVAR_RE.finditer(value):
            found.append(f"{path}: %{m.group(1)}% 未展开")
    elif isinstance(value, dict):
        for k, v in value.items():
            _scan_placeholders(v, f"{path}.{k}" if path else k, found)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_placeholders(v, f"{path}[{i}]", found)
    return found


def _scan_missing_env(text: str) -> list[str]:
    """扫**原始 YAML 文本**里的 ${VAR} / %VAR%，报告本机没设的那些。

    为什么必须扫原文：`_expand()` 会把缺失的 ${VAR} 静默换成空串，
    于是"展开后再扫"永远扫不到 —— 表现就是"配置看起来正常，一调云端就 401"。
    """
    out: list[str] = []
    seen: set[str] = set()
    for m in ENV_RE.finditer(text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        if not os.environ.get(name):
            out.append(f"配置里的 ${{{name}}} 环境变量本机没有值 —— 会被当成空串"
                       f"（云端 profile 会直接 401），要么设上它，要么直接写明文/删掉该 profile")
    for m in WINVAR_RE.finditer(text):
        name = m.group(1)
        if name in seen:
            continue
        seen.add(name)
        if not os.environ.get(name):
            out.append(f"配置里的 %{name}% 在本机不存在，展开后会保留字面量")
    return out


def deep_merge(base: dict, override: dict) -> dict:
    """override 覆盖 base（递归），返回新字典。"""
    out = copy.deepcopy(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


class Config:
    def __init__(self, data: dict, path: pathlib.Path | None = None):
        self.data = data
        self.path = path
        self.base_dir = (path.parent if path else pathlib.Path.cwd())
        self.warnings = _scan_placeholders(data)

    # ---- 载入 ----
    @classmethod
    def load(cls, path: str | pathlib.Path) -> "Config":
        p = pathlib.Path(path).expanduser().resolve()
        text = p.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        cfg = cls(_expand(raw), p)
        # 原文先扫一遍（缺失的环境变量），再扫展开后的残留
        cfg.warnings = _scan_missing_env(text) + cfg.warnings
        return cfg

    # ---- 取值 ----
    def get(self, dotted: str, default=None):
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path_of(self, dotted: str, default: str = "") -> pathlib.Path:
        """把配置里的相对路径解析到工程目录下。"""
        val = self.get(dotted, default) or default
        p = pathlib.Path(val).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    # ---- 会话 ----
    def contacts(self, enabled_only: bool = True) -> list[dict]:
        out = []
        for c in self.get("contacts", []) or []:
            if enabled_only and not c.get("enabled", True):
                continue
            out.append(c)
        return out

    def contact_by_username(self, username: str) -> dict | None:
        for c in self.contacts(enabled_only=False):
            if c.get("username") == username:
                return c
        return None

    def contact_by_name(self, name: str) -> dict | None:
        for c in self.contacts(enabled_only=False):
            if c.get("name") == name:
                return c
        return None

    # ---- 生效规则：默认 ← 会话覆盖 ----
    def effective(self, contact: dict | None) -> dict:
        base = {
            "trigger": self.get("defaults.trigger", {}) or {},
            "limits": self.get("defaults.limits", {}) or {},
            "persona": self.get("defaults.persona", {}) or {},
            "reply": self.get("defaults.reply", {}) or {},
            # ★2026-09-28：低俗熔断等安全开关也要能按会话覆盖（示例群C要关掉）
            "safety": self.get("safety", {}) or {},
        }
        if not contact:
            return base
        override = {k: contact[k] for k in ("trigger", "limits", "persona", "reply", "safety")
                    if k in contact}
        return deep_merge(base, override)

    def llm_profiles(self) -> tuple[str, list[str], dict]:
        active = self.get("llm.active", "local")
        fallback = self.get("llm.fallback", []) or []
        profiles = self.get("llm.profiles", {}) or {}
        return active, fallback, profiles

    # ---- 安全门禁（B1）----
    def validate_safety(self) -> list[str]:
        """返回问题列表（空 = 通过）。
        规则：启用任何"非自聊"会话（真实联系人/群）时，必须
          ① 有完整显示名（发送时按这个名字搜索与校验）② 开启标题校验 ③ 开启完整名校验。
        目的：不允许出现"只靠前两个字匹配"就把消息发出去的场景。"""
        problems = []
        if not self.get("send.verify_title", True):
            problems.append("send.verify_title 被关闭：无法防止发错人")
        if not self.get("send.verify_full_name", True):
            problems.append("send.verify_full_name 被关闭：只靠前缀匹配不够安全")
        for c in self.contacts(enabled_only=True):
            if c.get("self_ok"):
                continue                       # 自聊会话（文件传输助手）不涉及发错人
            name = (c.get("name") or "").strip()
            if len(name) < 2:
                problems.append(f"会话 {c.get('username')} 启用但缺少完整显示名（name）")
            if not c.get("username"):
                problems.append(f"会话 {name or '?'} 缺少 username，无法读消息")
        return problems

    # ---- 结构校验（Minor：启动就报出关键键缺失/写错位置）----
    def validate_structure(self) -> list[str]:
        problems = []
        required = ["app.data_dir", "llm.active", "llm.profiles",
                    "ingest.source", "send.verify_full_name", "defaults.limits", "contacts"]
        for key in required:
            if self.get(key) in (None, {}, []):
                problems.append(f"缺少配置项: {key}")
        active = self.get("llm.active")
        profiles = self.get("llm.profiles") or {}
        if active and active not in profiles:
            problems.append(f"llm.active={active} 在 llm.profiles 里不存在")
        for fb in (self.get("llm.fallback") or []):
            if fb not in profiles:
                problems.append(f"llm.fallback 里的 {fb} 不存在")
        # ★2026-09-29 评审"方向 1：档位 / 工具 / 权限 三张表要自洽"：
        #   tools.profiles 与 tools.web.profiles 里写的名字必须是 llm.profiles 里**真实存在**的档位 ——
        #   拼错一个字母不会报错，只会静默变成"这个档位永远不允许调工具"（评审说的"不自洽"就是这种）。
        for key in ("tools.profiles", "tools.web.profiles"):
            names = self.get(key)
            if names is None:
                continue
            if not isinstance(names, list):
                problems.append(f"{key} 应该是列表，现在是 {type(names).__name__}")
                continue
            for n in names:
                if str(n) not in profiles:
                    problems.append(f"{key} 里的 {n!r} 在 llm.profiles 里不存在（拼错？）")
        for c in self.contacts(enabled_only=True):
            if not c.get("name") or not c.get("username"):
                problems.append(f"contacts 里有一项缺 name/username: {str(c)[:80]}")
        # ★死配置检查（2026-09-26 实测踩到：模板里 persona/reply 被写到了 `safety:` 下面，
        #   而代码只读 `defaults.persona` / `defaults.reply` → 那两段完全没生效，
        #   启动却一声不响。写错层级的配置比缺配置更坑，宁可报出来。）
        known_defaults = {"trigger", "limits", "persona", "reply"}
        for k in (self.get("defaults") or {}):
            if k not in known_defaults:
                problems.append(f"defaults.{k} 不是已知配置项（不会生效）；"
                                f"已知：{'/'.join(sorted(known_defaults))}")
        # ★2026-09-28：safety 段后来长出了 lowbrow_filter（低俗熔断）——白名单要跟着长，
        # 否则新加的开关会被自己人判成"未知键"（doctor --fix 实测抓到这条）。
        known_safety = {"require_ack", "lowbrow_filter"}
        for k in (self.get("safety") or {}):
            if k not in known_safety:
                problems.append(f"safety.{k} 不是已知配置项（不会生效）；"
                                f"已知：{'/'.join(sorted(known_safety))}，"
                                f"persona/reply 要写在 defaults 下面")
        return problems

    def hints(self) -> list[str]:
        """非阻塞提示（不算错误，只在启动时提醒）。"""
        out = []
        groups = [c for c in self.contacts()
                  if str(c.get("username") or "").endswith("@chatroom")]
        if groups and not self.get("bot.names") and not self.get("bot.username"):
            out.append("启用了群聊但 bot.names / bot.username 都是空的："
                       "被 @ 和「引用我的消息」都可能匹配不到（运行时会尝试自动取群昵称，填上更稳）")
        if groups and not self.get("bot.username"):
            out.append("bot.username 未填：会在启动时**自动认当前登录账号**（认不到才需要手填）；"
                       "自动认号关闭时，引用触发只能靠「引用原文与我最近的回复一致」兜底")
        if not self.get("ingest.weflow.access_token"):
            out.append("未配置 WeFlow access_token：SSE 实时推送不可用，将退回轮询")
        if self.get("vision.enabled") and not self.get("vision.model"):
            out.append("vision.enabled 打开了但 vision.model 是空的：图片理解不会生效"
                       "（先 ollama pull 一个多模态模型再填进来）")
        # ★2026-09-29（评审方向 1）：一眼看出"当前档位到底会不会自己调工具"。
        # 本地小模型不列进 tools.profiles 是**故意的**（它不会调、还占显存），所以只在
        # 当前档位不是 ollama 时提示，避免正常配置也天天刷一行。
        listed = self.get("tools.profiles") or self.get("tools.web.profiles") or []
        active_p = str(self.get("llm.active") or "")
        _, _, _profs = self.llm_profiles()
        is_local = str(((_profs or {}).get(active_p) or {}).get("type") or "").lower() == "ollama"
        if listed and active_p and not is_local and active_p not in [str(x) for x in listed]:
            out.append(f"当前档位 {active_p} 不在 tools.profiles（{listed}）里 → 它不会自己调工具"
                       f"（联网/提醒/记忆）；想让它调就把 {active_p} 加进这个列表")
        return out

    # 推理模型名单：这些模型不关思考会把 token 全花在 reasoning 上，正文就空了
    REASONING_MODELS = ("qwen3", "qwq", "deepseek-r1", "r1-", "-r1", "reasoning",
                        "o1-", "o3-", "glm-z", "magistral")

    def reasoning_model_warnings(self) -> list[str]:
        """T204：ollama profile 用了推理模型却没设 think: false → 一定要提醒（会静默空正文）。"""
        out = []
        _, _, profiles = self.llm_profiles()
        for name, profile in (profiles or {}).items():
            if str((profile or {}).get("type", "")).lower() != "ollama":
                continue
            model = str((profile or {}).get("model") or "").lower()
            if not model:
                continue
            if any(k in model for k in self.REASONING_MODELS) and \
                    (profile or {}).get("think") is not False:
                out.append(f"llm.profiles.{name} 用的是推理模型 {model} 但没设 think: false —— "
                           f"它会先把 token 花在思考上，正文会是空的（实测踩过）")
        return out
