# Daily 只读问答

用户只是询问「某天做了什么」「昨天忙了啥」「上周三那场会定了什么」时走本分支，**不创建、修改、覆盖或确认任何日报，也不触发 Work 关联检查或会议确认流程**。

## 执行顺序

1. **先查日报现状**：运行 `lifeos reports path --day <YYYY-MM-DD> --json`（确定「今天/昨天」前如存在 `~/.config/lifeos/agent-profile.md` 需按其中 `Asia/Shanghai` 口径换算）。
   - 若目标日已是 `confirmed`：直接读取并提炼该日报正文回答问题，明确说明「依据 <YYYY-MM-DD> 已确认日报」，通常到此即可停止。
   - 若目标是 `draft`：先说明这是尚未确认的草稿，再按需补充来源核对。
2. **无已确认日报时的只读补充**：
   - 运行 `lifeos reports prepare --day <YYYY-MM-DD> --json` 查看四路来源就绪状态和当日 `work` 记录。
   - 只按回答问题所需的最小范围只读读取：`sessions index` / `pack`（必要时 `show`）、`dchat index` / `pack`、`calendar index`、`work changes`。
   - 遇到未扫描或缺失的来源，如实说明缺失范围，**不运行 `reports begin` / `write` / `confirm`**。

## 回答边界

- 区分开「已确认日报陈述」「草稿线索」「会话/聊天原始证据」与「尚未扫描的空白窗口」，不把空索引说成当天没有工作。
- 不替用户写入或修改 Work 记录；如果对话中顺带发现值得记下的事，只口头建议，等明确授权再转 Work 分支。
