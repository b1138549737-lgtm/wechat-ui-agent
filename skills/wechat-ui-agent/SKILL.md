---
name: wechat-ui-agent
description: 帮用户在自己的 Windows 电脑上安装、配置、运行、排查 wxbot（仓库名 wechat-ui-agent）——一个在本机微信 PC 版上做界面自动化收发的 AI 助理；也用于改它的配置（加群、换模型、调限流、改人设）和读日志定位"为什么不回消息 / 为什么没发出去"。不适用于微信以外的聊天软件，也不适用于其他微信自动化项目。
metadata:
  short-description: 安装与运维本机微信 AI 助理
---

# wechat-ui-agent（wxbot）上手与运维

帮用户把 wxbot 装好、配好、跑起来，并在出问题时**按证据**定位。以下路径都相对仓库根目录；Windows + PowerShell 环境。

## 红线（做任何操作前先记住）

1. **只往用户明确允许的会话发消息。** 要验证发送链路，只用「文件传输助手」（自聊）或用户点名的测试会话；**绝不**擅自往任何群 / 联系人发"测试消息"。
2. **不要削弱发送三层校验**（唯一候选 + 完整名 + 发送前回读），也不要为了"让它跑通"去关校验、放宽判据。
3. 改 `config.yaml` 前先备份；改完跑一次 `doctor` 复核；会改变对外行为的改动先 `--dry-run` 或面板"试跑"。
4. 面板链接里的 `#token=`、WeFlow 的 `access_token`、云端模型 key，都不进任何公开渠道（截图 / issue / 转发都不行）。
5. 聊天内容只在本机 `data\`。要用云端模型处理用户的内容前，先确认用户知道并同意（云端档位 `allow_context` 默认不开上下文）。

## 先读这三份，再动手

- 完整手册（配置全表、发送校验、故障排查、长跑验收）：公开仓库里是 `docs/GUIDE.md`，
  交付目录里是 `README.md`。
- `config.example.yaml` —— 每个配置项都有中文注释；**改配置前先在这里找对应项**，不要凭记忆猜字段名。
- `README.md` —— 快速开始与 FAQ。

## 首次搭起来

前提：Windows 10/11 桌面会话、微信 PC 版已登录、WeFlow 在运行（默认 `http://127.0.0.1:5031`）。

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1     # 建 .venv、装依赖、生成 config.yaml、自检
$py = ".\.venv\Scripts\python.exe"

& $py -m wxbot.cli whoami                           # ① 认号：本号 wxid + 各群里的昵称（@ 判据）
& $py -m wxbot.cli ack-risk                         # ② 看风险提示并确认（只做一次）
& $py -m wxbot.cli doctor --with-llm --with-tools   # ③ 自检：窗口 / 读端 / 模型 / 联网 / 发送通路
```

然后陪用户改 `config.yaml` 的三处必改（细节看 README / GUIDE）：模型档位（`llm.profiles`）、
WeFlow token（`ingest.weflow.access_token`）、白名单（`contacts`）。

**先试跑再正式跑**：

```powershell
& $py -m wxbot.cli run --seconds 90 --dry-run      # 试跑：只生成、不发送 —— 确认名单与语气
& $py -m wxbot.cli web --port 8765                 # 正式：面板 + 常驻循环（推荐）；看启动时打印的带 #token= 的地址
```

## 用户常见请求 → 怎么做

- **加一个群 / 联系人**：在 `contacts` 加一项 —— `name` 写微信里显示的名字（备注 / 昵称 / 群名都认），
  群 `username` 形如 `<数字>@chatroom`；改完 `doctor` 复核，再用 `whoami` 确认群昵称对得上。
- **换模型 / 加档位**：改 `llm.profiles` 与 `active` / `fallback`；云端 key 建议用环境变量占位（如 `${LLM_CLOUD_KEY}`）。
- **嫌话多 / 嫌话少**：调 `limits`（回复间隔、每小时上限、连点合并窗口）与群的 `trigger.mode`。
- **改性格**：`persona`（或在微信里发 `/人设`；权限：owner / admin）。
- **看运行状态**：面板（日志 / 试跑 / 暂停）或微信里发 `/状态`、`/近况`；指令全集发 `/帮助`。
- **"它怎么不回消息"**：按 `references/diagnostics.md` 的清单逐条对。

## 出问题的排查顺序（30 秒版；详见 references/diagnostics.md）

1. 先分清**没回**还是**回了没发出去**：日志里找 `跳过：…`（没回，带原因）还是 `❌`（发送失败）。
2. 没回 → 依次查：有没有 @ / 引用（mention 模式）→ 在不在时间窗 → 是不是被限流（日志会写原因）→
   会话在不在 `contacts` → 消息是不是超过 `ingest.backlog_max_age_seconds` 被丢弃。
3. 发不出去 → `doctor` 的会话名体检；微信窗口别拖太窄；WeFlow 报 `-105` 用 `doctor --fix` 或 `工具\repair-weflow.ps1`。
4. 改完代码 / 配置后跑离线回归（不碰微信、不花钱）：

```powershell
& $py tests\run_all.py
& $py tests\offline\run_probes.py --all
```

## 改这个项目的代码时

- 项目惯例：**每一条真机事故都要留一个离线复现探针**（`tests/offline/probes/`），修完跑绿再交付；
  写新探针前先看 `tests/offline/offline_harness.py`（假读端 / 假发送器 / stub 模型）。
- 改发送、记忆、限流、恢复逻辑后，务必把 `run_probes --all` 跑一遍（约 8 分钟）。
- 不要动 `data\`（真实聊天数据）与用户没让你碰的文件；发布流程见 `工具\发布.ps1`（脱敏 + 自检 + 推送）。
