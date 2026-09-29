# wxbot · 微信 AI 助理

**在你自己的微信上跑一个 AI 助理** —— 本机运行 · 界面自动化收发（不修改微信客户端）· 本地 Ollama 或任意云端大模型

![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-0078D6?style=flat-square)
![python](https://img.shields.io/badge/python-3.11%20%2F%203.12%20tested-3776AB?style=flat-square&logo=python&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-green?style=flat-square)
![tests](https://img.shields.io/badge/tests-24%20suites%20%2F%20970%2B%20asserts-brightgreen?style=flat-square)

> 仓库名 `wechat-ui-agent`；Python 包名与命令行都是 `wxbot`。

> ⚠️ **用途与免责声明（先读这一段）**
>
> - 仅供**个人学习与研究**：在**你自己登录的微信账号**上做界面自动化实验。
> - **不修改微信客户端**、不做内存注入 / 反编译 / 破解；不提供任何微信数据；数据全部**本机处理、不上传**。
> - 界面自动化可能**违反微信用户协议**并带来**账号限制**风险，**后果自负**。
> - 与腾讯**无关联**、未获授权；请勿用于批量操作、代挂、收费服务。
>
> Personal learning/research only. No WeChat client modification, no injection or reverse
> engineering. All data stays local. Use may violate WeChat's ToS at your own risk.
> Not affiliated with or endorsed by Tencent.

---

## 它能做什么

- **自动回复** —— 群里被 @ / 被引用 / 命中关键词才说话，私聊可全自动；连点、刷屏、低俗话题都有专门护栏
- **记得住** —— 三层记忆：个人（谁唤出加载谁的）/ 群共享 / 滚动摘要；忌口、称呼、约定这类事实**每轮必带**
- **会聊天** —— 多档位 LLM（本地 ⇄ 云端自动回退）、人格化提示词、风格/长度/表情可控
- **会干活** —— 联网搜索（多引擎聚合 + 自动读正文）、到点提醒、关键词订阅通知、看图（多模态）
- **在微信里管它** —— 25 条指令（`/状态 /人设 /模型 /限流 /记忆 /总结 /提醒 /订阅 …`）+ owner / admin / member 三级权限
- **看得清的运维** —— Web 控制面板、看门狗、崩溃恢复、一键自检修复（`doctor --fix`）、长跑监控与报告
- **被真机打磨过的护栏** —— 发送三层校验（唯一候选 + 完整名 + 发送前回读）、防自回环、人设劫持防护、出站净化

## 它是怎么工作的

```text
WeFlow (HTTP + SSE)          ┌─ 触发/限流规则（@/引用/关键词/时间窗/连点/刷屏/低俗）
        │  读                │
        ▼                    ▼
  去重入库 (SQLite) ──▶ 记忆 + 摘要 + 人格组装 ──▶ LLM（本地/云端 + 工具）
                                                        │
  MaaMCP（OCR 定位 + 坐标点击） ◀── 出站净化 ◀────────────┘
        │  发（发送前完整名校验 / 发送后记录核对）
        ▼
    微信群 / 私聊
```

- **读**：WeFlow（本地 HTTP + SSE 实时推送）；可选 WeChatDataAnalysis MCP 兜底
- **发**：MaaMCP 界面自动化 —— OCR 找会话/发送键 + 坐标点击，**不注入、不改客户端**
- **脑**：任意 OpenAI 兼容端点或 Ollama；多档位 + 回退；模型可自行调用工具（联网/提醒/记忆/知识库）

## 长什么样（示意）

```text
群聊（被 @ 才说话）
  群友：@机器人 下周去上海出差要注意什么
  机器人：查了下：下周上海多雨、最高 32℃ 左右，带把伞；那边地铁早高峰很挤，尽量错峰。（附来源）

私聊（可全自动）
  你　：提醒我 10 点交周报
  机器人：好，10 点提醒你。
  你　：/记忆
  机器人：我记得的 3 条 —— ① 忌口：不吃辣 ② 称呼：主人 ③ 约定：周五交项目文档
```

回复风格由人设提示词 + 记忆决定，以上只是示意。

## 环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | **Windows 10 / 11** —— 唯一支持平台。界面自动化需要真实桌面会话，**不支持** Docker / 云服务器 / macOS / Linux |
| 微信 | 微信 PC 版，登录**你自己的**账号（建议先用小号试） |
| 读消息 | [WeFlow](https://github.com/hicccc77/WeFlow) 在运行（默认 `http://127.0.0.1:5031`），并拿到它的 `access_token` |
| 发消息 | [`maa-mcp`](https://github.com/MaaXYZ/MaaMCP)（MaaFramework 的 MCP 封装）—— `install.ps1` 会自动装好，不用手工配 |
| 模型 | 本地 [Ollama](https://ollama.com/)，或任意 OpenAI 兼容 / Anthropic / Gemini 端点 |
| Python | **3.11 / 3.12 实测通过**（推荐 3.12；3.10 理论可用，3.13+ 上游 `maa-mcp` 轮子可能还没齐，脚本会优先挑 3.12/3.11）。没装 Python 也行：装 [uv](https://astral.sh/uv) 后 `install.ps1` 会自动找一个 |

## 快速开始

### 1）一键安装

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

脚本会：找 Python（优先 3.12 / 3.11）→ 建 `.venv` → 装依赖（pyyaml / mcp / maa-mcp / pillow，失败自动换国内镜像）→ 从 `config.example.yaml` 生成 `config.yaml` → 跑一遍 `doctor` 自检。

### 2）改 `config.yaml`

只有三处必须改（其余每一项都有中文注释）：

```yaml
llm:
  active: local                    # local = 本机 Ollama；改成 cloud 走云端
  fallback: [cloud]                # 当前档位失败时按顺序回退
  profiles:
    local:
      type: ollama
      base_url: http://127.0.0.1:11434/v1
      model: qwen3.5:9b            # 换成 `ollama list` 里你有的模型
    cloud:
      type: openai                 # OpenAI 兼容：DeepSeek / Kimi / 通义 / OpenAI…
      base_url: https://api.deepseek.com/v1
      api_key: ${LLM_CLOUD_KEY}    # 建议用环境变量占位，别写明文
      model: deepseek-chat

ingest:
  source: weflow_sse               # mcp | weflow_sse | both（两个都开 = 互相兜底）
  weflow:
    base_url: http://127.0.0.1:5031
    access_token: "<WeFlow 的 token>"

contacts:                          # 白名单：它只在列出来的会话里干活
  - name: 文件传输助手              # 名字写微信里**显示的那个**（备注 / 昵称 / 群名都认）
    username: filehelper
    enabled: true
    self_ok: true                  # 自聊会话（给自己发消息做测试）
    trigger: { mode: always }
  - name: 某个群名
    username: 10000000002@chatroom   # 群聊的 username（形如 <数字>@chatroom）
    enabled: true
    trigger: { mode: mention, keywords: ["机器人"] }   # 群里：被 @ / 被引用 / 命中关键词才回
```

### 3）首次运行（四步）

```powershell
$py = ".\.venv\Scripts\python.exe"                  # 后面都用它
& $py -m wxbot.cli whoami                           # ① 认号：本号 wxid + 各群里的昵称 + @ 判据
& $py -m wxbot.cli ack-risk                         # ② 看一遍风险提示并确认（只做一次）
& $py -m wxbot.cli doctor --with-llm --with-tools   # ③ 自检：微信窗口 / 读端 / LLM / 联网 / 发送通路
& $py -m wxbot.cli web --port 8765                  # ④ 控制面板：白名单、试跑、日志、暂停都在里面
```

## 三种启动方式

| 方式 | 命令 | 说明 |
| --- | --- | --- |
| 控制面板（推荐） | `& $py -m wxbot.cli web --port 8765` | 面板 + 常驻循环在同一个进程里；启动时会打印带 `#token=` 的完整地址 |
| 无面板常驻 | `& $py -m wxbot.cli run --seconds 0` | `--seconds 0` = 一直跑，适合挂后台 |
| 开机自启 | `powershell -ExecutionPolicy Bypass -File autostart.ps1 -Enable` | 登录后自动拉起面板；`-Status` 查看、`-Disable` 卸载 |

同一时间只允许一个进程操作微信界面（Windows 单实例锁）：重复启动会被拒绝，不会出现两个机器人抢鼠标。
停止请用面板的"停止"或 Ctrl+C，不要在任务管理器里强杀 —— 发送中的消息会留下 `sent_unverified` 状态（不重发，防止重复）。

## 在微信里怎么用

直接发指令（群里要先 @ 它），常用几条：

| 指令 | 作用 |
| --- | --- |
| `/帮助` | 按用途分组的指令表；`/帮助 提醒` 看某条细节 |
| `/状态`、`/近况` | 现在忙不忙、最近回了谁、有没有失败 |
| `/记忆`、`/记住 xxx`、`/忘记 xxx` | 看 / 加 / 删长期记忆 |
| `/总结 [N]` | 把最近 N 条聊天压成摘要（补课用） |
| `/提醒 8:00 给猫喂药`、`/取消提醒` | 到点主动发消息 |
| `/订阅 关键词`、`/取消订阅` | 命中关键词就通知你 |
| `/模型`、`/人设`、`/限流`、`/设置` | 在微信里直接改配置（管理员 / 主人） |

25 条指令的完整列表和权限分级（owner / admin / member）见 [docs/GUIDE.md](docs/GUIDE.md)。

## 常见问题（FAQ）

**支持 macOS / Linux / Docker 吗？**
不支持。发送依赖 Windows 桌面里微信 PC 版窗口的 OCR + 点击，服务器上没有可操作的窗口。

**会不会封号？**
不修改客户端、不做注入或逆向；但界面自动化仍可能违反微信用户协议。建议只用自己的号、低频、不群发、不代挂。
项目内置了风险横幅、写闸门、间隔抖动、失败不重发、看门狗自动暂停等护栏，但**风险自担**这条不因此改变。

**发送失败 / 日志说"列表里没有该会话"？**
发送前有三层校验，宁可不发也不发错人。排查顺序：① `contacts[].name` 和微信里显示的是否一致（备注 / 昵称 / 群名都认）；
② 微信窗口别拖太窄（群名会被截断成"XX…"）；③ 跑一次 `doctor`，它会做会话名体检。

**WeFlow 连不上 / 报 `-105`？**
`& $py -m wxbot.cli doctor --fix`（尝试拉起 WeFlow / Ollama、修常见错误）；也可以单独跑 `工具\repair-weflow.ps1`。
再确认 WeFlow 正在运行，且 `ingest.weflow.access_token` 与它的一致。

**面板打不开？**
面板默认只监听本机 `127.0.0.1`；必须用**启动时打印的那个带 `#token=` 的完整地址**打开。端口被占就换一个：`--port 8899`。
把带 token 的链接发出去 = 别人能用你的机器人发消息，别外传。

**数据存在哪？会上传吗？**
全部在工程目录 `data\`（SQLite `wxbot.db` + 日志 + 截图），只在本机。聊天上下文是否发云端由每个模型档位的 `allow_context` 决定，默认云端档位不带上下文。

**谁的信息能给谁看？**
记忆分"人"和"群"两份存：**私聊**里说的只进那个人的个人记忆（任何群、任何人都拿不到），**A 群**里说的不进 B 群；
同一个群里**公开说过**的事，再被问到会正常回答（群里所有人都看得到原话）。这条边界有离线探针盯着（`tests/offline/probes/round9_privacy.py`）。

**联网搜索用不了？**
装可选依赖：`& $py -m pip install ddgs trafilatura`；并确认 `tools.web.profiles` 里列了允许调用工具的档位（默认只给云端档位开）。

**想换模型 / 同时配几套？**
`llm.profiles` 想加几套加几套，`active` + `fallback` 决定用哪套、失败怎么退。微信里发 `/模型` 也能切。

**消息重复发了怎么办？**
先看是不是连点触发（有合并窗口 `limits.burst_merge_seconds`，默认 8 秒）；"点了发送但没确认"的消息只记 `sent_unverified`，**不会重发** ——
一般重复是两次独立触发，用 `log` 看两条 request 是否不同。

## 质量与测试

```powershell
& $py tests\run_all.py                     # 24 个离线套件 / 970+ 断言：不碰微信、不花钱、约 80 秒
& $py tests\offline\run_probes.py --all    # 端到端复现探针（连点合并 / 崩溃恢复 / 限流矩阵…）
```

项目里**每一次真机事故都留了离线回归**：发错人、消息丢失、崩溃恢复、群摘要串话、人设劫持、空头支票、
异常键活锁、连点刷屏、低俗对骂…… 都能不连微信复现，并在修改后自动钉住。

## 文档

| 想看什么 | 去哪 |
| --- | --- |
| 完整手册（配置全表 / 发送三层校验 / 故障排查 / 长跑验收） | [docs/GUIDE.md](docs/GUIDE.md) |
| 配置模板（逐项中文注释） | [config.example.yaml](config.example.yaml) |
| 怎么判断"装好了" | `& $py -m wxbot.cli doctor --with-llm --with-tools` |
| 出问题时的修复工具 | [`工具\repair-weflow.ps1`](工具/repair-weflow.ps1)、`doctor --fix` |
| 长时间稳定性 | [`工具\soak_monitor.py`](工具/soak_monitor.py)（24h 存活率 / 耗时 / 失败口径） |

## 仓库结构

```text
wxbot/                # Python 包：cli / ingest / rules / store / llm / send_maa / web / doctor …
tests/                # 24 个离线套件（run_all.py）+ offline/ 事故复现探针
工具/                  # repair-weflow.ps1（WeFlow 修复）、soak_monitor.py（长跑监控）
docs/GUIDE.md         # 完整手册
knowledge/            # 本地知识库目录（放自己的资料，模型可用 lookup_notes 查）
config.example.yaml   # 配置模板（200+ 行，逐项注释）
install.ps1           # 一键安装（建 venv / 装依赖 / 生成配置 / 自检）
autostart.ps1         # 开机自启（-Enable / -Status / -Disable）
```

## 依赖与许可证

| 组件 | 说明 |
| --- | --- |
| `pyyaml`(MIT) / `mcp`(MIT) / `pillow`(HPND) | 基础依赖 |
| `ddgs`(BSD) / `trafilatura`(Apache-2.0) | **可选**：联网搜索与正文抓取 |
| [MaaMCP](https://github.com/MaaXYZ/MaaMCP) | 独立进程，通过 MCP stdio 调用；本项目**不包含**其代码。其自身为 **AGPL-3.0**，使用时以它的许可证为准 |
| [WeFlow](https://github.com/hicccc77/WeFlow) | 独立程序，只读地提供消息数据 |
| 本项目 | **MIT**（见 [LICENSE](LICENSE)） |

## 免责声明（完整版）

本项目仅用于个人学习与研究，在你自己拥有并登录的微信账号上做自动化实验。它不修改微信客户端、
不做内存注入 / 反编译 / 破解，不提供或上传任何微信数据；所有数据处理都在本机完成。
使用界面自动化可能违反微信的用户协议并导致账号受到限制，**使用风险由使用者自行承担**。
本项目与腾讯公司无任何关联，也未获得其授权或认可；请勿将其用于批量操作、账号代挂、收费服务等场景。

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=b1138549737-lgtm/wechat-ui-agent&type=Date)](https://star-history.com/#b1138549737-lgtm/wechat-ui-agent&Date)
