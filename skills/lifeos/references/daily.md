# Daily 工作流

本文件拥有日报生成、补写、重做与确认的**执行顺序与交付检查**；正文怎么写和正反例样张见 [`daily-writing-guide.md`](daily-writing-guide.md)，只读问答走 [`daily-readonly.md`](daily-readonly.md)。

- 若 `~/.config/lifeos/agent-profile.md` 存在，读取其中的日期时区约定（如 `TZ=Asia/Shanghai date +%F`）与各项目关注视角。
- 日报不再保留「待办与依赖」「需要关注」两节，也不再从上期日报滚雪球对账；持续跟进交由 `lifeos work` 承担，当天新冒出的线索在交付时列为候选。

## 一、准备上下文

```bash
lifeos reports prepare --day <YYYY-MM-DD> --json
```

`prepare` 只读返回目标自然日 `[from, to)` 窗口、当前日报状态、最近一份已确认前序日报、项目清单摘要、四路来源 `ready` 状态，以及当日 `work.window_events` 与 `work.same_day_entries`。

- 目标日已是 `confirmed` 时保持不变；只有本人明确要求重做时，才在第四步使用 `begin --redo`。
- 前序已确认日报只用来避免重复交代旧背景，不逐项继承旧待办。

## 二、补齐未就绪来源

只对 `prepare` 报告 `ready=false` 且已配置的来源执行扫描；尚未配置（`enabled=false` 或无注册 Git 仓库）直接跳过：

1. **Sessions**（必需）：若未就绪，运行 `sessions scan --source all --from <from> --to <to> --json` 并 `sessions validate --scan <scan-id>`，随后读取无过滤的完整 `sessions index`。细则见 [`session-evidence.md`](session-evidence.md)。来源缺失、校验失败或覆盖不足以支撑结论时停下来报告 Pending，不把空 index 写成「当天没有工作」。
2. **Git**（有启用仓库时）：未就绪时运行 `git scan` 与 `git validate --scan <id>`。正文出现任何提交、推送或部署层结论时，对应仓库窗口内必须有通过校验的快照，否则降级为「会话陈述」。
3. **DChat**（`enabled=true` 时）：未就绪时按 [`dchat-evidence.md`](dchat-evidence.md) 执行 `dchat scan`、`dchat validate` 与 `dchat index`；遇到 `client_ipc_forbidden` 时保持命令不变，在允许本机 IPC 的环境重试，不操作桌面客户端。
4. **Calendar**（`enabled=true` 时）：未就绪时按 [`calendar-evidence.md`](calendar-evidence.md) 执行 `calendar scan` 与 `calendar index`，按「性质 → 证据 → 名单」生成「准备记入」和「剔除」两张会议清单。

## 三、定调与两阶段收敛

1. **先抓本人定调（不阻塞出稿）**：优先读取本人在本次对话里给的重点/取舍原话，以及 `prepare` 返回的 `work.same_day_entries`（当天记下的 `note` / `question` / `insight` 是高置信人工锚点）。本人未提前给定调时，直接按证据出稿，在第五步交付时单独列「主线定调核对」。
2. **渐进深读**：核心主题调用 `sessions pack --activity` 与必要 Slice 的 `sessions show`（必要时 `--evidence-ref`）；次要变化读到对象与终态即停；DChat 只对可能改变主线或终态的会话调用 `dchat pack`。
3. **先收内部「当日事实简报」再动笔**：写正文前，先在脑内或临时草稿里收出干净的事实简报，把原始 JSON、文件路径和工具日志留在写作窗口之外。每条拟入主线只留四要素：
   - 昨天为止的旧状态 → 今天之后的新状态（能拿它干什么）
   - 促成变化的关键拍板或推翻（优先引用本人原话）
   - 证据实际能证明到的最高交付层
   - 仍影响下一步或风险的现实缺口（无则省略）
4. **依简报写正文**：严格按 [`daily-writing-guide.md`](daily-writing-guide.md) 成文，并跑完文末的**写入前 10 项自检**。

## 四、写入草稿

```bash
lifeos reports begin --day <YYYY-MM-DD> --json          # 重做已确认日报时加 --redo
lifeos reports write --day <YYYY-MM-DD> --body-file - \
  --sessions-scan-id <SESS_SCAN_ID> \
  [--git-scan-id <GIT_SCAN_ID> --git-commit-id <REPO@SHA> ...] \
  [--calendar-scan-id <CAL_SCAN_ID>] \
  [--work-event-id <EVT_ID> ...] \
  [--user-notes <N>] [--unresolved <N>] --json
```

- 优先传 `--sessions-scan-id`（由 CLI 自动吸纳全部唯一 `ACT-*` ID 与清洗计数）和 `--calendar-scan-id`（不传 `--calendar-event-id` 时自动吸纳窗口内全部日程 ID）。
- `reports write` 会拦截正文中泄漏的内部 ID、40 位 SHA、本机路径及超过 10 条的「还做了」；若返回 `lint_warnings`，先对照规范修完再交付。
- 日历已启用时，草稿可先写占位或暂不写 `## 会议`，**必须等本人在对话里确认两张会议清单后，再把确认参加的会写入 `## 会议` 并更新草稿**。

## 五、交付、候选与确认

每次交付（首次出稿、收到补录、确认日报）随回复一并给出以下检查结果（无内容时省略对应区）：

1. **主线定调核对**（仅当本人事前未给定调时）：用 1–2 句简问「今天最看重的是否就是这两条、关键取舍原因是否准确」，方便本人一句话纠偏。
2. **会议确认清单**（日历启用且未定稿时）：列出「准备记入」与「剔除」两张表，说明项目/性质和依据；本人若说某个循环系列以后默认去/不去，顺手调用 `lifeos calendar series set`。
3. **待你判断（当日新线索）**：当天新冒出、事实成立且与本人有关、但拿不准要不要长期跟踪的事项，写清「事实来源 + 与本人关系 + 建议去向（记成 Work 待办 / 随记或疑问 / 不跟踪）」。未经本人确认，绝不自动写入 Work。
4. **关联更新检查**：用 `lifeos work entries --live` 与相关项目 `CURRENT.md`（沿已有引用只查一跳，含 `（lifeos关注）` 标记）核对当天变化：
   - 已有 Work 或项目表述过时/需了结：列出「目标 ID 或文档位置 + 现状 + 建议改成 + 依据」；
   - 本人已认领且需持续跟踪的新结果：列新增 Work 候选；
   - 全部一致或无对应记录：明确写「本次关联检查无需更新」。
   - 收到补录时先区分发生时间：属于目标日结束前的补入草稿；目标日之后新做的决定留到对应日期和当前更新候选。

本人明确确认后执行：

```bash
lifeos reports confirm --day <YYYY-MM-DD> --json
lifeos reports validate
```

若本人同时明确授权了某条 Work 写入或更新候选，转顶层 Work 分支按授权执行并分别回读验证。

## 边界

- 不生成周期报（转 [`periodic.md`](periodic.md)）；不替本人确认日报或自动写入 Work；Git 只读本地注册仓库；DChat 只读 `p2p / extp2p` 私聊与项目清单声明的群聊；日历只读不改日程；重做留下的旧快照仅在本人明确要求时用 `reports prune --apply` 清理。
