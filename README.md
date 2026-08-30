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

一个人同时推进多件事情时，真正的困难往往不是没做，而是说不清楚。计划在脑子里，进展在聊天里，结果散在提交和文档里，过几天回头只能靠记忆重建。更麻烦的是两种误认：把“我说过要做”当成“我答应了”，把“我提交过代码”当成“这件事已经交付”。

LifeOS 的回答是把三样东西分开，并且不让它们互相推导。**工作事实**是你本人确认过的承诺，只能通过 `lifeos work` 写入，每次写入都要求来源，状态变化要求原因，并留下一条不可改写的审计事件。**辅助证据**是本机的 Agent 会话、Git 提交和聊天记录，只用来还原发生过什么，不会自己变成待办，也不会自己关掉一条待办。**日报**是对一天的解释，由证据生成，由你确认。

所以证据被刻意做得比结论弱：本地提交只能证明提交存在，聊天消息只能证明有人说过这句话，两者都不能证明已经推送、部署或通过验证。LifeOS 宁可让你多确认一次，也不替你把推断记成事实。

## 它能帮你做什么

### 一天结束时，日报已经写好了

写日报最费劲的从来不是写，是回忆。LifeOS 把回忆这一步交给确定性采集：CLI 先取得目标日的唯一自然日窗口，Agent 按这个窗口读取当天的 Agent 会话、Git 提交、IM聊天和待办变更，把它们归并成几条真实的工作主线——哪件事为什么做、判断怎么变的、最后停在哪一层，再写成正文。你要做的只是看一眼，然后 `lifeos reports confirm`。

它也不会替你把话说满。实现、本地测试、提交、推送、部署和目标环境验证分层记录，前一层不能推出后一层；证据不完整的地方会写清楚边界，而不是补一句听上去完整的话。日报里出现的候选待办同样不会自动进账本——确认日报和确认“我要做这件事”是两个动作。

### 打开终端，就知道现在该做什么

`lifeos work brief` 是每天用得最多的一屏，只展示会改变当前行动的事实：当前事项停在哪个门槛、哪几条待办真的到期、有什么刚记下还没处理的闪念。另外两种模式服务另外两个时刻，`--mode reminder` 只提醒需要关注的事，`--mode closeout` 用于一天收口。

### 其它顺手的地方

账本本身的规矩少而稳：闪念先记下来，不产生责任也不进提醒，你确认提升它才成为事项或待办；只有结果硬截止过去才算逾期，改期要选原因码，所以事后看得出一件事到底是被什么推迟的。

项目不用手工登记，配一个发现根，LifeOS 自己扫出各项目根的 `lifeos-project.json`，项目搬家只要键不变，下一次发现就跟上了。每次写入还会自动刷新一组 Markdown 视图（当前工作、项目、事项、闪念、成果、术语），用编辑器打开就能读，不必先想起命令怎么写。

仓库自带一个通用 `lifeos` Skill，Agent 通过公共命令读写，不直接碰你的数据文件，账本的授权也不会顺带变成改代码、发消息或部署的授权。

## 快速开始

需要 Python 3.9 或更高版本。检出仓库后推荐用 pipx 安装：

```bash
cd lifeos-cli
pipx install .
lifeos --version
```

创建本机私有配置，并确认当前可用能力：

```bash
lifeos config init
lifeos config validate
lifeos capabilities
```

初始化属于你自己的 Work Runtime，然后记下第一条待办：

```bash
lifeos work init \
  --self-name "你的名字" \
  --source "本人确认"

lifeos work task-add \
  --outcome "预约完成并收到确认短信" \
  --next-action "打电话确认可预约时段" \
  --due 2026-09-05 \
  --responsible-kind self \
  --source "本人确认"

lifeos work brief --mode current
```

`--outcome` 写可验证的结果而不是动作，这是 LifeOS 唯一比较“挑剔”的地方，也是后面所有复盘能成立的原因。写入类命令共享 `--source`、`--reason`、`--idempotency-key` 这几个约定，具体参数以各自 `--help` 为准：

```bash
lifeos work task-add --help
lifeos work task-close --help
lifeos work task-reschedule --help
```

想看全量记录时，基础列表在没有过滤参数时返回全部：`lifeos work tasks`、`work-items`、`ideas`、`achievements`、`projects`、`glossary`。

日常操作和日报大多是通过 Agent 完成的：把仓库里的 [`skills/lifeos/`](skills/lifeos/) 装进你的 Agent 之后，直接说“写今天的日报”或“把这条记成待办”即可，安装与同步见下文 [Agent Skill](#agent-skill)。

## 命令地图

| 领域 | 作用 |
| --- | --- |
| `lifeos work` | 个人工作事实：项目引用、事项、里程碑、待办、闪念、术语、成果胶囊，查询与写入都在这里 |
| `lifeos reports` | 日报的落点、权限、frontmatter、草稿写入与确认状态 |
| `lifeos project` | 校验项目工作区的 `lifeos-project.json`，并从配置的发现根动态发现项目 |
| `lifeos sessions` | 只读采集本机支持的 Agent 会话来源，维护私有派生索引 |
| `lifeos git` | 只读本地提交作为证据，全程不访问远端 |
| `lifeos dchat` | 按需启用的 DChat 证据，通过显式配置的本机 wrapper 工作 |
| `lifeos config` | Git 外私人配置的初始化与校验 |
| `lifeos capabilities` | 无副作用地查看本机哪些能力已就绪、已禁用或不可用 |
| `lifeos web` | 在本机回环地址启动只读工作台，浏览工作、日报、闪念与成果 |

## 只读 Web 工作台

```bash
lifeos web serve --open
```

工作台只监听本机回环地址，默认打开 `http://localhost:8787/`。顶部四个 Tab 分别浏览工作、日报、闪念和成果；工作页默认隐藏完成记录，日报页可以显式交给系统默认应用打开原文。页面不提供 Agent、编辑或状态流转，也不建立第二套数据：每次刷新都通过现有 Work 与 Reports 读取入口取得当前事实。

## 工作模型

Work 对象沿着“项目引用 → 事项 → 里程碑 → 待办”组织，`events.jsonl` 只保存追加式审计证据，不是第二套用户对象。

- **项目引用**只登记 `project_key`、个人跟踪状态、原因和时间戳；当前名称、来源和目录从动态 Project Catalog 补全。清单被发现不等于自动创建个人跟踪关系。
- **事项**承载单个动作无法表达的结果主线。轻量事项可以直接保存最近门槛，不建立里程碑；路线事项必须只有一个当前里程碑，未来阶段保持计划状态。
- **待办**是执行层可关闭的可验证结果。它区分当前行动、结果硬截止和实际开始事件；关联事项后继承其项目，路线事项中的未完成待办只属于当前里程碑。
- **闪念**承载尚未形成承诺的输入，只有指向本人已确认的事项或待办时才算完成提升。
- **成果胶囊**保存可复用结果、经验与来源，可以关联已完成待办，但不参与责任、日期和提醒。

责任方必须是真实确认的本人、个人或组织；缺失时保持未知，不从上下文猜测。等待、暂停、取消、关闭、归档和替换按命令契约保留原因，历史审计事件不会为了适配新的当前 Schema 而被改写。普通写入在锁内完成校验、原子替换、审计事件和视图刷新，跨事实源写入前先备份整个 Runtime。精确字段、状态和写入参数以相应命令的 `--help`、代码及测试为准。

## 项目关系

项目位置不逐个写进 Runtime。项目身份只在项目根的 `lifeos-project.json` 中保存一次：

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

再用公共命令维护一个或多个发现根，让 LifeOS 自己找到它们：

```bash
lifeos config project-root add /absolute/path/to/projects
lifeos project discover
lifeos project validate --all

lifeos work project-track \
  --project-key example-project \
  --source "本人确认纳入个人跟踪"
```

发现不跟随符号链接，并跳过配置中的排除目录；同一 `project_key` 同时存在于多个路径时全部隔离，等待人工裁决。每种来源的字段由对应适配器负责，未知适配器和未知字段会被拒绝。项目搬家后只要键不变，下一次发现直接使用新位置。

## 辅助证据

下面这几类证据主要服务于日报：采集全部只读来源、只写私有快照，既不改动来源，也不写入 Work。

Sessions 适配器只读来源文件，不会修改它们；启用哪些来源由本机私有配置决定：

```bash
lifeos sessions scan --source codex \
  --from 2026-08-28T00:00:00+08:00 \
  --to 2026-08-29T00:00:00+08:00 --json

lifeos sessions index \
  --from 2026-08-28T00:00:00+08:00 \
  --to 2026-08-29T00:00:00+08:00 --json
```

Git 证据只读取显式注册的本地检出，不会联系远端：

```bash
lifeos git repos add --key example --root /path/to/repository
lifeos git scan --from 2026-08-28 --to 2026-08-29 --json
```

DChat 是按需启用的辅助证据。私聊按结构化会话类型采集；群聊只在当前有效 `lifeos-project.json` 的 `sources.dchat.groups` 中声明时读取正文：

```bash
lifeos dchat validate --json
lifeos dchat scan \
  --from 2026-08-28T00:00:00+08:00 \
  --to 2026-08-29T00:00:00+08:00 --json
```

## 日报

日报正文由 Skill 的 Daily 分支生成，CLI 只拥有它的落点、权限、frontmatter、确认状态，以及重做时的旧稿留存。

```bash
lifeos reports path --day 2026-08-28 --json      # 只读：状态与当日窗口
lifeos reports begin --day 2026-08-28 --json     # 建立草稿；已确认的日报需要 --redo
lifeos reports write --day 2026-08-28 --body-file /tmp/daily.md
lifeos reports confirm --day 2026-08-28
lifeos reports validate
```

`reports path --json` 是只读状态入口，返回该日在 Asia/Shanghai 的完整自然日窗口；已确认日报需要重新采集证据时直接使用这个窗口，不必覆盖日报。

## Agent Skill

[`skills/lifeos/`](skills/lifeos/) 是由仓库维护的通用 Skill：Agent 先探测本机能力，再判断请求属于项目、Work 还是日报，只加载该分支需要的说明。CLI 版本、制品、SmartWork / cc-switch 同步与回滚统一按 [`DEPLOYMENT.md`](DEPLOYMENT.md) 执行；个人偏好不会写回仓库 Skill，同步动作也不能推导出真实 Runtime 已切换。

## 配置与数据边界

私有配置默认位于 `~/.config/lifeos/config.json`，可用 `LIFEOS_CONFIG` 指向其他位置；目录和文件分别以 `0700`、`0600` 权限创建。

```json
{
  "schema_version": 1,
  "timezone": "Asia/Shanghai",
  "modules": {
    "dchat": {
      "enabled": false,
      "dws_wrapper": null
    },
    "sessions": {
      "sources": ["codex", "claude", "smartwork", "deepseek"]
    },
    "project_sources": {
      "enabled": ["dchat", "cooper"]
    },
    "projects": {
      "roots": [],
      "exclude": [".git", ".venv", "archive", "node_modules"]
    }
  }
}
```

模块和来源适配器来自经过代码审查的静态注册表。配置只能启用或停用内置能力，不能要求 LifeOS 从配置中导入任意 Python 模块，也不接受凭据字段。

| 能力 | 默认状态 | 就绪条件 |
| --- | --- | --- |
| Work、Reports | 启用 | 内置能力 |
| Project Catalog | 未配置 | 至少一个可读的项目发现根 |
| Git 证据 | 启用 | 本机可使用 `git` |
| Sessions 来源 | 启用 | 已配置的来源目录存在 |
| DChat 证据 | 禁用 | 已启用，并配置存在的本地 wrapper；群聊范围来自项目清单 |
| 项目清单中的 DChat / Cooper 来源 | 启用 | 内置 Schema 1 适配器 |

启用 DChat 不需要手工编辑 JSON，该命令只写入 Git 外的私有配置，证据仍保存在 `$LIFEOS_HOME/dchat/`：

```bash
lifeos dchat configure \
  --dws-wrapper </absolute/path/to/dws-wrapper>
```

`LIFEOS_HOME` 默认指向 `~/.local/share/lifeos/`。它是个人工作事实和派生证据的权威来源，不得放进任何 Git 工作树：

```text
Git 仓库
  源码、测试、文档、通用 Skill

~/.config/lifeos/
  config.json           本机模块配置
  agent-profile.md      可选的用户专属 Agent 偏好

$LIFEOS_HOME/
  Work 事实与审计事件
  Sessions / Git / DChat 证据
  日报
```

仓库不会创建或管理可选的 `agent-profile.md`。通用 Skill 可以读取其中的称呼和输出偏好，但该文件不能授予写入或外部操作权限。

所有公开 JSON 合同和持久化数据从 Schema 1 开始（项目跟踪事实源使用 Schema 2，通过 `lifeos work migrate-project-catalog` 从已发布的项目 Schema 1 原子迁移）。公开前的数据不读取、不迁移、不覆盖；切换真实个人 Runtime 是独立的数据操作，不由安装、提交或 Tag 自动完成。

## 开发与验证

参与开发时可以使用可编辑虚拟环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
lifeos --help
```

测试只使用合成 fixture 和临时 `LIFEOS_HOME` / `LIFEOS_CONFIG`，不得读取维护者的真实 Runtime。

```bash
./scripts/test.sh -v
PYTHONPYCACHEPREFIX=/tmp/lifeos-pycache \
  python3 -m compileall -q \
  lifeos.py lifeos_modules.py lifeos_config lifeos_projects lifeos_work \
  lifeos_sessions lifeos_git lifeos_dchat lifeos_reports lifeos_web tests
python3 lifeos.py --help
python3 lifeos.py capabilities --json
python3 lifeos.py project --help
python3 lifeos.py work init --help
git diff --check
```

## 深入阅读

- [架构](ARCHITECTURE.md)：模块注册、数据 Owner、证据流与安全属性
- [部署与发布](DEPLOYMENT.md)：版本源、候选核验、Skill 同步与恢复
- [安全说明](SECURITY.md)：本地数据边界与漏洞报告方式
- [版本记录](CHANGELOG.md)：每个版本给使用者带来的最终结果

## 许可证

采用 Apache License 2.0，详见 [LICENSE](LICENSE)。
