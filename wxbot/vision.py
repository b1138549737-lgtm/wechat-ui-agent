"""图片理解（T214 第二步）：把图片交给本地视觉模型，拿回一段文字描述。

设计取舍：
1. 只产出**一段描述文本**，接进正常回复流程 —— 回复模型不必支持图片，本地/云端都能用；
2. 任何失败（模型没装、超时、返回格式不对、文件不存在）都**安静返回空串**，
   调用方拿不到描述就按"忽略非文本"的老路走，绝不因为看图失败打断回复；
3. 走 Ollama 原生 `/api/chat`，带 `think: false`（推理模型不关思考会把 token 全花在思考上，
   这个坑在正文生成上已经踩过一次）。

实测注意：gemma4:e4b 能吃图片（Ollama 日志里 `image decoded` 正常），但 120 个 token 全进了
`message.thinking`，`message.content` 是空的 —— 所以这里 content 为空时回退取 thinking。

★ 2026-09-26 起支持两类后端：
- `provider: ollama`（默认，本地）：走原生 `/api/chat` + `think:false`；
- `provider: openai`（云端）：走 OpenAI 兼容的 `/chat/completions`，图片用 `image_url` data URI。
  实测 `deepseek-flash` 是多模态且 1.5 秒能出描述 —— 用云端看图就不必在显存里再塞一个视觉模型
  （8G 卡上本地 qwen + gemma 装不下，来回换模型要 15 秒）。
  ⚠️ 推理型模型必须给足 `max_tokens`（实测 200 会被思考链吃光、正文为空，1500 正常）。
"""
from __future__ import annotations

import base64
import json
import pathlib
import re
import urllib.request

# ★2026-09-27 改：原来只说"用一两句客观描述" —— 模型就给个"这是游戏界面"这种泛泛之谈，
# 甚至把《暗区突围》认成《魔兽战纪》《荒野行动》，把卡牌名"引路仙人"读成"李连杰"，
# 于是回复模型拿着错的关键词去搜梗 → "查不到"。改成**先定性、再逐字读图上文字**，
# 实测同一张图一次就对（"暗区突围/引路仙人/猛攻/50分解/（请输入文本）"全对）。
DEFAULT_PROMPT = ("先用一句话说清这是什么（什么游戏/什么界面/图里主体是什么），"
                  "然后把图里**能看清的文字逐字写出来**（标题、名字、数字、按钮、水印都算；"
                  "没有文字就跳过这一步）。看不清的字写「?」，不要猜、不要编、不要客套、不要 Markdown。")

# 推理模型（实测 gemma4:e4b）会把 thinking 一起吐出来，开头常有这类元话语，反复剥掉
_INTRO = re.compile(r"^(好的|好吧|好|下面|我来描述一下|我来|我来给出|这是一张图片)"
                    r"[，,：:、\s]*")


def _clean(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    prev = None
    while prev != text:                       # "好的，我来描述一下：" 这种要连剥两次
        prev = text
        text = _INTRO.sub("", text).strip()
    return text[:max_chars].strip() if max_chars > 0 else text


def describe_image(path: str | pathlib.Path, model: str,
                   base_url: str = "http://127.0.0.1:11434",
                   prompt: str = DEFAULT_PROMPT, max_tokens: int = 200,
                   timeout: float = 60, max_chars: int = 120,
                   provider: str = "ollama", api_key: str = "",
                   reasoning_effort: str = "none") -> str:
    """返回中文描述；任何异常都返回空字符串（调用方据此安静跳过）。

    ★2026-09-27 踩到（真机）：`deepseek-flash` 会**把 max_tokens 全烧在推理上** ——
    实测一次 `finish_reason=length`、`completion_tokens=1500` 里 `reasoning_tokens=1500`、
    `content=''`，于是我们拿到空描述，群里表现成"图里的字我看不清 / 认成别的游戏"。
    所以这里：① 空正文且是 openai 兼容后端 → **再要一次**（把预算翻倍、要它简短点）；
    ② 还是空就用 `reasoning_content` 兜底（推理里通常已经写出了看图结果）；
    ③ 万一 reasoning 里也是一堆废话，就照旧返回空串，让上层安静跳过。
    """
    is_openai = (provider or "ollama").lower() == "openai"
    tries: list[dict] = []
    if reasoning_effort:
        # ★实测最省最好的一条：`reasoning_effort: none`（1.7s、正文 292 字、零思考 token；
        # 不带它则 17.7s 全烧思考、正文空）
        tries.append({"prompt": prompt, "max_tokens": max_tokens,
                      "reasoning_effort": reasoning_effort})
    tries.append({"prompt": prompt, "max_tokens": max_tokens, "reasoning_effort": ""})
    if is_openai:
        tries.append({"prompt": ("只用一两句中文说明这张图是什么，并把图上能看清的文字写出来；"
                                 "不要推理过程、不要解释。看不清的字写「?」。"),
                      "max_tokens": max(int(max_tokens) * 3, 8000), "reasoning_effort": ""})
    for t in tries:
        out = _describe_once(path, model, base_url, t["prompt"], t["max_tokens"],
                             timeout, max_chars, provider, api_key,
                             t.get("reasoning_effort", ""))
        if out:
            return out
    return ""


def _describe_once(path: str | pathlib.Path, model: str,
                   base_url: str = "http://127.0.0.1:11434",
                   prompt: str = DEFAULT_PROMPT, max_tokens: int = 200,
                   timeout: float = 60, max_chars: int = 120,
                   provider: str = "ollama", api_key: str = "",
                   reasoning_effort: str = "") -> str:
    """真正发一次请求（describe_image 会用它重试/兜底）。"""
    p = pathlib.Path(str(path))
    if not model or not p.exists():
        return ""
    try:
        if p.stat().st_size > 8 * 1024 * 1024:      # 太大的图不硬塞，浪费 token 还容易超时
            return ""
        b64 = base64.b64encode(p.read_bytes()).decode()
    except Exception:  # noqa: BLE001
        return ""
    base = base_url.rstrip("/")
    if (provider or "ollama").lower() == "openai":
        url = f"{base}/chat/completions"
        payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.0,
                   "messages": [{"role": "user", "content": [
                       {"type": "text", "text": prompt},
                       {"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}]}
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        headers = {"Content-Type": "application/json",
                   **({"Authorization": f"Bearer {api_key}"} if api_key else {})}
    else:
        url = f"{base}/api/chat"
        payload = {
            "model": model, "stream": False, "think": False,
            "messages": [{"role": "user", "content": prompt, "images": [b64]}],
            "options": {"num_predict": max_tokens, "temperature": 0.0},
        }
        headers = {"Content-Type": "application/json"}
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    if (provider or "ollama").lower() == "openai":
        choices = data.get("choices") or [{}]
        msg = (choices[0] or {}).get("message") or {}
    else:
        msg = data.get("message") or {}
    return _clean(msg.get("content") or msg.get("thinking")
                  or msg.get("reasoning_content") or "", max_chars)
