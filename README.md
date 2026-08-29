# LifeOS CLI

LifeOS 是一套本地优先的命令行系统，用来管理个人工作事实、确定性证据和经本人确认的日报。本仓库只保存可复用的产品能力；个人数据、本机路径、私有来源标识和用户专属的 Agent 偏好始终位于 Git 之外。

它解决的是同一个人在多个项目、Agent 和证据来源之间推进工作时，事实容易散落、状态容易靠记忆、提交或聊天又容易被误当成交付证明的问题。LifeOS 把工作事实、辅助证据和文字解释分开管理；Work 事实只能通过公共命令写入并记录审计，其他数据按各自 Owner 的受控路径管理。

`v1.0.0` 是首个公开稳定基线。它不负责发送消息、部署代码、修改 Git 仓库，也不会把聊天表述或本地提交推断为已经上线。

## 主要能力

- `lifeos work`：管理项目、事项、里程碑、待办、闪念、术语和可复用成果胶囊。
- `lifeos reports`：管理私有日报的路径、frontmatter、草稿写入、确认状态和校验。
- `lifeos project`：校验可移植的 `lifeos-project.json`。
- `lifeos sessions`：通过只读适配器采集本机支持的 Agent 会话来源。
- `lifeos git`：采集本地提交证据并保存私有扫描快照，全程不访问远端。
- `lifeos dchat`：按需启用 DChat 辅助证据，通过显式配置的本地 wrapper 工作。
- `lifeos capabilities`：无副作用地展示当前机器上哪些能力已就绪、已禁用或不可用。

## 安装

需要 Python 3.9 或更高版本。下载或检出仓库后，推荐通过 pipx 安装：

```bash
cd lifeos-cli
pipx install .
lifeos --version
```

参与开发时也可以使用可编辑虚拟环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
lifeos --help
```

## 配置

先创建本机私有配置：

```bash
lifeos config init
lifeos config validate
lifeos capabilities
```

默认路径是 `~/.config/lifeos/config.json`；如需使用其他位置，可设置 `LIFEOS_CONFIG`。目录和文件会分别以 `0700`、`0600` 权限创建。

```json
{
  "schema_version": 1,
  "timezone": "Asia/Shanghai",
  "modules": {
    "dchat": {
      "enabled": false,
      "dws_wrapper": null,
      "attention_tag_id": null
    },
    "sessions": {
      "sources": ["codex", "claude", "smartwork", "deepseek"]
    },
    "project_sources": {
      "enabled": ["dchat", "cooper"]
    }
  }
}
```

模块和来源适配器来自经过代码审查的静态注册表。配置只能启用或停用内置能力，不能要求 LifeOS 从配置中导入任意 Python 模块。

| 能力 | 默认状态 | 就绪条件 |
| --- | --- | --- |
| Work、Project、Reports | 启用 | 内置能力 |
| Git 证据 | 启用 | 本机可使用 `git` |
| Sessions 来源 | 启用 | 已配置的来源目录存在 |
| DChat 证据 | 禁用 | 已启用，并配置存在的本地 wrapper 和 tag ID |
| 项目清单中的 DChat / Cooper 来源 | 启用 | 内置 Schema 1 适配器 |

无需手工编辑 JSON 即可启用 DChat：

```bash
lifeos dchat configure \
  --attention-tag-id <OPAQUE-TAG-ID> \
  --dws-wrapper </absolute/path/to/dws-wrapper>
```

该命令只写入 Git 外的私有配置；证据仍保存在 `$LIFEOS_HOME/dchat/`。

## Runtime 边界

`LIFEOS_HOME` 默认指向 `~/.local/share/lifeos/`。它是个人工作事实和派生证据的权威来源，不得放进任何 Git 工作树。

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

## 快速开始

使用 CLI 原生视图查看当前工作：

```bash
lifeos work init \
  --self-name "你的名字" \
  --source "本人确认"
lifeos work brief --mode current
lifeos work brief --mode reminder
lifeos work brief --mode closeout
lifeos work projects
lifeos work work-items
lifeos work tasks
lifeos work ideas
lifeos work achievements
```

写命令需要的来源、原因和幂等参数以各自的 `--help` 为准：

```bash
lifeos work task-add --help
lifeos work task-update --help
lifeos work task-close --help
lifeos work task-reschedule --help
```

项目身份只在项目根的 `lifeos-project.json` 中保存一次：

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

```bash
lifeos project validate /path/to/project
lifeos work project-add \
  --manifest /path/to/project \
  --source "已确认的项目注册"
```

每种来源的字段由对应适配器负责；未知适配器和未知字段会被拒绝。

## 工作模型

Work 对象沿着“项目引用 → 事项 → 里程碑 → 待办”组织，`events.jsonl` 只保存追加式审计证据，不是第二套用户对象。

- **项目引用**只登记项目清单路径、个人跟踪状态、原因和时间戳；产品计划、交付状态与正式材料仍由项目自身维护。
- **事项**承载单个动作无法表达的结果主线。轻量事项可以直接保存最近门槛，不建立里程碑；路线事项必须只有一个当前里程碑，未来阶段保持计划状态。
- **待办**是执行层可关闭的可验证结果。它区分当前行动、结果硬截止和实际开始事件；关联事项后继承其项目，路线事项中的未完成待办只属于当前里程碑。
- **闪念**承载尚未形成承诺的输入，只有指向本人已确认的事项或待办时才算完成提升。
- **成果胶囊**保存可复用结果、经验与来源，可以关联已完成待办，但不参与责任、日期和提醒。

责任方必须是真实确认的本人、个人或组织；缺失时保持未知，不从上下文猜测。等待、暂停、取消、关闭、归档和替换按命令契约保留原因，历史审计事件不会为了适配新的当前 Schema 而被改写。

基础列表在没有显式过滤条件时返回全部记录；`brief --mode current`、`reminder` 和 `closeout` 分别是当前工作、提醒和日终收口的原生视图。精确字段、状态和写入参数以相应命令的 `--help`、代码及测试为准。

## 辅助证据

Sessions 适配器只读来源文件，不会修改它们。启用哪些来源由本机私有配置决定：

```bash
lifeos sessions scan \
  --source codex \
  --from 2026-08-28T00:00:00+08:00 \
  --to 2026-08-29T00:00:00+08:00 \
  --json

lifeos sessions index \
  --from 2026-08-28T00:00:00+08:00 \
  --to 2026-08-29T00:00:00+08:00 \
  --json
```

Git 证据只读取显式注册的本地检出，不会联系远端：

```bash
lifeos git repos add --key example --root /path/to/repository
lifeos git scan --from 2026-08-28 --to 2026-08-29 --json
```

DChat 是按需启用的辅助证据：

```bash
lifeos dchat validate --json
lifeos dchat scan \
  --from 2026-08-28T00:00:00+08:00 \
  --to 2026-08-29T00:00:00+08:00 \
  --json
```

辅助证据刻意弱于交付结论：本地提交只能证明提交存在，聊天消息只能证明有人说过这句话；两者都不能证明已经推送、部署或通过目标环境验证。

## 日报

CLI 负责私有文件、权限、frontmatter 和确认状态，Agent Skill 负责生成正文。除非用户另行授权，日报中的候选内容不会写入 Work。

```bash
lifeos reports path --day 2026-08-28 --json
lifeos reports begin --day 2026-08-28 --json
lifeos reports write --day 2026-08-28 --body-file /tmp/daily.md
lifeos reports confirm --day 2026-08-28
lifeos reports validate
```

`reports path --json` 是只读状态入口，并返回该日在 Asia/Shanghai 的完整自然日窗口；已确认日报需要重新采集证据时使用该窗口，不必覆盖日报。

## Agent Skill

[`skills/lifeos/`](skills/lifeos/) 是由仓库维护的通用 Skill。CLI 版本、制品、SmartWork / cc-switch 同步与回滚统一按 [`DEPLOYMENT.md`](DEPLOYMENT.md) 执行；不会把个人偏好写回仓库 Skill，也不会从同步推导真实 Runtime 已切换。

## v1 基线

所有公开 JSON 合同和持久化数据从 Schema 1 开始。CLI 不读取、不迁移也不覆盖任何公开前 Runtime、配置或项目清单；已有本地数据时，请保留原目录并使用新的 `LIFEOS_HOME` / `LIFEOS_CONFIG` 初始化 v1。切换真实个人 Runtime 是独立的数据操作，不由安装、提交或 Tag 自动完成。

本仓库只保留当前实现与发布合同，不建立 `archive/`。退出当前职责但仍有追溯价值的项目材料由仓库之外的 Project Workspace 统一归档。

## 深入阅读

- [架构](ARCHITECTURE.md)
- [部署与发布](DEPLOYMENT.md)
- [安全说明](SECURITY.md)

## 开发与验证

测试只使用合成 fixture 和临时 `LIFEOS_HOME` / `LIFEOS_CONFIG`，不得读取维护者的真实 Runtime。

```bash
./scripts/test.sh -v
PYTHONPYCACHEPREFIX=/tmp/lifeos-pycache \
  python3 -m compileall -q \
  lifeos.py lifeos_modules.py lifeos_config lifeos_projects lifeos_work \
  lifeos_sessions lifeos_git lifeos_dchat lifeos_reports tests
python3 lifeos.py --help
python3 lifeos.py capabilities --json
python3 lifeos.py project --help
python3 lifeos.py work init --help
git diff --check
```

## 许可证

采用 Apache License 2.0，详见 [LICENSE](LICENSE)。
