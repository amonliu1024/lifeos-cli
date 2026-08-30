# LifeOS CLI 架构

本文描述当前仓库内部的模块、配置、存储、数据流和安全边界，并随 CLI 版本共同回滚。LifeOS 划分为三个信任区：公开代码仓库、本机私有配置和私有 Runtime；可复用行为进入版本管理，身份与本机路径通过配置提供，个人事实和辅助证据不进入 Git。

```text
lifeos-cli 仓库
  命令模块、适配器、校验、测试、通用 Agent Skill
                    |
                    v
~/.config/lifeos/config.json
  已启用的内置模块和本机来源配置
                    |
                    v
$LIFEOS_HOME（默认 ~/.local/share/lifeos）
  Work 权威事实、审计事件、证据存储、日报
```

## 能力组合

`lifeos_work.cli` 是命令组合根，`lifeos_modules.COMMAND_MODULES` 是顶层命令模块的静态注册表。静态注册让代码审查、打包和安全边界保持可见；配置不能指定导入路径，也不能加载任意代码。

可选能力在下一层使用相同模式：

- `lifeos_sessions.adapters.SESSION_SOURCES` 将受支持的来源名称映射到延迟加载的适配器工厂和来源根探测器。
- `lifeos_projects.sources.PROJECT_SOURCE_ADAPTERS` 负责项目清单中来源特有字段的校验。
- `lifeos_projects.catalog.ProjectCatalog` 从私人配置声明的根目录动态发现清单，隔离非法清单与同键冲突，并为 Work、Sessions、DChat 和 Git 提供唯一项目目录 seam。
- DChat 扫描从 Project Catalog 派生群 VID 并集；同一份清单关系同时决定群正文采集与项目索引，不用 Runtime 映射维护第二套 scope。
- `lifeos_config` 负责本机私有模块设置和无副作用的能力检查。
- `lifeos_web` 把现有 Work 与 Reports 读取入口投影为仅回环可达的只读页面；投影不落盘、不缓存，也不拥有任何个人事实。

因此，新增内置模块必须在同一个仓库内同时完成代码、注册、测试和文档；是否在某台机器上启用该模块，则始终是本机私有配置的选择。

## 数据 Owner

| 数据 | 唯一 Owner | 写入路径 |
| --- | --- | --- |
| 当前发布行为与 Schema | 仓库代码和文档 | 经审查的 Git 变更 |
| 项目身份与来源链接 | 项目根 `lifeos-project.json` | 项目维护者写入，再执行 `lifeos project validate` |
| 项目发现范围 | 私有配置 `modules.projects` | `lifeos config project-root` |
| 当前项目目录 | 动态 Project Catalog | 只读完整扫描；不持久化为第二事实源 |
| 已启用模块和本机路径 | 私有配置 | `lifeos config` 或模块配置命令 |
| 个人工作事实 | Work Runtime | 仅通过 `lifeos work` |
| Agent 会话派生数据 | Sessions Runtime | `lifeos sessions scan/rebuild/prune` |
| 本地提交证据 | Git Evidence Runtime | `lifeos git` |
| DChat 原始 revision 与索引 | DChat Runtime | `lifeos dchat scan` |
| 日报 | Reports Runtime | `lifeos reports` 与 Agent Skill |

`lifeos web serve` 是本地读取 adapter，不是新的数据 Owner。它只接受回环地址，浏览器通过同源接口取得一次性投影；打开日报原文时，服务端根据日期从 Reports Runtime 根目录重新推导规范路径，不接受浏览器传入文件路径。静态资源来自仓内固定 package，不提供通用文件服务。

派生 Markdown 和 SQLite 索引都不是替代事实源。Work 写入必须在同一个事务边界内完成互斥锁、校验、原子替换、审计事件和派生视图刷新。

新写入的幂等键从不可变来源身份派生，不使用时间或可变展示文字。已经存在的审计事件保持只追加，不为适配新的当前 Schema 批量改写。

## 项目清单

Schema 1 把稳定的项目外壳与来源适配器分开：

```json
{
  "schema_version": 1,
  "project_key": "example-project",
  "name": "示例项目",
  "aliases": [],
  "scope": "project",
  "sources": {
    "dchat": {"groups": []},
    "cooper": {"resources": []}
  }
}
```

核心层只校验项目身份和作用域，每个来源适配器只校验自己的 payload。当前仓库只接受 Schema 1，不包含公开前格式的读取 fallback 或迁移路径。

Project Catalog 扫描全部配置根，不跟随符号链接，也不从 Work 历史路径猜测项目。一个合法清单无需 Work 注册即可被证据模块消费；Work 的项目记录只保存 `PRJ-*`、`project_key` 和个人跟踪状态。项目移动后只要键不变且当前路径唯一，下一次扫描直接使用新位置；同键多路径全部隔离，单个非法或失联项目不会阻断其他项目。

## 证据流

```text
本机来源 --> 只读适配器 --> 规范化的私有 revision / index
                                      |
                                      v
                              有边界的 CLI 视图
                                      |
                                      v
                              Agent 解释 / 日报
```

来源适配器保留来源身份；输入不完整或未知时直接报告，不做猜测。Sessions、Git 和 DChat 证据不会写入 Work。日报工作流可以组合这些证据，但候选工作只有经过用户单独授权后才能成为 Work 事实。

DChat 的 `p2p / extp2p` 私聊全部进入正文采集；`channel / extchannel` 只有 VID 至少由一份当前有效项目清单声明时进入。多个项目声明同一 VID 时只采集一次，索引保留全部项目关联；VID 从全部清单退出只停止后续正文读取，既有不可变 revision 继续保留。

Sessions checkpoint 的 `cache_generation` 由来源 Adapter revision、shared extraction revision 和 Slice Schema 共同决定。来源特有解析变化只提升对应 Adapter revision，共享提取或判断语义变化提升 shared extraction revision；验证重扫是否生效应检查扫描报告的 `files_read` 与 `reused`，不能只依据成功退出码。

## 安全属性

- 配置 Schema 拒绝未知字段和疑似凭据字段，不充当通用秘密存储。
- Runtime 与配置默认位于仓库之外，并使用仅属主可访问的权限。
- 只读能力检查不会创建配置目录或 Runtime 目录。
- DChat 在显式配置前保持禁用。
- 测试只使用合成 fixture 和临时 home。
- 本地实现、提交、推送、发布、部署和目标环境验证是彼此独立的交付状态，不能互相推断。
