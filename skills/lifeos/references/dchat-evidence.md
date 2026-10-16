# DChat 证据

DChat 是 Daily 的可选 supporting evidence，补足私聊与项目核心群中的背景、反馈和协调。归档保留来源标记；消息能支持哪项事实，按下文的责任、对象、范围与时间判断。

## 采集门

1. 先运行 `lifeos dchat validate --json`。`config` 显示尚未配置时停止本分支并继续其他日报来源，不自行猜 wrapper；其他校验失败表示已有私有归档不可信，若会改变日报核心结论则保留 Pending。
   若 DWS 返回 `client_ipc_forbidden`，或 debug 中出现 `workspace-server` socket 的 `Permission denied / Operation not permitted`，保持原命令和采集范围不变，仅申请允许访问本机 IPC 的沙箱外执行后重试。这个错误不能证明 D-Chat 未启动；不得因此启动、切换、读取或操作桌面客户端。
2. 查询精确自然日窗口的 `lifeos dchat scans --from <window.from> --to <window.to> --json`。最新同窗口 scan 为 `complete` 且重新 `validate` 通过时复用；没有可复用 scan 时执行一次 `lifeos dchat scan`。`partial` 或失败不等于没有消息，先读取 warning，不能证明的范围保留为缺口。
3. 读取无会话过滤的完整 `dchat index`。index 固定为 `evidence_level=supporting`，并给出纳入会话、消息数、由项目清单派生的项目关联、source scan 与完整性；`source_status=unknown` 时不得把查询结果当作完整窗口。

完成标准：选中精确窗口 scan 且通过校验；每个会改变日报结论的 `partial / range_truncated / source gap / permission` 已解释或保留 Pending。

## 渐进深读

先用 index 选择可能改变当天工作主线或承接事项状态的会话，再对这些会话运行：

```bash
lifeos dchat pack \
  --from <window.from> \
  --to <window.to> \
  --conversation <conversation-id> \
  --max-bytes <预算> \
  --json
```

只有 `budget.omitted_messages=0` 才能声称该会话在当前窗口的已归档正文已读完；否则提高预算、继续缩小会话或保留省略边界。不要把整日所有聊天一次性装入上下文。会话范围先依据 DChat 返回的结构化 `type`：`p2p / extp2p` 一律作为正常私聊采集，不得根据账号名称、头像、发送者名称、消息内容或表达风格推断其为机器人、AI 或官方账号；只有 `type` 明确为 `official / p2bot / p2ai` 时才排除。`channel / extchannel` 只有 VID 出现在当前有效项目清单中才读取正文；其他群聊只有 scope 元数据。不要用群名、活跃度、本人是否发言或关键词绕过 scope。

消息证明力按具体主张判断：

- 责任由已有任务、项目记录或明确认领确认。执行负责人对其负责结果的明确完成反馈、接收方的收讫或验收确认，以及有决定权者的明确决定，在对象、范围、实际时间吻合且无未解决的直接冲突时，可支持相应业务状态。只完成一部分时仅解除对应部分；日期不明时继续核对时间。
- 上线通知按编号或明确对象关联读取对应系统详情，核对范围和状态时间；详情成立可支持该需求已上线，实际可用范围和效果仍需各自证据。
- 转述、推测、意图、身份或对象不明的肯定答复只作线索；Agent 自述成功需读取执行结果。沿已有事项来源定向核对，Work 关闭、Git 提交和重复转述都只证明各自发生的事实。

完成标准：引用事实有原文和明确预算边界；每项状态结论与说话者责任、对象、范围及实际时间相符；未解决冲突或来源缺口交 Daily 处理。

## 项目判断

`projects_confirmed=true` 表示该群聊已由一个或多个当前 Project Catalog 项目的 `lifeos-project.json` 声明；项目不需要先进入 Work 跟踪。`project_candidates` 是派生的搜索范围，不代表每条消息都属于这些项目。按具体内容把当前片段判断为零个、一个或多个既有 Project；同一会话可以拆入不同主线，同一片段也可以同时关联多个项目。

私聊不进入项目清单，因此 `projects_confirmed=false` 是正常状态。Daily 可依据原文做本次日报的临时归属，但不把私聊或临时判断持久化。群聊出现稳定的新项目关系或与清单冲突时，只把差异列为 `unresolved` 并说明证据；确认日报不等于授权修改项目工作区。只有本人另行明确要求维护项目数字化清单时，才交给顶层 Project 分支处理。
