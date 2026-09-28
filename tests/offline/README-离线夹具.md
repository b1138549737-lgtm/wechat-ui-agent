# 离线夹具说明（给二号）

来源：2026-09-27 代码审查期间搭的验证工具。目的只有一个——**不碰微信窗口、不调 MaaMCP，
也能驱动真实业务装配**，把"看起来做了其实没做"的问题提前挡住。

## 一、怎么工作

`offline_harness.py` 里做了三件替换（都用官方入口注入，不改业务代码）：

1. `cli.Sources = lambda cfg: FakeSources(...)` —— 假的读端，按时间表把"群友的话"喂进真循环
2. `cli.make_sender = lambda cfg, resolver=None: FakeSender()` —— 假的发送器，只记账不操作界面
3. `cli.build_llm = lambda cfg: StubLLM()` —— 假的模型（只在完全离线的模式里用）

这样 `cli._run_async()` / `cli.generate_reply()` / `commands.dispatch()` 走的都是真代码路径。

## 二、模式一览

| 模式 | 跑什么 | 需要模型吗 | 要不要花钱 |
|---|---|---|---|
| `--mode a` | 指令/人设/字数/档位 是否真的作用到 system；记忆写入与跨人隔离 | 不需要（stub） | 不 |
| `--mode h` | 积压超过 `backlog_max_age` 的消息是否被静默作废 | 不需要 | 不 |
| `--mode b` | 基础群聊（谁说的、被 @ 才回） | 需要 | 云端会花钱 |
| `--mode c` | 图片带 @ 是否触发、跨会话（私聊 vs 群）隔离、全角 @ | 需要云端视觉 | 会 |
| `--mode d` | 13 轮长对话 + 群成员进出 + 长程召回 | 需要 | 会 |
| `--mode e` | 20+ 轮长跑，跨摘要刷新后的召回 | 需要 | 会 |
| `--mode g` | 两个群并行 + 同一人在两个群（跨群隔离） | 需要 | 会 |

跑法（用工程自带解释器）：

```
<REPO>\tmp\venv312\Scripts\python.exe offline_harness.py --mode a
E:\...\python.exe offline_harness.py --mode h
E:\...\python.exe offline_harness.py --mode e --model cloud --seconds 150
```

## 三、配套探针（都是纯函数/纯本地，不花钱）

| 文件 | 查什么 |
|---|---|
| `probe_clean_and_budget.py` | `clean_output` 的 7 条输入/输出矩阵（含两种必错形态）+ 预算裁剪 |
| `probe_edges.py` | 群事件数量护栏 / 组员字段映射 / 超长入站 token / 本地档紧预算丢事 |
| `probe_fixes.py` | 已修项复核：`/人设`·`/设置`·`/模型` 链路、去重键跨进程、清理只删终态、`.venv` 能被找到 |
| `probe_fixes2.py` | 已修项复核：`allow_context` 载荷、token 不进 URL、WAL、Store 单例、summary 聚合、配置审计 |

## 四、建议怎么并进仓库

1. `tests/offline_harness.py` ← 本文件（把顶部 `ROOT` 改成相对路径即可）
2. `tests/test_offline_scenarios.py` ← 把 `--mode a` 与 `--mode h` 两段做成常规用例（完全离线、不花钱），
   加进 `tests/run_all.py` 的 SUITES
3. 需要模型的模式（b/c/d/e/g）留在"真机前可选冒烟"，README 里注明会消耗云 token

## 五、它现在能复现的已知缺陷（改完后这些应变成回归用例）

1. `clean_output` 对"成块伪调用"整段删空 → 空回复；对 XML 结果块/函数式伪调用原样漏出
2. 图片消息的描述会覆盖原文 → 群里"@它 + 发图 + 提问"不触发（mode c 可复现）
3. 记忆没有合并/更正 → 同一件事被灌多份；说"改主意了"库里不变（mode a / d 可观察）
4. 积压超过 `backlog_max_age` → 消息静默作废（mode h 可复现）
5. 无工具档位会编造"已记录/已提醒"（mode e/g 用本地档时可见）

## 六、注意

- 这套夹具**不会**启动 MaaMCP，也不会操作微信窗口（假发送器只记账），可以放心在开发机跑。
- 云调用走的是 `config.yaml` 里的档位配置，跑之前确认 `llm.active` 是你想要的档位。
- 我先没往二号仓库写任何文件；上面两个文件要不要并进 `tests/` 由二号决定。

## 七、复现脚本已并入：`tests/offline/probes/`（2026-09-27 工单收尾）

`%TEMP%\wxbot_sim\` 里的 6 个零成本复现已落到 `probes/`（路径相对化，交付目录也能跑），
统一由 `tests/offline/run_probes.py` 驱动：

```powershell
python tests\offline\run_probes.py           # quick（约 30s）：空头支票崩溃 + 长跑保留
python tests\offline\run_probes.py --all     # 全部（约 6-8 分钟，真跑 _run_async 循环）
```

| 脚本 | 钉住的行为 |
|---|---|
| `round18_empty_promise_crash.py` | 模型说"记着了"却没调工具 → 回复必须照发（空头支票分支不许崩） |
| `round16_retention.py` | messages/截图/own_sent/replies 四张表的保留边界 |
| `round5_regressions.py` | `send_plain` 的 search_as + 过期出口统一为"该回但错过了" |
| `round8_drop_matrix.py` | 消息去向矩阵（合并/静默/延后/重试窗口），延后不许打转（deferred≤2） |
| `round10_watch.py` | 订阅通知闸门按**收通知会话**算 + "另有 N 条已合并" |
| `round11_recovery.py` | 崩溃恢复重放 + 异常键活锁终止（标 expired/数据异常） |

quick 档已接进 `tests/run_all.py`（`test_offline_probes`）；`--all` 留给"改发端判分/恢复/闸门后"手动跑。
原 `round10b_gate_scope.py` 是纯演示（调用 `proactive_gate` 对比两种对象），
其结论已被 `round10_watch` 的真实链路用例取代，不再并入。
