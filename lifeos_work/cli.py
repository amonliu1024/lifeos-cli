"""Argparse registration for the LifeOS Work domain.

Domain behavior lives in ``commands`` modules; Runtime initialization,
maintenance and the v2 migration remain owned by ``runtime`` and
``migration``. The CLI composition root is ``lifeos_modules``.
"""

import argparse

from .config import (
    ENTITY_KINDS,
    ENTRY_KINDS,
    ENTRY_STATUS_VALUES,
    PROJECT_TRACKING_STATES,
    SCHEDULE_REASON_CODES,
)
from .model import (
    validate_date,
    validate_half,
    validate_moment,
    validate_month,
    validate_nonempty_text,
    validate_quarter,
)
from .runtime import command_init, command_refresh, command_validate
from .migration import command_migrate_v2
from .commands.entries import (
    command_entries,
    command_entry_drop,
    command_entry_keep,
    command_entry_update,
    command_insight_add,
    command_note_add,
    command_question_add,
    command_question_answer,
    command_task_add,
    command_task_done,
    command_task_reschedule,
    command_task_schedule,
    command_task_schedule_history,
)
from .commands.glossary import command_glossary, command_term_add, command_term_update
from .commands.projects import command_project_track, command_project_update, command_projects
from .commands.reporting import (
    command_brief,
    command_changes,
    command_history,
    command_now,
    command_review,
    command_show,
)


WORK_DESCRIPTION = """管理 LifeOS 的个人工作记录：按天记下的每一笔、项目引用和实体名词。

每一笔用子弹笔记的四种符号：• 待办（定了要做的事）、– 随记（想到的、要记住的，还没决定做）、? 疑问（要查、要问或要拍板的问题）、! 洞见（本人认过有用的一句判断）。重要用星标，紧急只看截止时间。"""
WORK_EPILOG = """查询与写入约定：
  entries、projects、glossary 无过滤参数时返回全部记录；只有显式参数会缩小结果集。
  写入命令会在锁内校验并更新声明的事实源，同时刷新派生视图并追加不可变 events.jsonl；跨事实源写入会先备份完整 Runtime，refresh 只重建视图，validate 只读校验。
  --source 是本次记录的事实来源（可重复；多数写入必填），写进审计事件；--idempotency-key 是重试时使用的稳定键，命中后不重复写入。
  每一笔只保留当前状态的一句批注（--note）：完成了什么、为什么划掉、疑问的答案、洞见为什么退役；之前的状态和批注都在审计事件里，show 会一并列出。
  待办的 --due 是结果硬截止，只有 --due 过去才算逾期；7 天内到期算紧急。排到以后（task-schedule）只精确到月。等别人的事记成一条 --owner 是对方的待办。
  随记或疑问要转成别的一笔时，在新一笔的 add 命令上用 --from 指向它，原来那笔会标成转成别的。
  每月初用 brief --mode monthly 做一次月初盘点：久未变化的、排到本月的和所有星标，逐笔给个去向。"""


QUERY_EPILOG = "只读命令：不会修改事实源、events.jsonl 或派生视图。"
WRITE_EPILOG = "写入命令：会校验当前 Runtime，更新对应事实源，刷新派生视图并追加一条不可变事件；提供 --source 记录依据，重试请使用稳定的 --idempotency-key。"
INIT_EPILOG = "初始化只创建全新的当前 Work Runtime，不导入或覆盖任何旧数据；已有任一 Work 文件时立即停止。"
DATE_EPILOG = "日期语义：--due 只表示结果硬截止；过了才算逾期，7 天内到期算紧急。"
MIGRATE_EPILOG = (
    "先用 --plan 列出需要本人决定的事项（--plan --json 输出决定模板），"
    "填好后用 --apply --decisions 文件 --source 执行；执行前会完整备份 Runtime，"
    "v1 的事项、闪念、成果胶囊文件在迁移后移除，历史 events.jsonl 不改写。"
)


COMMAND_DESCRIPTIONS = {
    "init": "创建全新的当前 Work Runtime，并建立唯一的 ENT-SELF 本人实体；不读取或迁移既有 Runtime。",
    "now": "只读当前工作视图：待做、等别人、排到以后、没想通的和随记。",
    "brief": (
        "只读聊天窗口简报。current 按重要紧急四组平铺待办；reminder 只列逾期、7 天内到期和等别人；"
        "closeout 列 18:00 仍逾期的结果；monthly 是月初盘点，列出要逐笔给去向的记录。"
    ),
    "entries": "只读列出记下的每一笔，按日期倒序分组；无过滤参数时返回全部。",
    "projects": "只读项目引用列表；无过滤参数时返回全部项目引用。",
    "project-track": "按 Project Catalog 中唯一有效的 project_key 建立个人跟踪关系。",
    "project-update": "更新已跟踪项目的跟踪状态；paused 或 archived 必须同时提供 --reason。",
    "task-add": "记一条 • 待办：定了要做的事。默认本人负责；等别人的事用 --owner 写对方名字。",
    "note-add": "记一条 – 随记：想到的点子或要记住的事，还没决定做，不进待办。",
    "question-add": "记一条 ? 疑问：要查、要问或要拍板的问题，可以标星。",
    "insight-add": "留下一条 ! 洞见：一句判断，--context 写让你想明白的那件事（来由）。只记本人认过有用的。",
    "entry-update": "修改还没了结的一笔：正文、背景、批注、项目、星标；待办还可改负责人，排到以后的可用 --reopen 取回。",
    "task-done": "把待办标为完成 ×，--note 写完成了什么。",
    "task-schedule": "把待办排到以后某个月 <；到那个月之前不出现在当前简报。",
    "task-reschedule": "只调整待办的截止时间，并把每次变化追加到截止时间历史。",
    "task-schedule-history": "只读指定待办的截止时间历史。",
    "question-answer": "疑问想通了：--note 写答案；答案是一条洞见时用 --by 指向它。",
    "entry-drop": "划掉一笔，--note 写原因；洞见被新洞见取代时用 --by 指向新的那条。",
    "entry-keep": "月初盘点里选择「继续」：记一次复核，30 天重新计时，本月不再出现在盘点清单。",
    "glossary": "只读实体名词查询；无过滤参数时返回全部人员、组织、项目、系统和概念。",
    "term-add": "创建实体名词；--confirmed-at 省略时使用本地当前日期。",
    "term-update": "更新实体名词或追加别名和来源；改名时同步负责人写着旧名字的待办。",
    "show": "只读输出一条记录的原始 JSON，并列出这一笔的审计历史；可查项目（按 project_key）、某一笔或实体名词。",
    "history": "只读检索指定周期内完成的待办和完成批注；不指定周期时返回全部。",
    "review": "只读生成一个月度、季度或半年度复盘：完成的待办和新留下的洞见；必须显式指定一个周期。",
    "changes": "只读读取不可变变更记录；无时间窗默认返回最近 20 条，提供时间窗时默认返回窗口内全部事件。",
    "refresh": "重建当前派生 Markdown 视图；不改变事实 JSON 或 events.jsonl。",
    "validate": "只读校验当前事实源、项目跟踪关系、不可变变更记录和派生视图的一致性。",
    "migrate-v2": "一次性把 v1 Runtime（事项、里程碑、待办、闪念、成果胶囊）迁移为 v2：按天记的每一笔。",
}


COMMAND_SUMMARIES = {
    "init": "初始化新的 Work Runtime（写入）",
    "now": "显示当前工作视图（只读）",
    "brief": "生成聊天窗口简报（只读）",
    "entries": "列出记下的每一笔（无过滤时全量）",
    "projects": "列出项目引用（无过滤时全量）",
    "project-track": "跟踪已发现项目（写入）",
    "project-update": "更新项目引用（写入）",
    "task-add": "记一条 • 待办（写入）",
    "note-add": "记一条 – 随记（写入）",
    "question-add": "记一条 ? 疑问（写入）",
    "insight-add": "留下一条 ! 洞见（写入）",
    "entry-update": "修改一笔（写入）",
    "task-done": "完成待办 ×（写入）",
    "task-schedule": "把待办排到以后某个月 <（写入）",
    "task-reschedule": "调整待办截止时间（写入）",
    "task-schedule-history": "查看待办截止时间历史（只读）",
    "question-answer": "疑问想通了（写入）",
    "entry-drop": "划掉一笔（写入）",
    "entry-keep": "月初盘点里选择继续（写入）",
    "glossary": "查询实体名词（无过滤时全量）",
    "term-add": "创建实体名词（写入）",
    "term-update": "更新实体名词（写入）",
    "show": "显示一条记录与它的历史（只读）",
    "history": "检索已完成待办（只读）",
    "review": "生成周期复盘（只读）",
    "changes": "读取不可变变更记录（只读）",
    "refresh": "重建派生视图（写入视图）",
    "validate": "校验事实源、事件和视图（只读）",
    "migrate-v2": "迁移 v1 Runtime 到 v2（写入）",
}


QUERY_COMMANDS = {
    "now", "brief", "entries", "projects", "task-schedule-history", "glossary",
    "show", "history", "review", "changes", "validate",
}


def add_actor_arguments(parser, *, include_idempotency=True):
    parser.add_argument(
        "--actor-kind",
        choices=["agent", "user"],
        default="agent",
        metavar="KIND",
        help="记录操作者类型；默认 agent，人工确认时可用 user。",
    )
    parser.add_argument(
        "--actor-name",
        metavar="NAME",
        help="记录操作者名称；默认读取 LIFEOS_ACTOR，未设置时使用 Agent。",
    )
    if include_idempotency:
        parser.add_argument(
            "--idempotency-key",
            metavar="KEY",
            help="重试时使用稳定键；命中同一已处理事件后不会重复写入。",
        )


def add_source_argument(parser, required=False):
    parser.add_argument(
        "--source",
        action="append",
        default=[],
        required=required,
        metavar="SOURCE",
        help=(
            "本次记录的事实来源，可重复；"
            + ("必填。" if required else "提供时会记录到事实与事件。")
        ),
    )


def add_period_arguments(parser, required=False):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--month",
        type=validate_month,
        metavar="YYYY-MM",
        help="按本地日历月筛选；与 --quarter、--half 互斥。",
    )
    group.add_argument(
        "--quarter",
        type=validate_quarter,
        metavar="YYYY-QN",
        help="按本地日历季度筛选；与 --month、--half 互斥。",
    )
    group.add_argument(
        "--half",
        type=validate_half,
        metavar="YYYY-HN",
        help="按本地日历半年度筛选；与 --month、--quarter 互斥。",
    )
    parser.add_argument(
        "--project",
        metavar="PROJECT",
        help="按项目名称或项目 ID 筛选；不提供时不按项目过滤。",
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出，不改变查询范围。"
    )


def add_entry_common_arguments(parser, *, source_required=True, context_help=None):
    parser.add_argument(
        "--text", required=True, type=validate_nonempty_text, help="一句话正文。"
    )
    parser.add_argument("--project", metavar="KEY", help="挂在哪个已跟踪项目上（project_key）；可以不挂。")
    parser.add_argument(
        "--context",
        required=context_help is not None,
        type=validate_nonempty_text,
        help=context_help or "理解或执行需要的一段背景；可选。",
    )
    add_source_argument(parser, required=source_required)
    add_actor_arguments(parser)


def add_from_argument(parser):
    parser.add_argument(
        "--from",
        dest="from_id",
        metavar="ID",
        help="由一条还没了结的随记或疑问转化而来；原来那笔会标成转成别的，并指向这一笔。",
    )


def _annotate_work_parsers(commands):
    """Attach user-facing descriptions and epilogs to every Work parser."""

    for name, command in commands.choices.items():
        command.description = COMMAND_DESCRIPTIONS.get(name, command.description)
        command.formatter_class = argparse.RawDescriptionHelpFormatter
        if name == "init":
            command.epilog = INIT_EPILOG
        elif name == "refresh":
            command.epilog = "只写派生 Markdown 视图，不改变事实 JSON 或 events.jsonl。"
        elif name == "migrate-v2":
            command.epilog = MIGRATE_EPILOG
        elif name in QUERY_COMMANDS:
            command.epilog = QUERY_EPILOG
        elif name in {"task-add", "task-reschedule"}:
            command.epilog = f"{WRITE_EPILOG}\n{DATE_EPILOG}"
        else:
            command.epilog = WRITE_EPILOG
        for action in command._actions:
            if action.dest == "id" and not action.option_strings:
                action.metavar = "ID"
                action.help = action.help or "目标记录 ID。"
            if action.help is None:
                action.help = "该命令的可选字段；省略时保持当前值。"
    for action in commands._choices_actions:
        if action.dest in COMMAND_SUMMARIES:
            action.help = COMMAND_SUMMARIES[action.dest]


def register_work_parser(domains):
    """Register the ``work`` domain parser and all of its command handlers."""

    work = domains.add_parser(
        "work",
        help="管理个人工作记录（查询与写入）",
        description=WORK_DESCRIPTION,
        epilog=WORK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = work.add_subparsers(dest="command", required=True)

    command = commands.add_parser("init", help="初始化新的 Work Runtime")
    command.add_argument(
        "--self-name", required=True, type=validate_nonempty_text, help="本人规范名称"
    )
    command.add_argument(
        "--self-alias",
        action="append",
        default=[],
        type=validate_nonempty_text,
        help="本人别名，可重复提供",
    )
    add_source_argument(command, required=True)
    add_actor_arguments(command, include_idempotency=False)
    command.set_defaults(handler=command_init)

    command = commands.add_parser("now", help="显示当前工作视图")
    command.set_defaults(handler=command_now)

    command = commands.add_parser("brief", help="生成聊天窗口简报")
    command.add_argument(
        "--mode",
        choices=["current", "reminder", "closeout", "monthly"],
        required=True,
        help="current 当前简报；reminder 提醒；closeout 18:00 收口；monthly 月初盘点。",
    )
    command.set_defaults(handler=command_brief)

    command = commands.add_parser("entries", help="列出记下的每一笔")
    command.add_argument("--kind", choices=ENTRY_KINDS, help="按类型过滤：task 待办、note 随记、question 疑问、insight 洞见。")
    command.add_argument(
        "--status",
        choices=ENTRY_STATUS_VALUES,
        help="按状态过滤：open 待做/记着/没想通/有效，scheduled 排到以后，done 完成/想通了，converted 转成别的，dropped 划掉/退役。",
    )
    command.add_argument(
        "--live",
        action="store_true",
        help="只看还没了结的（以及仍有效的洞见）。",
    )
    command.add_argument("--starred", action="store_true", help="只看有星标的。")
    command.add_argument("--project", metavar="PROJECT", help="按项目名称或项目 ID 过滤。")
    command.add_argument("--on", type=validate_date, metavar="YYYY-MM-DD", help="只看记在这一天的。")
    command.add_argument("--query", metavar="TEXT", help="按正文、上下文、来由或回答里的文字过滤。")
    command.add_argument("--json", action="store_true", help="以 JSON 输出，不改变查询范围。")
    command.set_defaults(handler=command_entries)

    command = commands.add_parser("projects", help="显示项目引用")
    command.add_argument(
        "--tracking-state",
        choices=sorted(PROJECT_TRACKING_STATES),
        help="按 active/paused/archived 过滤；省略时返回全部项目引用。",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出，不改变查询范围。")
    command.set_defaults(handler=command_projects)

    command = commands.add_parser("project-track", help="跟踪已发现项目")
    command.add_argument("--project-key", required=True, help="Project Catalog 中的稳定项目键。")
    command.add_argument(
        "--tracking-state",
        choices=sorted(PROJECT_TRACKING_STATES),
        default="active",
        help="LifeOS 跟踪状态，默认 active；paused/archived 需要 --reason。",
    )
    command.add_argument("--reason", help="暂停或归档的原因。")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_project_track)

    command = commands.add_parser("project-update", help="更新项目引用")
    command.add_argument("project_key", metavar="KEY", help="已跟踪项目的 project_key。")
    command.add_argument(
        "--tracking-state",
        choices=sorted(PROJECT_TRACKING_STATES),
        help="LifeOS 跟踪状态；paused/archived 需要 --reason。",
    )
    command.add_argument("--reason", help="暂停或归档的原因。")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_project_update)

    command = commands.add_parser("task-add", help="记一条待办")
    add_entry_common_arguments(command)
    add_from_argument(command)
    command.add_argument("--star", action="store_true", help="标星：这件事重要。")
    command.add_argument("--due", type=validate_date, metavar="YYYY-MM-DD", help="结果硬截止日期。")
    command.add_argument(
        "--owner",
        type=validate_nonempty_text,
        metavar="NAME",
        help="负责人名字；省略表示你自己。写了别人，就进简报的「等别人」。",
    )
    command.set_defaults(handler=command_task_add)

    command = commands.add_parser("note-add", help="记一条随记")
    add_entry_common_arguments(command, source_required=False)
    command.set_defaults(handler=command_note_add)

    command = commands.add_parser("question-add", help="记一条疑问")
    add_entry_common_arguments(command)
    add_from_argument(command)
    command.add_argument("--star", action="store_true", help="标星：这个问题重要。")
    command.set_defaults(handler=command_question_add)

    command = commands.add_parser("insight-add", help="留下一条洞见")
    add_entry_common_arguments(
        command, context_help="来由：让你想明白的那件具体的事，1–3 句；必填。"
    )
    add_from_argument(command)
    command.add_argument(
        "--answers",
        metavar="ASK-ID",
        help="这条洞见回答了哪条疑问；该疑问会标成想通了并指向这条洞见。",
    )
    command.set_defaults(handler=command_insight_add)

    command = commands.add_parser("entry-update", help="修改一笔")
    command.add_argument("id", help="要改的那一笔的 ID。")
    command.add_argument("--text", type=validate_nonempty_text, help="新的正文。")
    context_change = command.add_mutually_exclusive_group()
    context_change.add_argument("--context", type=validate_nonempty_text, help="新的背景；洞见就是来由。")
    context_change.add_argument("--clear-context", action="store_true", help="清空背景（洞见不能清空）。")
    note_change = command.add_mutually_exclusive_group()
    note_change.add_argument("--note", type=validate_nonempty_text, help="当前状态的一句批注，例如洞见写进了哪条规则。")
    note_change.add_argument("--clear-note", action="store_true", help="清空批注。")
    project_change = command.add_mutually_exclusive_group()
    project_change.add_argument("--project", metavar="KEY", help="改挂到这个项目（project_key）。")
    project_change.add_argument("--clear-project", action="store_true", help="不再挂项目。")
    star_change = command.add_mutually_exclusive_group()
    star_change.add_argument("--star", action="store_true", help="标星（待办、疑问）。")
    star_change.add_argument("--unstar", action="store_true", help="去掉星标。")
    owner_change = command.add_mutually_exclusive_group()
    owner_change.add_argument("--owner", type=validate_nonempty_text, metavar="NAME", help="改成别人负责。")
    owner_change.add_argument("--mine", action="store_true", help="改回自己负责。")
    command.add_argument("--reopen", action="store_true", help="把排到以后的待办取回成待做。")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_entry_update)

    command = commands.add_parser("task-done", help="完成待办")
    command.add_argument("id", help="待办 ID（TASK-*）。")
    command.add_argument("--note", required=True, type=validate_nonempty_text, help="完成了什么。")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_done)

    command = commands.add_parser("task-schedule", help="把待办排到以后某个月")
    command.add_argument("id", help="待办 ID（TASK-*）。")
    command.add_argument("--month", required=True, type=validate_month, metavar="YYYY-MM", help="排到哪个月；必须是以后的月份。")
    command.add_argument("--note", type=validate_nonempty_text, help="为什么现在不做；可选。")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_schedule)

    command = commands.add_parser("task-reschedule", help="调整待办截止时间")
    command.add_argument("id", help="待办 ID（TASK-*）。")
    due_change = command.add_mutually_exclusive_group()
    due_change.add_argument("--due", type=validate_date, metavar="YYYY-MM-DD", help="新的结果硬截止日期。")
    due_change.add_argument("--clear-due", action="store_true", help="清除截止时间；必须同时给 --reason-code。")
    command.add_argument(
        "--reason-code",
        choices=sorted(SCHEDULE_REASON_CODES),
        help="延后或清除截止时间时的原因。",
    )
    command.add_argument("--note", help="日期变化的补充说明；需同时给 --reason-code。")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_reschedule)

    command = commands.add_parser("task-schedule-history", help="查看待办截止时间历史")
    command.add_argument("id", help="待办 ID（TASK-*）。")
    command.add_argument("--json", action="store_true", help="以 JSON 输出。")
    command.set_defaults(handler=command_task_schedule_history)

    command = commands.add_parser("question-answer", help="疑问想通了")
    command.add_argument("id", help="疑问 ID（ASK-*）。")
    command.add_argument("--note", required=True, type=validate_nonempty_text, help="答案。")
    command.add_argument("--by", metavar="INS-ID", help="答案是哪条洞见；可选。")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_question_answer)

    command = commands.add_parser("entry-drop", help="划掉一笔")
    command.add_argument("id", help="要划掉的那一笔的 ID。")
    command.add_argument("--note", required=True, type=validate_nonempty_text, help="为什么划掉；洞见就是为什么退役。")
    command.add_argument("--by", metavar="INS-ID", help="洞见被新洞见取代时，指向新的那条。")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_entry_drop)

    command = commands.add_parser("entry-keep", help="月初盘点里选择继续")
    command.add_argument("id", help="待办、随记或疑问的 ID。")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_entry_keep)

    command = commands.add_parser("glossary", help="查询人员、组织、项目、系统与专有概念")
    command.add_argument("query", nargs="?", help="可选查询文本；省略时不按文本过滤。")
    command.add_argument("--kind", choices=sorted(ENTITY_KINDS), help="按名词类型过滤。")
    command.add_argument("--json", action="store_true", help="以 JSON 输出。")
    command.set_defaults(handler=command_glossary)

    command = commands.add_parser("term-add", help="创建实体名词")
    command.add_argument("--name", required=True, help="名称。")
    command.add_argument("--kind", choices=sorted(ENTITY_KINDS), required=True, help="名词类型。")
    command.add_argument("--description", required=True, help="简短定义。")
    command.add_argument("--alias", action="append", default=[], help="别名，可重复。")
    command.add_argument(
        "--confirmed-at",
        type=validate_date,
        help="确认日期（YYYY-MM-DD）；省略时使用本地当前日期。",
    )
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_term_add)

    command = commands.add_parser("term-update", help="更新实体名词")
    command.add_argument("id", help="实体名词 ID（ENT-*）。")
    command.add_argument("--name", help="新名称；会同步负责人写着旧名字的待办。")
    command.add_argument("--kind", choices=sorted(ENTITY_KINDS), help="名词类型。")
    command.add_argument("--description", help="简短定义。")
    command.add_argument("--alias", action="append", default=[], help="追加别名，可重复。")
    command.add_argument(
        "--confirmed-at",
        type=validate_date,
        help="更新确认日期（YYYY-MM-DD）；省略时保持当前值不变。",
    )
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_term_update)

    command = commands.add_parser("show", help="显示一条记录与它的历史")
    command.add_argument("id", help="某一笔或名词的 ID，或项目的 project_key。")
    command.add_argument("--no-history", action="store_true", help="只输出记录本身，不列审计历史。")
    command.set_defaults(handler=command_show)

    command = commands.add_parser("history", help="检索已完成待办")
    add_period_arguments(command)
    command.set_defaults(handler=command_history)

    command = commands.add_parser("review", help="生成月度、季度或半年度复盘")
    add_period_arguments(command, required=True)
    command.set_defaults(handler=command_review)

    command = commands.add_parser("changes", help="读取内部变更记录")
    command.add_argument(
        "--from",
        dest="from_value",
        type=validate_moment,
        metavar="FROM",
        help="窗口起点，YYYY-MM-DD 表示当地零点，或带时区偏移的 ISO 时间戳",
    )
    command.add_argument(
        "--to", dest="to_value", type=validate_moment, metavar="TO", help="窗口终点，不含"
    )
    command.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="返回最近 N 条；不给时间窗时默认 20，给了时间窗时默认返回窗口内全部事件",
    )
    command.add_argument("--json", action="store_true", help="以 JSON 输出。")
    command.set_defaults(handler=command_changes)

    command = commands.add_parser("refresh", help="重建当前派生视图")
    command.set_defaults(handler=command_refresh)

    command = commands.add_parser("validate", help="验证当前事实源、内部审计与派生视图")
    command.set_defaults(handler=command_validate)

    command = commands.add_parser("migrate-v2", help="迁移 v1 Runtime 到 v2")
    step = command.add_mutually_exclusive_group(required=True)
    step.add_argument("--plan", action="store_true", help="只读列出迁移清单与需要本人决定的事项。")
    step.add_argument("--apply", action="store_true", help="按决定文件执行迁移；执行前完整备份。")
    command.add_argument("--json", action="store_true", help="与 --plan 一起用：输出决定模板 JSON。")
    command.add_argument("--decisions", metavar="FILE", help="填好的决定文件（--apply 必填）。")
    add_source_argument(command)
    add_actor_arguments(command, include_idempotency=False)
    command.set_defaults(handler=command_migrate_v2)

    _annotate_work_parsers(commands)
