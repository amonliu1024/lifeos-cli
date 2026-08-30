# Project 数字化工作流

通过项目根 `lifeos-project.json` 记录 LifeOS 与项目工作区需要共同识别的静态身份和核心协作入口。文件由项目单边维护，LifeOS、Sessions、DChat 只读取，不复制这些字段。

## 写入门槛与边界

只有本人明确要求为项目创建、更新或校验 LifeOS 清单时才写项目工作区。先读取目标目录适用的 `AGENTS.md` 和现有清单；`scope` 使用该项目治理口径中的 `project` 或 `project-group`，但不增加自动同步或额外读取链路。

清单只允许以下精确结构：

```json
{
  "schema_version": 1,
  "project_key": "stable-project-key",
  "name": "项目规范名称",
  "aliases": [],
  "scope": "project",
  "sources": {
    "dchat": {
      "groups": [
        {"vid": "123456", "name": "群名", "description": "与项目的关系"}
      ]
    },
    "cooper": {
      "resources": [
        {"link": "https://cooper.didichuxing.com/...", "name": "资源名", "description": "为什么是项目核心入口"}
      ]
    }
  }
}
```

不增加 `content_scope`、`since`、类型枚举、同步状态或其他推导字段。`scope` 只允许 `project` / `project-group`；`project_group` 不属于 v1 合同，发现后报告校验失败，不做转换。

## 来源准入

`sources.dchat.groups` 只维护与项目持续协作强相关的群聊。通过 DChat 当前会话信息核实 `vid`、群名和会话类型后写入；不维护单聊，也不把临时讨论群自动提升为核心群。`description` 只说明该群与项目的稳定关系。

`sources.cooper.resources` 统一承载协同文档、知识页面、协同表格和空间，不按资源类型拆字段。只纳入项目级核心入口，例如项目整体空间、需求/功能大盘、整体计划或唯一核心页面。普通 PRD、会议纪要、阶段材料、子目录和“某个库里所有文档”均不进入。判断不出读者会用它完成哪个项目级动作时保持不写。

每条来源都逐项判断；同一资源只写一处。名称和说明以当前已核实事实为准，不从文件名或旧记录猜测。

## 执行顺序

1. 确认项目根、当前 `AGENTS.md` scope、规范名称、别名和稳定 `project_key`。
2. 只读核实候选 DChat 群与 Cooper 核心入口；删除单聊、普通材料和整库枚举。
3. 创建或最小更新项目根 `lifeos-project.json`，不顺手改项目状态文档。
4. 运行 `lifeos project validate <项目根>`；失败时只修清单，不绕过 Schema。
5. 运行 `lifeos project discover --json`，确认该 `project_key` 在已配置发现根中唯一有效；未覆盖项目根时，只有本人明确授权维护本机发现范围后才运行 `lifeos config project-root add <稳定上级目录>`。
6. 若该项目尚未进入 Work 且本人同时授权个人跟踪，运行 `lifeos work project-track --project-key <project_key> --source <依据>`。项目搬迁不更新 Work 路径；保持键不变并重新发现即可。
7. 回读 `lifeos work projects --json`、`lifeos sessions projects --json` 和相关 `lifeos dchat projects list --json`，确认三处均由同一 Catalog 项目派生。

完成标准：清单位于正确项目根、精确通过校验并在 Catalog 中唯一可见；只含已核实的静态身份、核心群聊和核心 Cooper 入口；LifeOS 没有第二份静态项目内容需要维护。是否进入个人 Work 跟踪单独报告。
