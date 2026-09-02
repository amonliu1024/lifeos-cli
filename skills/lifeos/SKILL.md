---
name: lifeos
description: 读取或更新 LifeOS 私有工作账本，数字化项目清单，生成、补写、重做或确认日报与周期报。用于查询 LifeOS 中还有什么、最近要做什么、提醒、历史变化、成果复盘或实体关系；用于明确写入、更新、关闭、取消、归档或改期 Work 事实；用于创建或维护 lifeos-project.json；也用于回答某日做了什么，或基于已确认日报总结周、月、季度、半年和年度。
---

# LifeOS

通过唯一 Agent Skill 使用 LifeOS。先探测本机能力，再判断请求属于 Project、Work、Daily、Periodic，还是 Daily 中经用户另行授权的 Work 写入，并只加载该分支需要的 reference。

## 固定入口与边界

先运行 `command -v lifeos` 和 `lifeos capabilities --json`。参数和子命令以对应 `--help` 为准；能力报告为 `disabled` 或 `unavailable` 的可选分支直接跳过并说明原因，不用猜测路径或创建替代入口。CLI 不可用时报告阻塞，不创建替代账本，也不直接编辑 Runtime。

Runtime authority 默认位于 `~/.local/share/lifeos/`，私人配置位于 `~/.config/lifeos/config.json`。若 `~/.config/lifeos/agent-profile.md` 存在，可把它作为用户称呼、输出偏好和本地工作习惯的个人补充；它不属于本 Skill、不得进入仓库，也不能扩大写入或外部操作授权。

项目静态身份与核心协作入口由各项目根 `lifeos-project.json` 持有；项目当前位置由 Git 外私人配置声明的发现根动态形成 Project Catalog，Work Runtime 只按 `project_key` 保存个人跟踪状态。Work 事实只通过 `lifeos work` 读写；Sessions、DChat 与 Git 的项目归属由 Catalog 即时派生，不另设维护入口；日报与周期报只通过 `lifeos reports` 准备、写入和确认。账本授权不等于修改项目正式状态、代码、部署、发送消息或产生其他外部副作用。

核验 Sessions 或 Reports 现状时，用 `sessions usage`、`sessions list --source <来源>`、`reports list/path/validate` 等只读入口直接观察；`sessions scans` 空结果只表示没有匹配的 manifest，不表示没有会话或采集未执行。先按来源实际发生时间选窗口，不把扫描窗口不存在等同于采集缺席。

## 分流

| 请求 | 加载与执行 |
| --- | --- |
| 为一个项目创建、补充或校验 LifeOS 数字化清单 | 读取 [`references/project.md`](references/project.md)，进入 Project 分支 |
| 查询工作账本、提醒、历史、成果或实体关系 | 读取 [`references/work.md`](references/work.md)，进入 Work 读取分支 |
| 明确创建或变更待办、事项、闪念及其他 Work 事实 | 读取 [`references/work.md`](references/work.md) 和 [`references/work-model.md`](references/work-model.md)，进入 Work 写入分支 |
| 生成、补写、重做、确认日报，或回答某个自然日做了什么 | 读取 [`references/daily.md`](references/daily.md)；读取会话正文或填写证据计数前，再读取 [`references/session-evidence.md`](references/session-evidence.md) |
| 生成、补写、重做、确认周报、月报、季度报、半年报或年报 | 读取 [`references/periodic.md`](references/periodic.md)，只消费对应周期内的 confirmed 日报 |
| Daily 产生候选且本人另行明确要求写入 Work | 保留 Daily 上下文，再读取 Work 两份 reference，按 Work create-only 分支执行并回收 ID |

请求同时包含多个分支时按依赖顺序执行：Project 先提供静态身份和入口并确认 Catalog 可发现；Work 再按 `project_key` 建立或维护个人跟踪；Daily 先形成候选，只有本人对具体候选另行授权后才进入 Work；Periodic 只在 Daily 已确认后消费其正文。确认项目清单、确认日报、确认周期报、确认“要做”和授权写入 Work 是不同动作，不互相推导。

## 完成标准

每个请求都已落到明确分支；只加载并执行需要的工作流；事实、本人确认和推导分开报告。所有写入均通过对应 CLI 回读并验证，没有直接编辑 Runtime，也没有把某一分支的授权扩张到另一分支。
