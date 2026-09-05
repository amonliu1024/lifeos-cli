<p align="center">
  <img src="assets/brand/lifeos-logo.svg" alt="LifeOS" width="320">
</p>

<p align="center">
  <strong>以数据，照见人生。</strong><br>
  让行动有迹，让经历成知。
</p>

<p align="center">
  <a href="https://github.com/amonliu1024/lifeos-cli/actions/workflows/test.yml"><img src="https://github.com/amonliu1024/lifeos-cli/actions/workflows/test.yml/badge.svg" alt="测试状态"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="Apache-2.0">
</p>

# LifeOS CLI

LifeOS 是一套完全跑在自己电脑上的个人工作系统，帮你把四件事记清楚：正在推进什么、下一步要交出什么结果、什么时候必须完成、完成之后留下了什么。它不需要账号，不同步到云端，也不依赖任何第三方包——装好之后，你的工作事实就是本机上一组只属于你的文件。

## 它解决什么问题

一个人同时推进多件事情时，真正的困难往往不是没做，而是说不清楚。计划在脑子里，进展在聊天里，结果散在提交和文档里，过几天回头只能靠记忆重建。更麻烦的是两种误认：把「我说过要做」当成「我答应了」，把「我提交过代码」当成「这件事已经交付」。

LifeOS 的回答是把三样东西分开，并且不让它们互相推导。**工作事实**是你本人确认过的承诺，只能通过 `lifeos work` 写入，每次写入都要求来源，状态变化要求原因，并留下一条不可改写的审计事件。**辅助证据**是本机的 Agent 会话、Git 提交和聊天记录，只用来还原发生过什么，不会自己变成待办，也不会自己关掉一条待办。**报告**先把证据整理成由你确认的日报，再从已确认日报中形成周、月、季度、半年或年度总结。

所以证据被刻意做得比结论弱：本地提交只能证明提交存在，聊天消息只能证明有人说过这句话，两者都不能证明已经推送、部署或通过验证。LifeOS 宁可让你多确认一次，也不替你把推断记成事实。

## 它能帮你做什么

一天结束时日报已经写好——Agent 按自然日窗口读取当天的会话、提交和待办变更，归并成几条真实的工作主线，你只需看一眼然后确认。一段时间过去后周期报从已确认的日报里把同一条主线重新连起来，看工作怎样变化。打开终端 `lifeos work brief` 只展示会改变当前行动的事实：当前事项停在哪个门槛、哪几条待办真的到期、有什么刚记下还没处理的闪念。

账本的规矩少而稳：闪念先记下来不产生责任，你确认提升它才成为事项或待办；只有结果硬截止过去才算逾期，改期要选原因码。项目不用手工登记，配一个发现根，LifeOS 自己扫出各项目根的 `lifeos-project.json`。仓库自带一个通用 `lifeos` Skill，Agent 通过公共命令读写，账本的授权不会顺带变成改代码、发消息或部署的授权。

## 快速开始

需要 Python 3.9+。检出仓库后安装并初始化：

```bash
pipx install .
lifeos config init
lifeos work init --self-name "你的名字" --source "本人确认"
```

然后就可以记待办、写日报、看当前进展。日常操作大多通过 Agent 完成：把 [skills/lifeos/](skills/lifeos/) 装进你的 Agent 后，直接说「写今天的日报」或「把这条记成待办」即可。完整命令与参数以 `lifeos --help` 和各子命令 `--help` 为准。

## 命令地图

- `lifeos work`：个人工作事实，项目引用、事项、里程碑、待办、闪念、术语、成果胶囊的查询与写入
- `lifeos reports`：日报与周期报的落点、权限、frontmatter、草稿写入与确认状态
- `lifeos project`：校验项目工作区的 `lifeos-project.json`，从配置的发现根动态发现项目
- `lifeos sessions`：只读采集本机支持的 Agent 会话来源
- `lifeos git`：只读本地提交作为证据，全程不访问远端
- `lifeos dchat`：按需启用的 DChat 证据，通过显式配置的本机 wrapper 工作
- `lifeos config`：Git 外私人配置的初始化与校验
- `lifeos capabilities`：无副作用地查看本机哪些能力已就绪、已禁用或不可用
- `lifeos web`：在本机回环地址启动只读工作台

## 只读 Web 工作台

`lifeos web` 在本机回环地址启动一个只读工作台，用来浏览当前工作、日报、闪念和成果。它不写入任何数据，也不能通过它改变状态。

## 工作模型

Work 对象沿着「项目引用 → 事项 → 里程碑 → 待办」组织，`events.jsonl` 只保存追加式审计证据，不是第二套用户对象。

- **项目引用**只登记 `project_key`、个人跟踪状态、原因和时间戳；当前名称、来源和目录从动态 Project Catalog 补全。
- **事项**承载单个动作无法表达的结果主线；轻量事项只保存最近门槛，路线事项只有一个当前里程碑。
- **待办**是执行层可关闭的可验证结果，区分当前行动、结果硬截止和实际开始事件。
- **闪念**承载尚未形成承诺的输入，只有指向本人已确认的事项或待办时才算完成提升。
- **成果胶囊**保存可复用结果、经验与来源，不参与责任、日期和提醒。

责任方必须是真实确认的本人、个人或组织，缺失时保持未知，不从上下文猜测。状态变化保留原因，历史审计事件不会为了适配新的当前 Schema 而被改写。精确字段、状态和写入参数以相应命令的 `--help`、代码及测试为准，写入安全机制见 [ARCHITECTURE.md](ARCHITECTURE.md#安全属性)。

## 项目关系

项目位置不逐个写进 Runtime；项目身份只在项目根的 `lifeos-project.json` 中保存一次，再用 `lifeos config project-root add` 维护发现根让 LifeOS 自己找到它们。发现不跟随符号链接，同一 `project_key` 存在于多个路径时全部隔离，等待人工裁决。项目搬家后只要键不变，下一次发现直接使用新位置。

## 辅助证据

Sessions、Git、DChat 三类证据全部只读来源、只写私有快照，既不改动来源，也不写入 Work。Sessions 适配器只读来源文件；Git 证据只读取显式注册的本地检出，不联系远端；DChat 群聊只在当前有效 `lifeos-project.json` 的 `sources.dchat.groups` 中声明时读取正文。各来源的采集窗口、索引和 JSON 输出以 `lifeos sessions/git/dchat --help` 为准。

## 日报与周期报

日报正文由 Skill 的 Daily 分支生成，CLI 只拥有它的落点、权限、frontmatter、确认状态，以及重做时的旧稿留存。周期报不重新扫描会话、提交或聊天，只读取目标周期内已确认的日报。`lifeos reports path --day <日期> --json` 是只读状态入口，返回该日在 Asia/Shanghai 的完整自然日窗口。

## Agent Skill

[`skills/lifeos/`](skills/lifeos/) 是通用 LifeOS Skill 的唯一源码，Agent 通过它调用公共 CLI。安装、同步和恢复见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 配置与数据边界

私有配置位于 Git 外 `~/.config/lifeos/config.json`（可用 `LIFEOS_CONFIG` 指向其他位置），个人工作事实与派生证据位于 `$LIFEOS_HOME`（默认 `~/.local/share/lifeos/`），两者均以 0700/0600 权限创建，不进入任何 Git 工作树。模块和来源适配器来自经过代码审查的静态注册表，配置只能启停内置能力，不接受凭据字段；当前能力及其就绪条件由 `lifeos capabilities` 直接返回。启用 DChat 用 `lifeos dchat configure` 写入 Git 外私有配置，不需要手工编辑 JSON。

## 开发

参与开发从 [ARCHITECTURE.md](ARCHITECTURE.md) 进入模块注册、数据 Owner 与证据流；验证入口是 `scripts/test.sh`，测试只使用合成 fixture 和临时 `LIFEOS_HOME`/`LIFEOS_CONFIG`，不得读取维护者的真实 Runtime。发布、版本与 Skill 同步见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 仓库结构

- [lifeos.py](lifeos.py)：CLI 入口
- [lifeos_work/](lifeos_work/)、[lifeos_reports/](lifeos_reports/)、[lifeos_sessions/](lifeos_sessions/)、[lifeos_git/](lifeos_git/)、[lifeos_dchat/](lifeos_dchat/)：Work 事实、日报周期报与各证据源模块
- [lifeos_config/](lifeos_config/)、[lifeos_projects/](lifeos_projects/)、[lifeos_web/](lifeos_web/)：配置、项目发现与只读 Web 工作台
- [skills/lifeos/](skills/lifeos/)：唯一通用 Agent Skill 源码
- [tests/](tests/)：合成 fixture 与回归测试
- [ARCHITECTURE.md](ARCHITECTURE.md)、[DEPLOYMENT.md](DEPLOYMENT.md)、[CHANGELOG.md](CHANGELOG.md)：模块与数据边界、发布合同、版本记录
- [SECURITY.md](SECURITY.md)：本地数据边界与漏洞报告

## 许可证

采用 Apache License 2.0，详见 [LICENSE](LICENSE)。
