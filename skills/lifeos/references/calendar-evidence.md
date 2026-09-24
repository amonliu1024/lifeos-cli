# 日历证据

日历是 Daily 的可选 supporting evidence，目标是把当天开了哪些会、花了多少时间、每场会服务哪个项目如实记进日报。它只证明日历上排了什么，不证明本人去了、也不证明会上定了什么；去没去、算不算工作、对到哪个需求，全部由 Daily 提议、本人逐场确认。

## 采集门

1. 先运行 `lifeos calendar validate --json`。`config` 显示未启用时停止本分支并继续其他来源；其他校验失败表示已有私有归档不可信，若会改变日报结论则保留 Pending。日历复用 DChat 的 dws wrapper，遇到 `client_ipc_forbidden` 与 DChat 同样处理：保持命令不变，在允许访问本机 IPC 的环境重跑，不操作桌面客户端。
2. 查询精确自然日窗口的 `lifeos calendar scans --from <window.from> --to <window.to> --json`。最新同窗口 scan 为 `complete` 时复用；没有时执行一次 `lifeos calendar scan`。`partial` 表示部分日程没拿到详情（参会人为空），日程本身仍在。
3. 读取 `lifeos calendar index --from <window.from> --to <window.to> --json`。`source_status=unknown` 时没有这一窗口的归档，不得把空结果当作当天没有会。

每条日程只有：`instance_id`（写进 frontmatter 的 ID）、`event_id`、`series_key`（循环系列身份）、`summary`、`dtstart`、`dtend`、`type`（Single、RecurringMaster、Exception）、`attendees`（姓名）、`series_rule`（名单值：attend、skip 或空）、`overlaps`（起止完全相同的其他日程，多半是会议室占位）。没有回复状态、组织者、描述和链接。

## 提议顺序

对索引里的每条日程按三步形成提议，结果分成「准备记入」和「剔除」两张清单：

1. **按性质**：对不到任何项目或需求的会先分出来——培训、分享、颁奖、团建、聚餐、考核沟通、部门周会。这些会本人可能去了，但它们不服务某个交付对象；本人确认去了的，记入时写明性质，工时流程会按性质处理。
2. **看证据**：会话、聊天、纪要或本人补录里提到这场会（评审结论、会后动作、纪要落盘）就算参加了，写上依据。`overlaps` 里的会议室占位与本人自己的日程同一时段时，只保留一条，占位那条剔除。
3. **看名单**：剩下的循环系列按 `series_rule`——`attend` 提议记入，`skip` 提议剔除；单次日程和没有名单的系列，默认待定，列出来请本人定。

跨日或全天日程整条保留起止时间，提议时按当天实际占用估算时长。

## 列出并确认

草稿生成后，在对话里给出两张清单，每场一行：起止时间、名称、准备对到的项目或需求（或性质）、依据。本人改完、明确确认后，才把记入的会写进草稿的「会议」一节，未经确认的不进日报，名单命中的也一样。本人在确认时说「这个系列以后默认去 / 不去」时，用 `lifeos calendar series set --series <series_key> --rule attend|skip --title <标题>` 写入名单；说「不要默认了」用 `--clear`。名单只是提议的默认值，不替代确认。

## 写入

`## 会议` 放在「还做了」之后、「欠与等」之前，只写本人确认参加的会，一场一行：`- 起止时间 名称 — 服务的项目或需求`，性质类的写 `— 部门例会` / `— 培训`；末行 `共 N 小时。` 剔除的会不进正文。frontmatter 由 `reports write --calendar-scan-id <scan_id>` 加窗口内全部日程的 `--calendar-event-id <instance_id>` 承担，记入与剔除的都在其中，它们是审计信息不是正文。未启用日历的日报不传这两个参数。

## 边界

命令不创建、修改、取消日程，不回复邀请，不查他人忙闲，不保留回复状态。日历原始归档不进镜像。会议事实不写 Work，不能单独证明完成、上线或任何交付层级。
