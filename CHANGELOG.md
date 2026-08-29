# 版本记录

本文档只记录明确版本给 LifeOS CLI 使用者带来的最终结果。尚未进入正式版本的用户可见变化合并写入 `Unreleased`；内部实现、验证过程和项目治理不进入版本说明。

## Unreleased

## v1.0.0

> 发布日期：2026-08-29

### 新增

- 提供本地优先的 Work 账本，支持项目引用、事项、里程碑、待办、闪念、术语、成果胶囊、审计事件和原生简报；写入使用仅属主可访问的权限、完整事务与失败恢复。
- 提供 `lifeos work init`，使用本人确认的身份创建全新的 Schema 1 Runtime；已有 Work 数据时拒绝覆盖。
- 提供 Git 外私有配置、按需启用的内置模块和无副作用的 `lifeos capabilities` 能力检查。
- 提供 Schema 1 项目清单以及 DChat、Cooper 来源适配器；Cooper 链接只接受内部域名，项目身份和核心来源入口只在项目根维护一次。
- 提供 Sessions、Git 和 DChat 的只读证据采集，以及由 CLI 管理结构和确认状态的私有日报。
- 提供通用 LifeOS Agent Skill、Python 安装包、自动化测试、安全说明和 Apache-2.0 许可证。
