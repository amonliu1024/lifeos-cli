# 会话证据参考

需要披露会话正文、解释 Activity、处理 `partial` / `interrupted_with_result`、清洗计数或日报 frontmatter ID 时，先读取本文件。Daily 工作流负责顺序和叙事，本文件负责证据语义。

## 来源采集门

- `index`、`list` 和 `pack` 查询当前私有派生存储，不读取来源应用。结构完整但 Activity 为零的 index，不能证明来源窗口已经扫描。
- Daily 复用的 scan 必须与 `begin` 的窗口完全一致，`status=complete` 或 `status=partial`，`manifest_valid=true`，列出 `codex`、`claude`、`smartwork`、`deepseek` 四个来源，并且没有来源为 `failed`。复用前校验其 `scan_id`。`partial` 表示已保留证据，但存在不完整 Slice 或来源告警；可以继续使用，但必须审计并披露缺口边界。
- 没有可复用 scan 时，执行一次精确窗口的 `sessions scan --source all` 并校验返回的 scan。来源缺失、失败或不可读应标记 Pending，不能当作当天没有工作。partial scan 继续进入完整 index 和异常审计；只有覆盖范围或告警可能改变报告事实时才停止。只有全来源 scan 成功、index 完整，并明确确认 partial 告警不会隐藏目标日工作时，才能得出 Activity 为零的结论。

## 全窗口粒度

- `index` 是全天盘点。必须使用 `begin` 返回的精确窗口，不增加来源、项目、会话、工作区或 query 过滤。
- 只有同时满足 `budget_exceeded=false`、`dropped_by_project` 为空、`retention_pruned.slices` 为零且无 `entries`，并且 `activity_total` 等于 `activities[]` 中唯一 `activity_id` 数量时，index 才完整；否则提高 `--max-bytes` 或记录保留期边界。
- `sessions_activities` 和 `activity_ids` 只来自这一份完整 index。不得汇总多个 pack、项目视图或重复的来源视图。`suppressed_activities` 是清洗元数据，不是预算省略。
- 即使某些 Slice 没有进入正文，partial、interrupted 和 omitted 计数仍来自同一份完整 index。

## Activity 语义

- Activity 是同一来源、同一会话在请求 gap 内形成的确定性证据导航单元，不是待办、工作主线、项目或价值结论。
- `project_key` 只能证明工作区映射，不能证明业务主题、归属或正文段落位置。
- `signal_score` 只提示证据密度，不衡量重要性、投入、价值或完成状态。
- Activity 生命周期刻意从严：一个 interrupted 或 incomplete Slice 就可能主导聚合状态。正文结果必须根据相关 Slice 序列及其最强终态证据判断。
- 并行 Agent 和不同来源可能为同一工作主线产生多个 Activity，一段长会话也可能包含多个目标。Daily 可以按语义合并或拆分，但不能修改 Sessions 事实。

## 输入审计

选择日报材料前，检查完整 index 中是否存在下列可能让自然日盘点失真的证据：

- 在一两秒内形成、包含大量 Slice 的 Activity；
- 无关项目出现相同时间戳聚集；
- 一个超长 Activity 中出现多个明显目标；
- partial 计数集中在同一时间戳或会话；
- pack / show 的原生 ID 或正文显示导入历史或应用上下文。

异常可能改变当天事实时，写初稿前必须展开。应用导入的上下文只说明来源，不能算作导入时刻发生的工作。Codex 输出中仍出现 `external-import-turn-*` 时属于采集缺陷，不是 Daily 解释问题。

## 渐进深读

1. 使用完整 index 盘点全部 Activity，并对每个高重要性 Activity 给出处理结论。
2. 对核心工作主线，使用与 index 完全相同的窗口和 gap，通过 `pack --activity` 读取所有相关 Activity。
3. 对承载终态结果、验证、矛盾或影响叙事的非完成边界的 Slice，使用 `show` 深读。
4. 次要工作在行动、结果和边界已经成立后停止；轻微信号可以保留在 index 粒度。
5. pack 报告省略 Activity / block、无法容纳或丢失所需终态 Slice 时，提高边界或精确读取该 Slice，不得从邻近内容或 Agent 声称中补齐。

完成性随重要性判断：每个高重要性 Activity 都有明确处理结论；每条写入报告的核心工作主线都有足够可见证据解释意图、关键变化、终态、证明和剩余边界。

## 生命周期与清洗计数

下列计数全部来自同一份完整的全窗口 `index`：

- `sessions_partial` = `coverage_summary.partial` 的 Slice 数量。
- `sessions_interrupted` = `cleaning_summary.interrupted_with_result` 的 Slice 数量。
- `sessions_omitted` = `cleaning_summary.explicit_abort_without_work` 的无正文 TurnOmission 数量。
- Activity 总量和 ID 始终保持 Activity 粒度，不能替代 Slice / Turn 计数。

`interrupted_with_result` 表示中断前观察到了结果，不代表完成。日报使用该结果时写明“中断时已观察到的结果”，并保留缺失边界。无正文中止只进入 `sessions_omitted`，不进入日报正文。

Activity 生命周期聚合顺序为 `interrupted_with_result` 优先，其次 `incomplete`，最后 `completed`；未知状态保守按 `incomplete` 处理。不得从旧 `user_interrupts` 推断。

## 结论强度

`agent_message` 只能证明说过，不证明成功。`origin: user` 也不能单独证明是本人表达；应结合 `shape`、`text_chars` 和注入分类，只引用自然语言正文。`verifications` 只能证明指定检查运行过，不证明部署或生产行为。工具调用量不能证明工作重要性或验证程度。

注入规则、任务说明、粘贴日志、审批裁决、压缩上下文和机器脚手架都只作为上下文，不当作本人意图或工作结果。先按时间顺序核对一条工作主线，再分开保留交付层级：

```text
实现 → 本地测试 → 本地 Runtime → 提交 → 推送 → 部署 → 目标环境验证
```

一层证据不能声称后一层结果。只有对同一结论至少具有同等证据强度时，最新表述才覆盖旧表述。

完成标准：自然日 index 可信或已明确标记 Pending；每个高重要性 Activity 都有处理结论；每条核心工作主线都有终态证据和交付层边界；所有 frontmatter 计数与 Activity ID 均来自同一份完整 index。
