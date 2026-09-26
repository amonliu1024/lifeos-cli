<p align="center">
  <img src="assets/brand/lifeos-logo.svg" alt="LifeOS" width="320">
</p>

<p align="center">
  <strong>做过的有记录，想明白的留下来。</strong><br>
  本地运行的个人工作系统：用子弹笔记的方式记录工作，由 Agent 起草日报和周期复盘。
</p>

<p align="center">
  <a href="https://github.com/amonliu1024/lifeos-cli/actions/workflows/test.yml"><img src="https://github.com/amonliu1024/lifeos-cli/actions/workflows/test.yml/badge.svg" alt="测试状态"></a>
  <img src="https://img.shields.io/badge/python-3.9%2B-blue" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/license-Apache--2.0-green" alt="Apache-2.0">
</p>

# LifeOS CLI

LifeOS 是一套完全跑在自己电脑上的个人工作系统。按天记下要做的、随手想到的、还没想通的和已经想明白的；一天结束时由 Agent 起草日报，隔一段时间再从日报里做周期复盘。它不需要账号，默认不离开本机，也不依赖任何第三方包——装好之后，你的工作记录就是本机上一组只属于你的文件；想在自己的服务器上留一份时，可以手动把记录、日报和只读工作台镜像过去。

## 它解决什么问题

一个人同时推进好几件事时，难的往往不是做，而是过几天说不清：做到哪了、卡在谁那儿、当时为什么这么定、踩过的坑到底学到了什么。计划在脑子里，进展在聊天里，结果散在提交和文档里，回头只能靠记忆重建。还有两种常见的误认：把「随口提过」当成「定了要做」，把「提交过代码」当成「这件事已经交付」。

LifeOS 把三样东西分开，并且不让它们互相推导。**工作记录**是你点过头的内容，只能通过 `lifeos work` 写入，每次写入都要求来源，状态变化要求原因，并留下一条不可改写的审计事件。**辅助证据**是本机的 Agent 会话、Git 提交、聊天记录和日历日程，只用来还原发生过什么，不会自己变成待办，也不会自己把事情标成做完。**报告**先把证据整理成由你确认的日报，再从已确认日报中形成周、月、季度、半年或年度总结。

所以证据被刻意做得比结论弱：本地提交只能证明提交存在，聊天消息只能证明有人说过这句话，两者都不能证明已经推送、部署或通过验证。LifeOS 宁可让你多确认一次，也不替你把推断记成事实。

## 它能帮你做什么

记法借自子弹笔记。每一笔只写一句话，前面一个符号：

- `•` **待办**：定了要做的事。做完打 `×`，推到以后某个月标 `<`，不做了就划掉并写一句为什么。等别人交付的事，也记成一条负责人是对方的待办。
- `–` **随记**：随手想到的点子、要记住的事。还没决定做，就不算待办；决定做了，再把它转成一条待办（`>`）。
- `?` **疑问**：要查、要问或要拍板的问题；想通了写下答案。
- `!` **洞见**：一句判断，加上让你想明白的那件事。只留你自己认过有用的；反复出现时写成规则，在批注里记下写进了哪里。

重要的事标星，紧急只看截止时间：`lifeos work brief` 按「重要 × 紧急」把你的待办分成四组，再列出等别人的、还没想通的和随记。每月初做一次月初盘点：一个月没动的、排到本月的和所有星标，逐笔给个去向——继续、推到以后、划掉，或者转成别的。懒得往下带的，多半本来就不必做。

每一笔可以挂在项目上，也可以不挂。项目不用手工登记，配一个发现根，LifeOS 自己扫出各项目根的 `lifeos-project.json`；项目的阶段和里程碑由项目自己的文档管理，LifeOS 只借它的名字来归组。仓库自带一个通用 `lifeos` Skill，平时跟 Agent 说一句「记一下……」「这个做完了」「这条留下」即可；Agent 只记你点过头的内容，账本的授权也不会顺带变成改代码、发消息或部署的授权。

## 快速开始

需要 Python 3.9+。检出仓库后安装并初始化：

```bash
pipx install .
lifeos config init
lifeos work init --self-name "你的名字" --source "本人确认"
```

然后就可以记待办、写日报、看当前进展。日常操作大多通过 Agent 完成：把 [skills/lifeos/](skills/lifeos/) 装进你的 Agent 后，直接说「写今天的日报」或「把这条记成待办」即可。完整命令与参数以 `lifeos --help` 和各子命令 `--help` 为准。

## 命令地图

- `lifeos work`：个人工作记录，待办、随记、疑问、洞见以及项目引用和术语的查询与写入
- `lifeos reports`：日报与周期报的落点、权限、frontmatter、草稿写入与确认状态
- `lifeos project`：校验项目工作区的 `lifeos-project.json`，从配置的发现根动态发现项目
- `lifeos sessions`：只读采集本机支持的 Agent 会话来源
- `lifeos git`：只读本地提交作为证据，全程不访问远端
- `lifeos dchat`：按需启用的 DChat 证据，通过显式配置的本机 wrapper 工作
- `lifeos calendar`：按需启用的 D-Chat 日历证据，复用同一个 wrapper，只读当天排了哪些会
- `lifeos config`：Git 外私人配置的初始化与校验
- `lifeos capabilities`：无副作用地查看本机哪些能力已就绪、已禁用或不可用
- `lifeos web`：在本机回环地址启动只读工作台
- `lifeos mirror`：把 Work 数据、日报与只读工作台单向镜像到自己的 SSH 服务器

## 只读 Web 工作台

`lifeos web` 在本机回环地址启动一个只读工作台，分今日、待办、疑问、洞见和日报五栏浏览。它不写入任何数据，也不能通过它改变状态。

## 服务器镜像

`lifeos mirror configure --target lab:lifeos-mirror` 在私有配置里记下一个 SSH 目标，之后每次手动运行 `lifeos mirror push`，就用 rsync 把两部分单向推过去：`data/` 是 Work 事实、审计事件和日报原文，`site/` 是在本机渲染好的只读工作台。目标机不需要安装 LifeOS，用静态文件服务以 `site/` 为站点根目录托管即可浏览，项目名称已在本机解析，例如用 Tailscale 给它一个固定端口的私有地址：`tailscale serve --bg --https=8443 ~/lifeos-mirror/site`。推送后的文件只有 SSH 账户本人可读，静态服务需以该账户或 root（如 tailscale serve）运行，其他普通用户读不到镜像内容。`--dry-run` 只列出将要变化的文件。派生视图、Sessions、Git、DChat 证据、备份与私有配置不在推送范围内。

## 工作模型

Work 的当前事实只有三类：按天记下的每一笔（`entries.json`）、按 `project_key` 保存的项目跟踪状态，以及实体名词。每一笔都是扁平的一行：

- 四种都有：`id`、`kind`、`text`、`project`（挂在哪个项目，可空）、`status`、`note`（当前状态的一句批注）、`ref`（当前状态指向的另一笔）、`context`（背景；洞见必填，就是来由）、`created_at`、`updated_at`。
- 待办另有 `starred`、`owner`（空表示本人）、`due`（结果的硬截止）、`month`（排到以后的月份）；疑问另有 `starred`。
- 状态只有一套值，每种记法用自己的叫法：待办是待做 / 排到以后 / 完成 / 划掉，随记是记着 / 转成别的 / 划掉，疑问是没想通 / 想通了 / 转成别的 / 划掉，洞见是有效 / 退役。完成、划掉、想通和退役都要写一句批注。

`events.jsonl` 是追加式审计历史：每次写入记下时间、操作者、动作、涉及的一笔、来源（`--source`）、状态的前后值和当时的批注，以及截止时间改动的前后日期与原因码。记录只保留当前那一句批注，之前怎么变过来的都在历史里，`lifeos work show ID` 会一并列出。

从 v1（事项、里程碑、待办、闪念、成果胶囊）升级时，用 `lifeos work migrate-v2 --plan` 查看需要你逐条决定的事项，填好决定文件后 `--apply`；执行前完整备份 Runtime，历史审计事件不改写。精确字段、状态和写入参数以相应命令的 `--help`、代码及测试为准，写入安全机制见 [ARCHITECTURE.md](ARCHITECTURE.md#安全属性)。

## 项目关系

项目位置不逐个写进 Runtime；项目身份只在项目根的 `lifeos-project.json` 中保存一次，再用 `lifeos config project-root add` 维护发现根让 LifeOS 自己找到它们。发现不跟随符号链接，同一 `project_key` 存在于多个路径时全部隔离，等待人工裁决。项目搬家后只要键不变，下一次发现直接使用新位置。

## 辅助证据

Sessions、Git、DChat 和 Calendar 四类证据全部只读来源、只写私有快照，既不改动来源，也不写入 Work。Sessions 适配器只读来源文件；Git 证据只读取显式注册的本地检出，不联系远端；DChat 群聊只在当前有效 `lifeos-project.json` 的 `sources.dchat.groups` 中声明时读取正文。Calendar 只读日程并保存私有快照，参会情况由本人确认。各来源的采集窗口、索引和 JSON 输出以 `lifeos sessions/git/dchat/calendar --help` 为准。

## 日报与周期报

日报正文由 Skill 的 Daily 分支生成，CLI 只拥有它的落点、权限、frontmatter、确认状态，以及重做时的旧稿留存。启用日历后，日报多一节「会议」，记当天你确认参加的会、每场服务哪个项目和总时长；日历只提供当天排了什么，去没去每次都由你确认，循环会议可以用 `lifeos calendar series set` 记下默认去或不去，作为下次提议的起点。周期报不重新扫描会话、提交或聊天，只读取目标周期内已确认的日报。`lifeos reports path --day <日期> --json` 是只读状态入口，返回该日在 Asia/Shanghai 的完整自然日窗口。重做留下的旧稿快照用 `lifeos reports prune` 查看，加 `--apply` 才删除，当前报告不在范围内。

## Agent Skill

[`skills/lifeos/`](skills/lifeos/) 是通用 LifeOS Skill 的唯一源码，Agent 通过它调用公共 CLI。安装、同步和恢复见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 架构与技术栈

纯 Python（3.9+），不依赖任何第三方包。命令层是唯一写入口，Work 事实、审计事件与派生视图共用一条链路：互斥锁、校验、临时文件、原子替换、幂等检查、审计追加和视图刷新属于同一次写入，不允许只完成一半。

数据分三处且方向单向：项目静态身份在各项目根的 `lifeos-project.json`，个人 Work 事实与报告在 `~/.local/share/lifeos/`，本机开关与来源路径在 `~/.config/lifeos/`。Sessions、Git、DChat 和 Calendar 四类证据只读来源、只写私有快照，不回写 Work。模块划分与安全属性见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 配置与数据边界

私有配置位于 Git 外 `~/.config/lifeos/config.json`（可用 `LIFEOS_CONFIG` 指向其他位置），个人工作事实与派生证据位于 `$LIFEOS_HOME`（默认 `~/.local/share/lifeos/`），两者均以 0700/0600 权限创建，不进入任何 Git 工作树。模块和来源适配器来自经过代码审查的静态注册表，配置只能启停内置能力，不接受凭据字段；当前能力及其就绪条件由 `lifeos capabilities` 直接返回。启用 DChat 用 `lifeos dchat configure` 写入 Git 外私有配置，不需要手工编辑 JSON。

## 开发

参与开发从 [ARCHITECTURE.md](ARCHITECTURE.md) 进入模块注册、数据 Owner 与证据流；验证入口是 `scripts/test.sh`，测试只使用合成 fixture 和临时 `LIFEOS_HOME`/`LIFEOS_CONFIG`，不得读取维护者的真实 Runtime。发布、版本与 Skill 同步见 [DEPLOYMENT.md](DEPLOYMENT.md)。

## 仓库结构

- [lifeos.py](lifeos.py)：CLI 入口
- [lifeos_work/](lifeos_work/)、[lifeos_reports/](lifeos_reports/)、[lifeos_sessions/](lifeos_sessions/)、[lifeos_git/](lifeos_git/)、[lifeos_dchat/](lifeos_dchat/)、[lifeos_calendar/](lifeos_calendar/)：Work 事实、日报周期报与各证据源模块
- [lifeos_config/](lifeos_config/)、[lifeos_projects/](lifeos_projects/)、[lifeos_web/](lifeos_web/)、[lifeos_mirror/](lifeos_mirror/)：配置、项目发现、只读 Web 工作台与服务器镜像
- [skills/lifeos/](skills/lifeos/)：唯一通用 Agent Skill 源码
- [tests/](tests/)：合成 fixture 与回归测试
- [ARCHITECTURE.md](ARCHITECTURE.md)、[DEPLOYMENT.md](DEPLOYMENT.md)、[CHANGELOG.md](CHANGELOG.md)：模块与数据边界、发布合同、版本记录
- [SECURITY.md](SECURITY.md)：本地数据边界与漏洞报告

## 许可证

采用 Apache License 2.0，详见 [LICENSE](LICENSE)。
