# 版本记录

本文档只记录明确版本给 LifeOS CLI 使用者带来的最终结果。尚未进入正式版本的用户可见变化合并写入 `Unreleased`；内部实现、验证过程和项目治理不进入版本说明。

## Unreleased

### 修复

- Codex 会话采集不再把上下文压缩记录误判为未知格式，并通过适配器代际更新强制重读已有来源。
- `lifeos git validate` 现在同时核验每个启用仓库的实时根目录，能在日报采集前发现已移动或失效的注册路径。
- DChat 消息采集兼容整数 `created_ts` 时间字段；仍无法解析时间时返回有界的字段形状诊断，不记录消息正文或原始时间值。

### 变更

- `lifeos reports path --json` 现在只读返回目标日在 Asia/Shanghai 的完整自然日窗口；已确认日报可以据此重新采集证据，无需覆盖日报或自行计算日期。
- 项目关系改为从私人配置的发现根动态读取 `lifeos-project.json`：新项目无需先写 Work 即可被 Sessions、DChat 与 Git 识别，项目搬迁不再需要修复 Runtime 路径；Work 只按 `project_key` 保存个人跟踪覆盖层，并提供旧路径注册的一次性原子迁移。
- DChat 群聊正文的采集范围与项目关联统一由当前 `lifeos-project.json` 中的群 VID 派生，只维护一份项目来源关系。

## v1.0.0

> 发布日期：2026-08-29

### 新增

- 提供本地优先的 Work 账本，支持项目引用、事项、里程碑、待办、闪念、术语、成果胶囊、审计事件和原生简报；写入使用仅属主可访问的权限、完整事务与失败恢复。
- 提供 `lifeos work init`，使用本人确认的身份创建全新的 Schema 1 Runtime；已有 Work 数据时拒绝覆盖。
- 提供 Git 外私有配置、按需启用的内置模块和无副作用的 `lifeos capabilities` 能力检查。
- 提供 Schema 1 项目清单以及 DChat、Cooper 来源适配器；Cooper 链接只接受内部域名，项目身份和核心来源入口只在项目根维护一次。
- 提供 Sessions、Git 和 DChat 的只读证据采集，以及由 CLI 管理结构和确认状态的私有日报。
- 提供通用 LifeOS Agent Skill、Python 安装包、自动化测试、安全说明和 Apache-2.0 许可证。
