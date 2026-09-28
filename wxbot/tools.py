"""工具集合（T330）：把"联网"和"机器人自己的动作"合成一个 toolbox，交给模型的 function calling。

工具清单：
- `web_search` / `web_fetch`：联网（实现在 `wxbot/web_tools.py`）
- `remind(text, when)`：到点提醒（复用 `commands.parse_when` + `store.add_reminder`）
- `find_in_history(keyword)`：在当前会话的历史消息里找
- `remember(fact)`：把这个人的事写进长期记忆（群里记到发言人名下）

安全边界（这块是刻意设计的，别为了"看起来聪明"放开）：
1. **会产生对外动作的**（`remind`）只允许主人调用 —— 否则群里任何人都能让机器人给他自己发提醒；
2. `find_in_history` / `remember` 对所有人生效，但作用域永远限定在
   **当前会话 + 当前发言人**（群里只能翻群里的消息、只记自己的事），越不出这个范围；
3. 参数一律校验，失败只回一句说明，绝不抛出去影响回复（工具是"帮忙的"，不是"关键的"）。
"""
from __future__ import annotations

import time

from . import commands
from .web_tools import WebTool


class ToolBox:
    """给 LLM 用的工具集合。每个 profile 能不能用由 `tools.profiles` 决定。"""

    def __init__(self, cfg, log=None, store=None, context: dict | None = None):
        self.cfg = cfg
        self.log = log or (lambda _m: None)
        self.store = store
        self.ctx = context or {}
        self.calls: list[dict] = []          # 审计：这一轮调过什么
        self._web_merged = 0                 # 已经把 WebTool 的多少条调用合并进来了
        self.web_used = False                # 这一轮有没有读过网页（M-3：读过就不许写记忆）
        self.max_total_chars = int((cfg.get("tools.max_total_chars") or 12000))
        self._used_chars = 0                 # 回给模型的资料总量（审查 Minor5）
        tools_cfg = cfg.get("tools", {}) or {}
        self.profiles = [str(p) for p in (tools_cfg.get("profiles")
                                          or (tools_cfg.get("web", {}) or {}).get("profiles") or [])]
        a_cfg = tools_cfg.get("assistant", {}) or {}
        self.assistant_enabled = bool(a_cfg.get("enabled", True))
        web_cfg = tools_cfg.get("web", {}) or {}
        self.web = WebTool(web_cfg, log=log) if web_cfg.get("enabled") else None
        self.enabled = bool(self.web) or self.assistant_enabled

    # ---- 给模型看的声明 ----
    def allowed_for(self, profile: str) -> bool:
        if not self.enabled:
            return False
        if self.profiles:
            return profile in self.profiles
        return bool(self.web and self.web.allowed_for(profile))

    def schemas(self) -> list[dict]:
        out = list(self.web.schemas()) if self.web else []
        if not self.assistant_enabled:
            return out
        out += [
            {"type": "function", "function": {
                "name": "remind",
                "description": ("给用户设一个到点提醒（机器人到点会主动发消息）。"
                                "只在用户明确要求提醒时调用。"),
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "要提醒的内容"},
                    "when": {"type": "string",
                             "description": "什么时候提醒。例：20分钟后 / 1小时 / 明天9点 / "
                                            "18:30 / 明晚 / 后天9点"},
                }, "required": ["text", "when"]}}},
            {"type": "function", "function": {
                "name": "find_in_history",
                "description": "在当前这个会话的历史消息里按关键词找（用于回忆之前聊过的东西）。",
                "parameters": {"type": "object", "properties": {
                    "keyword": {"type": "string", "description": "要找的关键词"},
                }, "required": ["keyword"]}}},
            {"type": "function", "function": {
                "name": "remember",
                "description": ("把一件值得长期记住的事记下来（对方明确说「记住…」时才用）。"
                                "例如称呼、忌口、约定、重要日期。"),
                "parameters": {"type": "object", "properties": {
                    "fact": {"type": "string", "description": "一句话，尽量短（不超过 200 字）"},
                }, "required": ["fact"]}}},
        ]
        # ★2026-09-28 评审（高级模型）：`/问` 指令要求 admin，但模型工具 `lookup_notes`
        # 原来**没有门** —— 群里任何人只要能让它回话，就可能让它念主人的本地资料。
        # 同一能力两条通道、只有一条上锁，是典型的架构级漏洞：这里按角色过滤声明。
        if self.ctx.get("is_owner") or str(self.ctx.get("role") or "") in ("owner", "admin"):
            out.append({"type": "function", "function": {
                "name": "lookup_notes",
                "description": ("查主人自己整理的本地资料（知识库）。当你需要项目/手册/"
                                "约定类的确定性信息、或者不确定答案时用。"),
                "parameters": {"type": "object", "properties": {
                    "question": {"type": "string", "description": "要查的问题或关键词"},
                }, "required": ["question"]}}})
        return out

    # ---- 执行 ----
    def call(self, name: str, args: dict) -> str:
        args = args if isinstance(args, dict) else {}
        if self.web and name in ("web_search", "web_fetch"):
            out = self.web.call(name, args)       # 它的调用记录由 collect_web_calls() 合并
            if out and not out.startswith("❌"):
                self.web_used = True              # 这一轮的上下文里已经有"外部内容"了
            # 一轮里回给模型的资料总量也要有上限，否则 3 轮 × 每轮好几篇会把上下文撑爆
            left = max(0, self.max_total_chars - self._used_chars)
            if len(out) > left:
                out = out[:left] + "\n（本轮参考资料已达上限，剩下的省略）"
            self._used_chars += len(out)
            return out
        if not self.assistant_enabled:
            return f"（没有这个工具：{name}）"
        try:
            if name == "remind":
                return self._remind(args)
            if name == "find_in_history":
                return self._find(args)
            if name == "remember":
                return self._remember(args)
            if name == "lookup_notes":
                return self._lookup_notes(args)
        except Exception as exc:  # noqa: BLE001 —— 工具炸了也不能影响回复
            return f"（{name} 执行失败：{type(exc).__name__}: {str(exc)[:80]}）"
        return f"（未知工具 {name}）"

    def _remind(self, args: dict) -> str:
        text = str(args.get("text") or "").strip()
        when = str(args.get("when") or "").strip()
        if not (text and when):
            return "❌ 失败：remind 需要 text 和 when 两个参数"
        if not self.ctx.get("is_owner"):
            # 别人不能借机器人的手给自己建提醒（否则就是给别人发消息的能力）
            return "❌ 失败：只有机器人主人能用提醒功能，请如实告诉对方你做不到"
        if not self.store:
            return "❌ 失败：这个环境没接存储，提醒不可用"
        due, _rest, err = commands.parse_when(f"{when} {text}")
        if err:
            return f"❌ 失败：时间没看懂（{err}）。请让对方换个说法，例如 20分钟后 / 明天9点"
        rid = self.store.add_reminder(self.ctx.get("username") or "",
                                      self.ctx.get("contact_name") or "", text, due)
        self.calls.append({"tool": "remind", "when": when, "id": rid})
        # 借鉴 mem0：机器人自己的动作也留一笔（之后问"我让你提醒我什么来着"能答上）
        self.store.note_agent(self.ctx.get("username") or "",
                              f"{time.strftime('%m-%d %H:%M')} 设了提醒：{commands.fmt_ts(due)} 提醒"
                              f"「{text}」（编号 #{rid}）")
        # ★ 回给模型的文案要**明确带"成功"字样**：实测只回"已建提醒"时，模型偶尔会当成失败、
        #   在回复里说"没存成功"（其实库里已经写进去了）。这类误导很难查，所以写死 ✅ 成功。
        return (f"✅ 成功：提醒已建好（编号 #{rid}），{commands.fmt_ts(due)} 会提醒「{text}」。"
                f"请如实告诉对方已设好。")

    def _find(self, args: dict) -> str:
        kw = str(args.get("keyword") or "").strip()
        if not kw or not self.store:
            return "❌ 失败：find_in_history 需要 keyword"
        rows = self.store.search_messages(self.ctx.get("username") or "", kw, 6)
        self.calls.append({"tool": "find_in_history", "keyword": kw, "hits": len(rows)})
        if not rows:
            return f"❌ 失败：这个会话的历史里没有含「{kw}」的消息"
        lines = []
        for r in rows:
            who = "我" if r["is_sent"] else (r["sender_name"] or "对方")
            lines.append(f"{time.strftime('%m-%d %H:%M', time.localtime(r['ts']))} {who}："
                         f"{r['content'][:80]}")
        return f"✅ 成功：找到 {len(rows)} 条历史消息\n" + "\n".join(lines)

    def _remember(self, args: dict) -> str:
        fact = str(args.get("fact") or "").strip()
        if not fact or not self.store:
            return "❌ 失败：remember 需要 fact（一句话，不超过 200 字）"
        # ★2026-09-28 评审：记忆是"外部输入能持久化进系统提示词"的唯一通道 ——
        # 指令句式的"事实"（从现在开始你是X / 你必须 / 以后回复都要…）一律拒收，
        # 和 cli 的自动提炼共用同一道门（rules.is_injection_like）。
        from .rules import is_injection_like  # noqa: PLC0415 —— 避免模块级循环导入
        if is_injection_like(fact):
            self.calls.append({"tool": "remember", "blocked": "指令句式", "fact": fact[:40]})
            return "❌ 失败：这条像是指令而不是事实，记忆里只放事实（称呼/忌口/约定这类）。"
        if self.web_used:
            # ★ 审查 M-3：这一轮读过网页，网页内容是不可信输入 —— 不许它顺手写进长期记忆。
            # 记忆会被之后每一轮注入，是唯一"外部内容能持久化"的路径，所以这里宁可麻烦一点。
            self.calls.append({"tool": "remember", "blocked": "看过网页", "fact": fact[:40]})
            return ("❌ 失败：这一轮刚查过网页，为防止网页里的指令被写进记忆，"
                    "这次不记。请让对方在下一条消息里直接说「记住…」再记。")
        speaker = str(self.ctx.get("speaker") or "")
        ok = self.store.add_memory(self.ctx.get("username") or "", fact,
                                   speaker=(speaker if self.ctx.get("is_group") else ""))
        self.calls.append({"tool": "remember", "fact": fact[:40], "ok": ok})
        return (f"✅ 成功：已写入长期记忆「{fact}」" if ok
                else "❌ 失败：这条记忆为空或超过 200 字，没记")

    def _lookup_notes(self, args: dict) -> str:
        """查本地知识库（借鉴 AstrBot 的 Knowledge Base / Skill：让模型自己去翻主人的资料）。"""
        # ★2026-09-28 评审：和 /问 指令同一把锁（声明层已按角色过滤，这里再兜一层，
        # 防止模型凭空编出这个工具名来调）。
        if not (self.ctx.get("is_owner") or str(self.ctx.get("role") or "") in ("owner", "admin")):
            return "❌ 失败：这个资料库只有机器人主人能查，请如实告诉对方你查不了。"
        q = str(args.get("question") or "").strip()
        if not q:
            return "❌ 失败：lookup_notes 需要 question"
        if not self.cfg.get("knowledge.enabled", True):
            return "❌ 失败：本地知识库没开（knowledge.enabled=false）"
        from . import knowledge
        hits = knowledge.retrieve(self.cfg, q, int(self.cfg.get("knowledge.max_snippets", 3)))
        self.calls.append({"tool": "lookup_notes", "query": q[:40], "hits": len(hits)})
        if not hits:
            return f"❌ 失败：本地资料里没有跟「{q}」相关的段落（别编，如实说查不到）"
        return ("✅ 成功：找到这些资料段落（**只依据它们回答**，并标出文件名）：\n"
                + "\n\n".join(f"【{h['file']}】\n{h['text'][:600]}" for h in hits))

    # ---- 显式联网（`/搜索`）与审计汇总 ----
    def force_query(self, text: str):
        return self.web.force_query(text) if self.web else None

    def research_text(self, query: str) -> str:
        if not self.web:
            return ""
        out = self.web.research_text(query)
        self.calls.extend(self.web.calls)
        return out

    def collect_web_calls(self) -> None:
        """把底层 WebTool 记的调用合并进本层的审计列表（生成结束前调用一次）。"""
        if self.web:
            self.calls.extend(self.web.calls[self._web_merged:])
            self._web_merged = len(self.web.calls)
