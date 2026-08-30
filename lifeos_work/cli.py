"""Argparse composition root for the LifeOS Work CLI.

Domain behavior lives in ``commands`` modules; Runtime initialization and
maintenance remain owned by the Runtime module.
"""

import argparse

from .config import (
    ACHIEVEMENT_LIFECYCLES,
    DATA_DIR,
    ENTITY_KINDS,
    IDEA_STATUSES,
    MILESTONE_DECISIONS,
    MILESTONE_STATUSES,
    PROJECT_TRACKING_STATES,
    SCHEDULE_REASON_CODES,
    TASK_STATUSES,
    VALUE_TYPES,
    WORK_ITEM_STATES,
)
from .model import (
    validate_date,
    validate_half,
    validate_moment,
    validate_month,
    validate_nonempty_text,
    validate_quarter,
)
from .runtime import (
    command_init,
    command_migrate_project_catalog,
    command_refresh,
    command_validate,
)
from .commands.achievements import (
    command_achievement_add,
    command_achievement_archive,
    command_achievement_supersede,
    command_achievement_update,
    command_achievements,
)
from .commands.glossary import command_glossary, command_term_add, command_term_update
from .commands.ideas import command_idea_add, command_idea_update, command_ideas
from .commands.projects import command_project_track, command_project_update, command_projects
from .commands.reporting import (
    command_brief,
    command_changes,
    command_history,
    command_now,
    command_review,
    command_show,
)
from .commands.tasks import (
    command_task_add,
    command_task_close,
    command_task_reflect,
    command_task_reschedule,
    command_task_schedule_history,
    command_task_start,
    command_task_update,
    command_tasks,
)
from .commands.work_items import (
    command_work_item_add,
    command_work_item_milestone_add,
    command_work_item_milestone_update,
    command_work_item_milestones,
    command_work_item_update,
    command_work_items,
)


ROOT_DESCRIPTION = "人生 OS / LifeOS CLI：管理个人工作事实、Agent 会话与本地协作证据、日报状态。"
ROOT_EPILOG = """领域边界：
  project  校验项目工作区的 lifeos-project.json；不写 Private Runtime。
  work     个人工作事实（项目引用、事项、待办、闪念、成果胶囊）；查询与写入都在此域。
  sessions 只读 Agent 应用来源，维护私有派生索引；不写来源日志、Work 或日报。
  git      只读本地 Git 提交，维护日报辅助证据快照；不修改仓库、remote、Work 或日报正文。
  dchat    只读 p2p / extp2p 私聊与关注群聊，维护私有原始证据；不发送消息、修改标签或写 Work。
  reports  日报落点、frontmatter 与确认状态；正文由 lifeos Skill 的 Daily 分支维护。

所有时间窗口使用半开区间 [from, to)；具体参数、默认值和写入边界以对应子命令 --help 为准。"""

WORK_DESCRIPTION = """管理 LifeOS 的个人工作事实：项目引用、事项、里程碑、待办、闪念、实体名词和成果胶囊。

先用查询命令读取当前事实，再用写入命令记录本人确认的变化。事项是工作脉络，待办是唯一可关闭的执行结果；成果胶囊是可选的复用资产。"""
WORK_EPILOG = """查询与写入约定：
  基础列表 projects、work-items、tasks、ideas、achievements、glossary 无过滤参数时返回全部记录；只有显式参数会缩小结果集。
  普通写入命令会在锁内校验并更新声明的事实源，同时刷新派生视图并追加不可变 events.jsonl；跨事实源写入会先备份完整 Runtime，refresh 只重建视图，validate 只读校验。
  --source 是本次记录的事实来源（可重复；创建和有标注的状态变化通常必填）；--idempotency-key 是重试时使用的稳定键，命中后不重复写入。
  状态原因按对象约束：project 的 tracking_state=paused/archived、work-item 的 state=waiting/needs_confirmation/paused/closed、task 的 status=waiting/paused/cancelled、idea 的 status=archived，以及 achievement 的 archive/supersede 都必须保存 --reason；其它状态变化不会自动补原因。里程碑另按完成信息和 decision 规则校验。
  待办的 --due 是结果硬截止，只有 --due 过去才算结果逾期；开始推进日期由 task_started 事件记录，里程碑目标日期由事项的 target_at 承担。"""


QUERY_EPILOG = "只读命令：不会修改事实源、events.jsonl 或派生视图。"
WRITE_EPILOG = "写入命令：会校验当前 Runtime，更新对应事实源，刷新派生视图并追加一条不可变事件；提供 --source 记录依据，重试请使用稳定的 --idempotency-key。"
INIT_EPILOG = "初始化只创建全新的当前 Work Runtime，不导入或覆盖任何旧数据；已有任一 Work 文件时立即停止。"
DATE_EPILOG = "日期语义：--due 只表示结果硬截止；里程碑目标日期由事项 target_at 承担。"
MILESTONE_EPILOG = "里程碑边界：completed 必须同时具备 --summary、--completion-source 和 --decision；decision=continue/adjust 时还必须用 --activate-next 指向同一事项的 planned 里程碑。"


COMMAND_DESCRIPTIONS = {
    "init": "创建全新的当前 Work Runtime，并建立唯一的 ENT-SELF 本人实体；不读取或迁移既有 Runtime。",
    "now": "只读当前工作视图，按当前有效事实展示需要关注的项目、事项和待办。",
    "brief": (
        "只读聊天窗口简报。current 展示当前前瞻；reminder 展示通用提醒；"
        "closeout 展示 18:00 收口。"
    ),
    "projects": "只读项目引用列表；无过滤参数时返回全部项目引用。",
    "project-track": "按 Project Catalog 中唯一有效的 project_key 建立个人跟踪关系。",
    "project-update": "更新已有项目引用的跟踪状态；paused 或 archived 必须同时提供 --reason。",
    "migrate-project-catalog": "一次性把旧项目路径注册迁移为 project_key 跟踪覆盖层。",
    "work-items": "只读事项列表；无过滤参数时查询集合为全部事项；人类可读视图默认隐藏 closed，需看完整记录时使用 --json。",
    "work-item-milestones": "只读指定事项的里程碑列表；事项 ID 必须是 WI-*。",
    "work-item-milestone-add": "为事项新增 planned 或 current 里程碑。新增里程碑会让路线事项的下一门槛由 current 里程碑派生。",
    "work-item-milestone-update": "更新里程碑或完成阶段。状态按 planned → current → completed/cancelled 流转，不允许跳过校验。",
    "work-item-add": "创建事项。初始记录没有里程碑，--next-gate 是轻量事项的一句话最近门槛；需要阶段链路时再新增里程碑。",
    "work-item-update": "更新事项字段。进入 waiting、needs_confirmation、paused 或 closed 时必须提供 --reason；路线事项不能手写根 next_gate。",
    "tasks": "只读待办列表；无过滤参数时查询集合为全部待办；人类可读视图默认展示活跃状态，需看完整记录时使用 --json。",
    "achievements": "只读成果胶囊列表；无过滤参数时返回全部生命周期记录，不会默认隐藏 archived 或 superseded。",
    "achievement-add": "从已完成待办创建成果胶囊（初始 lifecycle=current）；至少要有一个 relation=origin 的完成待办，正文与来源均为必填。",
    "achievement-update": "更新 current 成果胶囊的正文、来源或待办贡献；archived/superseded 胶囊不能继续改写。",
    "achievement-archive": "归档 current 成果胶囊，并保存为什么不再作为默认参考的 --reason。",
    "achievement-supersede": "用另一个 current 成果胶囊替代旧结论；旧胶囊保留为 superseded，并保存替代原因。",
    "task-add": "创建待办。可独立存在，也可通过 --work-item-id 关联事项；关联事项后项目由事项继承，不能再声明 --project-id。",
    "task-update": "更新未完成待办的内容、归属、责任方或状态；下一行动只保留文本，结果日期变化请使用 task-reschedule。",
    "task-start": "记录待办开始推进日期；日期只写入 task_started 事件，不增加 Task 日期字段。",
    "task-reschedule": "只调整待办的 due_at，并把每次硬截止变化追加到计划日期历史。",
    "task-schedule-history": "只读指定待办的硬截止日期历史；旧行动日期只留在不可变审计中，不作为当前字段展示。",
    "task-close": "记录完成摘要和复核来源并关闭待办；关闭会清除下一行动，普通完成不要求创建成果胶囊。",
    "task-reflect": "只为已完成待办补充成果、实际价值或复盘；不能用它重新打开或修改未完成待办。",
    "ideas": "只读闪念列表；无过滤参数时返回全部状态，归档闪念也会保留在查询结果中。",
    "idea-add": "记录一条新的 inbox 闪念。闪念不是待办；确认提升时使用 idea-update --promote-to 指向事项或待办。",
    "idea-update": "更新闪念状态（inbox/incubating/promoted/archived），或提升到事项/待办；archived 必须提供 --reason，提升必须提供至少一个事项或待办 ID。",
    "glossary": "只读实体名词查询；无过滤参数时返回全部人员、组织、项目、系统和概念。",
    "term-add": "创建实体名词，并可关联已有项目、事项或待办；--confirmed-at 省略时使用本地当前日期。",
    "term-update": "更新实体名词或追加别名、关联对象和来源；改名时级联更新所有引用该 entity_id 的待办责任方名称。",
    "show": "只读输出指定 ID 的原始记录 JSON；可查项目、事项、待办、闪念、成果胶囊或实体名词。",
    "history": "只读检索指定周期内已完成待办的完成摘要、来源、价值、复盘和成果胶囊关联；不指定周期时返回全部。",
    "review": "只读生成一个月度、季度或半年度成果复盘；必须显式指定一个周期，不会写日报或改变 Work 事实。",
    "changes": "只读读取不可变变更记录；无时间窗默认返回最近 20 条，提供时间窗时默认返回窗口内全部事件。",
    "refresh": "重建当前派生 Markdown 视图；不改变事实 JSON 或 events.jsonl。",
    "validate": "只读校验当前事实源、项目跟踪关系、不可变变更记录和派生视图的一致性。",
}


COMMAND_SUMMARIES = {
    "init": "初始化新的 Work Runtime（写入）",
    "now": "显示当前工作视图（只读）",
    "brief": "生成聊天窗口简报（只读）",
    "projects": "列出项目引用（无过滤时全量）",
    "project-track": "跟踪已发现项目（写入）",
    "project-update": "更新项目引用（写入）",
    "migrate-project-catalog": "迁移项目跟踪关系（写入）",
    "work-items": "列出事项（无过滤时全量）",
    "work-item-milestones": "列出事项里程碑（只读）",
    "work-item-milestone-add": "新增事项里程碑（写入）",
    "work-item-milestone-update": "更新事项里程碑（写入）",
    "work-item-add": "创建事项（写入）",
    "work-item-update": "更新事项（写入）",
    "tasks": "列出待办（无过滤时全量）",
    "achievements": "列出成果胶囊（无过滤时全量）",
    "achievement-add": "创建成果胶囊（写入）",
    "achievement-update": "更新成果胶囊（写入）",
    "achievement-archive": "归档成果胶囊（写入）",
    "achievement-supersede": "替代成果胶囊（写入）",
    "task-add": "创建待办（写入）",
    "task-update": "更新待办（写入）",
    "task-start": "记录待办开始推进日期（写入）",
    "task-reschedule": "调整待办日期（写入）",
    "task-schedule-history": "查看待办日期历史（只读）",
    "task-close": "关闭待办并记录完成信息（写入）",
    "task-reflect": "补充已完成待办成果或复盘（写入）",
    "ideas": "列出闪念（无过滤时全量）",
    "idea-add": "记录闪念（写入）",
    "idea-update": "更新、提升或归档闪念（写入）",
    "glossary": "查询实体名词（无过滤时全量）",
    "term-add": "创建实体名词（写入）",
    "term-update": "更新实体名词（写入）",
    "show": "显示一条原始记录（只读）",
    "history": "检索已完成待办成果（只读）",
    "review": "生成周期成果复盘（只读）",
    "changes": "读取不可变变更记录（只读）",
    "refresh": "重建派生视图（写入视图）",
    "validate": "校验事实源、事件和视图（只读）",
}


QUERY_COMMANDS = {
    "now", "brief", "projects", "work-items", "work-item-milestones", "tasks",
    "achievements", "task-schedule-history", "ideas", "glossary", "show",
    "history", "review", "changes", "validate",
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
        "--value-type",
        choices=sorted(VALUE_TYPES),
        metavar="TYPE",
        help=(
            "只保留记录过该实际价值类型的待办；TYPE 为 "
            + "/".join(sorted(VALUE_TYPES))
            + "。"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="以 JSON 输出，不改变查询范围。"
    )


def add_realized_value_arguments(parser):
    parser.add_argument(
        "--value",
        nargs=2,
        action="append",
        default=[],
        metavar=("TYPE", "STATEMENT"),
        help="可重复；TYPE 为 business/capability/relationship/reportable/efficiency/risk_reduction/other。",
    )
    parser.add_argument(
        "--no-realized-value",
        action="store_true",
        help="明确记录本次关闭尚未观察到实际价值；不能与 --value 同用。",
    )


def _set_action_help(parser, destination, text, metavar=None):
    """Set help metadata after parser composition without changing parsing."""

    for action in parser._actions:
        if action.dest != destination:
            continue
        action.help = text
        if metavar is not None:
            action.metavar = metavar
        return


def _annotate_work_parsers(commands):
    """Attach user-facing descriptions and semantic option help to Work parsers."""

    common_help = {
        "id": "目标记录 ID；按命令可为 PRJ-*、WI-*、TASK-*、IDEA-*、ACH-* 或 ENT-*。",
        "milestone_id": "目标里程碑 ID（MS-*）。",
        "name": "项目或实体名词名称。",
        "title": "事项、里程碑或成果胶囊标题。",
        "text": "闪念正文。",
        "context": "仅保留执行或理解所需的背景。",
        "reason": "状态变化或归档/替代的原因；进入非活跃状态时必填。",
        "summary": "完成摘要：说明实际得到的结果。",
        "completion_source": "完成依据或复核来源，可重复；完成里程碑/待办时必填。",
        "source_ref": "成果胶囊证据来源三元组 KIND LOCATION LABEL，可重复。",
        "task_link": "待办关联三元组 TASK_ID RELATION CONTRIBUTION，可重复。",
        "json": "以 JSON 输出，不改变查询范围。",
        "query": "可选查询文本；省略时不按文本过滤。",
        "project": "按项目名称或 ID 筛选；省略时不按项目过滤。",
        "project_key": "Project Catalog 中的稳定项目键。",
        "tracking_state": "LifeOS 跟踪状态；paused/archived 需要 --reason。",
        "state": "事项状态；waiting、needs_confirmation、paused、closed 需要 --reason。",
        "status": "记录状态；进入 waiting、paused、cancelled 或归档状态需提供 --reason。",
        "lifecycle": "成果生命周期；current、archived、superseded。",
        "stage": "来源明确时记录的自由文本业务阶段，不是枚举。",
        "next_gate": "事项一句话最近要跨过的门槛；只写一个可观察结果，不写日期、背景或任务清单；路线事项由 current 里程碑派生。",
        "outcome": "可验证的目标结果，而不是动作描述。",
        "completion_criteria": "判定结果/阶段完成的标准。",
        "target": "里程碑目标日期（YYYY-MM-DD）；不等于待办硬截止。",
        "decision": "完成阶段后的决定：continue、adjust、pause 或 close。",
        "activate_next": "继续/调整时要激活的同一事项 planned 里程碑 ID。",
        "responsible_party": "真实责任方名称。",
        "responsible_entity": "责任方实体 ID（ENT-*）；需已存在于 glossary。",
        "responsible_kind": "责任方类型：self/person/organization/unknown。",
        "work_item_id": "关联事项 ID（WI-*）；关联后项目引用由事项继承。",
        "project_id": "直接关联项目引用 ID（PRJ-*）。",
        "milestone_id": "路线事项当前里程碑 ID（MS-*）。",
        "promote_to": "提升目标事项或待办 ID，可重复；至少一个目标才能变为 promoted。",
        "kind": "实体名词类型或来源类型，按命令的 choices 取值。",
        "description": "实体名词的简短定义。",
        "alias": "别名，可重复；更新命令只追加不删除。",
        "related_item": "关联项目、事项或待办 ID，可重复。",
        "confirmed_at": "确认日期（YYYY-MM-DD）。",
        "learning": "成果胶囊的关键经验，可重复。",
        "reuse": "成果可复用的场景。",
        "task": "按关联待办 ID（TASK-*）筛选成果胶囊；省略时不按待办过滤。",
        "why": "为什么该结果值得推进或不能遗漏。",
        "reflection": "完成后的复盘或可复用经验。",
        "by": "替代旧胶囊的新成果胶囊 ID（ACH-*）。",
        "mode": (
            "简报模式：current 当前前瞻；reminder 通用提醒；"
            "closeout 18:00 收口。"
        ),
        "month": "日历月（YYYY-MM）。",
        "quarter": "日历季度（YYYY-QN）。",
        "half": "日历半年度（YYYY-HN）。",
        "value_type": "实际价值类型。",
        "from_value": "窗口起点；YYYY-MM-DD 按本地零点解释，或带时区的 ISO 时间戳。",
        "to_value": "窗口终点（不含）；格式同 --from。",
        "limit": "返回最近 N 条；无时间窗默认 20，有时间窗默认窗口内全部。",
        "due": "结果硬截止日期（YYYY-MM-DD）；不表示下一行动日期。",
        "next_action": "要采取的下一步动作文本。",
        "reason_code": "延后/清除硬截止时的原因：external_change/priority_changed/dependency_blocked/capacity_overload/estimate_error/self_delay/date_correction。",
        "note": "日期变化补充说明；使用 --reason-code 时才能提供。",
        "clear_due": "清除结果硬截止；必须同时提供 --reason-code。",
        "clear_next_action": "清除下一行动文本。",
        "clear_work_item": "解除事项关联；解除后可再设置直接项目。",
        "clear_project": "解除直接项目关联。",
        "clear_milestone": "解除里程碑关联；路线事项的未完成待办仍需关联 current 里程碑。",
        "clear_stage": "清除自由文本业务阶段。",
        "clear_target": "清除里程碑目标日期。",
        "value": "实际价值二元组 TYPE STATEMENT，可重复。",
        "no_realized_value": "明确记录尚未观察到实际价值。",
    }
    positional_metavars = {
        "id": "ID",
        "milestone_id": "MILESTONE_ID",
        "query": "TEXT",
        "reason_code": "CODE",
    }
    for name, command in commands.choices.items():
        command.description = COMMAND_DESCRIPTIONS.get(name, command.description)
        command.formatter_class = argparse.RawDescriptionHelpFormatter
        if name == "init":
            command.epilog = INIT_EPILOG
        elif name == "refresh":
            command.epilog = "只写派生 Markdown 视图，不改变事实 JSON 或 events.jsonl。"
        elif name in QUERY_COMMANDS:
            command.epilog = QUERY_EPILOG
        elif name == "work-item-milestone-update":
            command.epilog = f"{WRITE_EPILOG}\n{MILESTONE_EPILOG}"
        elif name == "task-add" or name == "task-reschedule":
            command.epilog = f"{WRITE_EPILOG}\n{DATE_EPILOG}"
        elif name == "task-update":
            command.epilog = (
                f"{WRITE_EPILOG}\n"
                "结果硬截止不在此命令修改；请用 task-reschedule 记录 due_at 的变化。"
            )
        else:
            command.epilog = WRITE_EPILOG

        for action in command._actions:
            if action.dest in positional_metavars and not action.option_strings:
                action.metavar = positional_metavars[action.dest]
            if action.help is None:
                action.help = common_help.get(
                    action.dest, "该命令的可选字段；省略时保持当前值。"
                )
        for destination, text in common_help.items():
            _set_action_help(command, destination, text)

        # A few option names are shared by different state machines.  Keep
        # their help precise at the command seam instead of pretending all
        # statuses have the same confirmation rule.
        if name == "work-item-milestone-add":
            _set_action_help(
                command,
                "status",
                "里程碑初始状态：planned 或 current，默认 current；同一事项最多一个 current。",
            )
        elif name == "work-item-milestone-update":
            _set_action_help(
                command,
                "status",
                "按 planned/current/completed/cancelled 的合法流转更新状态；completed 还需完成信息。",
            )
        elif name == "task-add":
            _set_action_help(
                command,
                "status",
                "待办初始状态：active/waiting/paused，默认 active；waiting 或 paused 必须提供 --reason。",
            )
            _set_action_help(
                command,
                "reason",
                "waiting 或 paused 待办的状态原因。",
            )
            _set_action_help(
                command,
                "responsible_kind",
                "责任方类型；默认 person。self 自动使用 ENT-SELF，其他类型需同时提供 --responsible-party。",
            )
            _set_action_help(
                command,
                "responsible_party",
                "个人或组织的真实名称；kind=self 时省略，CLI 自动使用 ENT-SELF 的规范名称。",
            )
        elif name == "task-update":
            _set_action_help(
                command,
                "status",
                "待办状态：active/waiting/paused/cancelled；非 active 状态必须提供 --reason。",
            )
            _set_action_help(
                command,
                "reason",
                "waiting、paused 或 cancelled 待办的状态原因。",
            )
        elif name == "task-reflect":
            _set_action_help(
                command,
                "completion_source",
                "补充成果的复核来源，可重复；不提供则保留原完成来源。",
            )
        elif name == "idea-update":
            _set_action_help(
                command,
                "status",
                "闪念状态：inbox/incubating/promoted/archived；archived 必须提供 --reason。",
            )
            _set_action_help(
                command,
                "reason",
                "归档或其他状态变化原因；归档时必填。",
            )

        if name == "projects":
            _set_action_help(
                command,
                "tracking_state",
                "按 active/paused/archived 过滤；省略时返回全部项目引用。",
            )
        elif name == "project-track":
            _set_action_help(
                command,
                "tracking_state",
                "LifeOS 跟踪状态，默认 active；paused/archived 需要 --reason。",
            )
        elif name == "work-items":
            _set_action_help(
                command,
                "state",
                "按事项状态过滤；省略时返回全部事项。",
            )
        elif name == "tasks":
            _set_action_help(
                command,
                "status",
                "按待办状态过滤；省略时返回全部待办。",
            )
        elif name == "ideas":
            _set_action_help(
                command,
                "status",
                "按 inbox/incubating/promoted/archived 过滤；省略时返回全部闪念。",
            )
        elif name == "task-add":
            _set_action_help(
                command,
                "project_id",
                "直接关联项目引用 ID（PRJ-*）；未关联事项时使用，不能与 --work-item-id 同时声明。",
            )
        elif name == "task-update":
            _set_action_help(
                command,
                "project_id",
                "设置直接项目引用 ID（PRJ-*）；已有事项关联时需同时 --clear-work-item。",
            )
        elif name == "work-item-add":
            _set_action_help(
                command,
                "project_id",
                "创建事项的直接项目引用 ID（PRJ-*）；省略时事项不关联项目。",
            )
            _set_action_help(
                command,
                "state",
                "事项初始状态：active/waiting/needs_confirmation/paused，默认 active；非 active 状态必须提供 --reason，needs_confirmation 不会自动确认。",
            )
        elif name == "work-item-update":
            _set_action_help(
                command,
                "project_id",
                "更新事项的直接项目引用 ID（PRJ-*）；省略时保持当前值。",
            )
            _set_action_help(
                command,
                "state",
                "事项状态：active/waiting/needs_confirmation/paused/closed；非 active 状态必须提供 --reason，needs_confirmation 不会自动确认。",
            )
        elif name == "term-add":
            _set_action_help(
                command,
                "confirmed_at",
                "确认日期（YYYY-MM-DD）；省略时使用本地当前日期。",
            )
        elif name == "term-update":
            _set_action_help(
                command,
                "confirmed_at",
                "更新确认日期（YYYY-MM-DD）；省略时保持当前值不变。",
            )

        id_help = {
            "project-update": "项目引用 ID（PRJ-*）。",
            "work-item-milestones": "事项 ID（WI-*）。",
            "work-item-milestone-add": "事项 ID（WI-*）。",
            "work-item-milestone-update": "事项 ID（WI-*）。",
            "work-item-add": None,
            "work-item-update": "事项 ID（WI-*）。",
            "achievement-update": "成果胶囊 ID（ACH-*）。",
            "achievement-archive": "成果胶囊 ID（ACH-*）。",
            "achievement-supersede": "待替代的成果胶囊 ID（ACH-*）。",
            "task-update": "待办 ID（TASK-*）。",
            "task-reschedule": "待办 ID（TASK-*）。",
            "task-schedule-history": "待办 ID（TASK-*）。",
            "task-close": "待办 ID（TASK-*）。",
            "task-reflect": "已完成待办 ID（TASK-*）。",
            "idea-update": "闪念 ID（IDEA-*）。",
            "term-update": "实体名词 ID（ENT-*）。",
            "show": "记录 ID；可为 PRJ/WI/TASK/IDEA/ACH/ENT-*。",
        }
        if id_help.get(name):
            _set_action_help(command, "id", id_help[name], "ID")
        if name == "work-item-milestone-update":
            _set_action_help(command, "milestone_id", "里程碑 ID（MS-*）。", "MILESTONE_ID")
        if name == "changes":
            _set_action_help(command, "from_value", common_help["from_value"], "FROM")
            _set_action_help(command, "to_value", common_help["to_value"], "TO")
            _set_action_help(command, "limit", common_help["limit"], "N")

    for action in commands._choices_actions:
        if action.dest in COMMAND_SUMMARIES:
            action.help = COMMAND_SUMMARIES[action.dest]


def build_parser(version):
    parser = argparse.ArgumentParser(
        prog="lifeos",
        description=ROOT_DESCRIPTION,
        epilog=ROOT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"LifeOS v{version}")
    domains = parser.add_subparsers(dest="domain", required=True)
    from lifeos_modules import register_command_modules

    work = domains.add_parser(
        "work",
        help="管理个人工作事实（查询与写入）",
        description=WORK_DESCRIPTION,
        epilog=WORK_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = work.add_subparsers(dest="command", required=True)

    command = commands.add_parser("init", help="初始化新的 Work Runtime")
    command.add_argument(
        "--self-name",
        required=True,
        type=validate_nonempty_text,
        help="本人规范名称",
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
        choices=["current", "reminder", "closeout"],
        required=True,
    )
    command.set_defaults(handler=command_brief)

    command = commands.add_parser("projects", help="显示项目引用")
    command.add_argument(
        "--tracking-state", choices=sorted(PROJECT_TRACKING_STATES)
    )
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_projects)

    command = commands.add_parser("project-track", help="跟踪已发现项目")
    command.add_argument("--project-key", required=True)
    command.add_argument(
        "--tracking-state",
        choices=sorted(PROJECT_TRACKING_STATES),
        default="active",
    )
    command.add_argument("--reason")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_project_track)

    command = commands.add_parser("project-update", help="更新项目引用")
    command.add_argument("id")
    command.add_argument(
        "--tracking-state", choices=sorted(PROJECT_TRACKING_STATES)
    )
    command.add_argument("--reason")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_project_update)

    command = commands.add_parser(
        "migrate-project-catalog",
        help="迁移旧项目路径注册",
    )
    add_source_argument(command, required=True)
    add_actor_arguments(command, include_idempotency=False)
    command.set_defaults(handler=command_migrate_project_catalog)

    command = commands.add_parser("work-items", help="显示事项")
    command.add_argument("--state", choices=sorted(WORK_ITEM_STATES))
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_work_items)

    command = commands.add_parser("work-item-milestones", help="显示事项里程碑")
    command.add_argument("id")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_work_item_milestones)

    command = commands.add_parser(
        "work-item-milestone-add", help="为事项新增里程碑"
    )
    command.add_argument("id")
    command.add_argument("--title", required=True)
    command.add_argument("--outcome", required=True)
    command.add_argument("--completion-criteria", required=True)
    command.add_argument(
        "--status", choices=["planned", "current"], default="current"
    )
    command.add_argument("--target", type=validate_date)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_work_item_milestone_add)

    command = commands.add_parser(
        "work-item-milestone-update", help="更新事项里程碑"
    )
    command.add_argument("id")
    command.add_argument("milestone_id")
    command.add_argument("--title")
    command.add_argument("--outcome")
    command.add_argument("--completion-criteria")
    command.add_argument("--status", choices=sorted(MILESTONE_STATUSES))
    target_change = command.add_mutually_exclusive_group()
    target_change.add_argument("--target", type=validate_date)
    target_change.add_argument("--clear-target", action="store_true")
    command.add_argument("--summary")
    command.add_argument("--completion-source", action="append", default=[])
    command.add_argument("--decision", choices=sorted(MILESTONE_DECISIONS))
    command.add_argument("--activate-next")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_work_item_milestone_update)

    command = commands.add_parser("work-item-add", help="创建事项")
    command.add_argument("--title", required=True)
    command.add_argument("--project-id")
    command.add_argument(
        "--state",
        choices=sorted(WORK_ITEM_STATES - {"closed"}),
        default="active",
    )
    command.add_argument("--stage", type=validate_nonempty_text)
    command.add_argument("--context")
    command.add_argument("--next-gate", required=True)
    command.add_argument("--reason")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_work_item_add)

    command = commands.add_parser("work-item-update", help="更新事项")
    command.add_argument("id")
    command.add_argument("--title")
    project_change = command.add_mutually_exclusive_group()
    project_change.add_argument("--project-id")
    project_change.add_argument("--clear-project", action="store_true")
    command.add_argument("--state", choices=sorted(WORK_ITEM_STATES))
    stage_change = command.add_mutually_exclusive_group()
    stage_change.add_argument("--stage", type=validate_nonempty_text)
    stage_change.add_argument("--clear-stage", action="store_true")
    command.add_argument("--context")
    command.add_argument("--next-gate")
    command.add_argument("--reason")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_work_item_update)

    command = commands.add_parser("tasks", help="显示待办")
    command.add_argument("--status", choices=sorted(TASK_STATUSES))
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_tasks)

    command = commands.add_parser("achievements", help="查询成果胶囊")
    command.add_argument(
        "--lifecycle", choices=sorted(ACHIEVEMENT_LIFECYCLES)
    )
    command.add_argument("--task")
    command.add_argument("--project")
    command.add_argument("--query")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_achievements)

    command = commands.add_parser(
        "achievement-add", help="从已完成待办创建成果胶囊"
    )
    command.add_argument("--title", required=True)
    command.add_argument(
        "--task-link",
        nargs=3,
        action="append",
        required=True,
        metavar=("TASK_ID", "RELATION", "CONTRIBUTION"),
    )
    command.add_argument("--context", required=True)
    command.add_argument("--outcome", required=True)
    command.add_argument("--learning", action="append", required=True)
    command.add_argument(
        "--source-ref",
        nargs=3,
        action="append",
        required=True,
        metavar=("KIND", "LOCATION", "LABEL"),
    )
    command.add_argument("--reuse", required=True)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_achievement_add)

    command = commands.add_parser(
        "achievement-update", help="用后续待办或证据更新成果胶囊"
    )
    command.add_argument("id")
    command.add_argument("--title")
    command.add_argument(
        "--task-link",
        nargs=3,
        action="append",
        default=[],
        metavar=("TASK_ID", "RELATION", "CONTRIBUTION"),
    )
    command.add_argument("--context")
    command.add_argument("--outcome")
    command.add_argument("--learning", action="append", default=[])
    command.add_argument(
        "--source-ref",
        nargs=3,
        action="append",
        default=[],
        metavar=("KIND", "LOCATION", "LABEL"),
    )
    command.add_argument("--reuse")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_achievement_update)

    command = commands.add_parser(
        "achievement-archive", help="归档不再默认参考的成果胶囊"
    )
    command.add_argument("id")
    command.add_argument("--reason", required=True)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_achievement_archive)

    command = commands.add_parser(
        "achievement-supersede", help="用新成果胶囊替代旧结论"
    )
    command.add_argument("id")
    command.add_argument("--by", required=True)
    command.add_argument("--reason", required=True)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_achievement_supersede)

    command = commands.add_parser("task-add", help="创建待办")
    command.add_argument("--outcome", required=True)
    command.add_argument("--work-item-id")
    command.add_argument("--project-id")
    command.add_argument(
        "--status", choices=["active", "waiting", "paused"], default="active"
    )
    command.add_argument("--responsible-party")
    command.add_argument("--responsible-entity")
    command.add_argument(
        "--responsible-kind",
        choices=["self", "person", "organization", "unknown"],
        default="person",
    )
    command.add_argument("--next-action")
    command.add_argument("--why")
    command.add_argument("--completion-criteria")
    command.add_argument("--context")
    command.add_argument("--reason")
    command.add_argument("--milestone-id")
    command.add_argument("--due", type=validate_date)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_add)

    command = commands.add_parser("task-start", help="记录待办开始推进日期")
    command.add_argument("id")
    command.add_argument("--started-at", type=validate_date, required=True)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_start)

    command = commands.add_parser("task-update", help="更新待办")
    command.add_argument("id")
    command.add_argument("--outcome")
    work_item_change = command.add_mutually_exclusive_group()
    work_item_change.add_argument("--work-item-id")
    work_item_change.add_argument("--clear-work-item", action="store_true")
    project_change = command.add_mutually_exclusive_group()
    project_change.add_argument("--project-id")
    project_change.add_argument("--clear-project", action="store_true")
    command.add_argument(
        "--status", choices=["active", "waiting", "paused", "cancelled"]
    )
    command.add_argument("--responsible-party")
    command.add_argument("--responsible-entity")
    command.add_argument(
        "--responsible-kind",
        choices=["self", "person", "organization", "unknown"],
    )
    command.add_argument("--next-action")
    command.add_argument("--clear-next-action", action="store_true")
    command.add_argument("--why")
    command.add_argument("--completion-criteria")
    command.add_argument("--context")
    command.add_argument("--reason")
    milestone_change = command.add_mutually_exclusive_group()
    milestone_change.add_argument("--milestone-id")
    milestone_change.add_argument("--clear-milestone", action="store_true")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_update)

    command = commands.add_parser(
        "task-reschedule", help="调整待办日期并记录结构化变更原因"
    )
    command.add_argument("id")
    due_change = command.add_mutually_exclusive_group()
    due_change.add_argument("--due", type=validate_date)
    due_change.add_argument("--clear-due", action="store_true")
    command.add_argument(
        "--reason-code", choices=sorted(SCHEDULE_REASON_CODES)
    )
    command.add_argument("--note")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_reschedule)

    command = commands.add_parser(
        "task-schedule-history", help="查询待办的结构化计划日期历史"
    )
    command.add_argument("id")
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_task_schedule_history)

    command = commands.add_parser("task-close", help="记录完成摘要并关闭待办")
    command.add_argument("id")
    command.add_argument("--summary", required=True)
    command.add_argument("--completion-source", action="append", required=True)
    add_realized_value_arguments(command)
    command.add_argument("--reflection")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_close)

    command = commands.add_parser(
        "task-reflect", help="为已完成待办补充成果或复盘"
    )
    command.add_argument("id")
    command.add_argument("--summary")
    command.add_argument("--completion-source", action="append", default=[])
    add_realized_value_arguments(command)
    command.add_argument("--reflection")
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_task_reflect)

    command = commands.add_parser("ideas", help="显示闪念")
    command.add_argument("--status", choices=sorted(IDEA_STATUSES))
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_ideas)

    command = commands.add_parser("idea-add", help="记录闪念")
    command.add_argument("--text", required=True)
    command.add_argument("--context")
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_idea_add)

    command = commands.add_parser("idea-update", help="整理、提升或归档闪念")
    command.add_argument("id")
    command.add_argument("--text")
    command.add_argument("--status", choices=sorted(IDEA_STATUSES))
    command.add_argument("--context")
    command.add_argument("--reason")
    command.add_argument("--promote-to", action="append", default=[])
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_idea_update)

    command = commands.add_parser(
        "glossary", help="查询人员、组织、项目、系统与专有概念"
    )
    command.add_argument("query", nargs="?")
    command.add_argument("--kind", choices=sorted(ENTITY_KINDS))
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_glossary)

    command = commands.add_parser("term-add", help="创建实体名词")
    command.add_argument("--name", required=True)
    command.add_argument("--kind", choices=sorted(ENTITY_KINDS), required=True)
    command.add_argument("--description", required=True)
    command.add_argument("--alias", action="append", default=[])
    command.add_argument("--related-item", action="append", default=[])
    command.add_argument("--confirmed-at", type=validate_date)
    add_source_argument(command, required=True)
    add_actor_arguments(command)
    command.set_defaults(handler=command_term_add)

    command = commands.add_parser("term-update", help="更新实体名词")
    command.add_argument("id")
    command.add_argument("--name")
    command.add_argument("--kind", choices=sorted(ENTITY_KINDS))
    command.add_argument("--description")
    command.add_argument("--alias", action="append", default=[])
    command.add_argument("--related-item", action="append", default=[])
    command.add_argument("--confirmed-at", type=validate_date)
    add_source_argument(command)
    add_actor_arguments(command)
    command.set_defaults(handler=command_term_update)

    command = commands.add_parser(
        "show", help="显示项目引用、事项、待办、闪念、成果胶囊或名词原始记录"
    )
    command.add_argument("id")
    command.set_defaults(handler=command_show)

    command = commands.add_parser("history", help="检索已完成待办成果")
    add_period_arguments(command)
    command.set_defaults(handler=command_history)

    command = commands.add_parser(
        "review", help="生成月度、季度或半年度成果复盘"
    )
    add_period_arguments(command, required=True)
    command.set_defaults(handler=command_review)

    command = commands.add_parser("changes", help="读取内部变更记录")
    command.add_argument(
        "--from",
        dest="from_value",
        type=validate_moment,
        help="窗口起点，YYYY-MM-DD 表示当地零点，或带时区偏移的 ISO 时间戳",
    )
    command.add_argument("--to", dest="to_value", type=validate_moment, help="窗口终点，不含")
    command.add_argument(
        "--limit",
        type=int,
        help="返回最近 N 条；不给时间窗时默认 20，给了时间窗时默认返回窗口内全部事件",
    )
    command.add_argument("--json", action="store_true")
    command.set_defaults(handler=command_changes)

    command = commands.add_parser("refresh", help="重建当前派生视图")
    command.set_defaults(handler=command_refresh)

    command = commands.add_parser(
        "validate", help="验证当前事实源、内部审计与派生视图"
    )
    command.set_defaults(handler=command_validate)

    _annotate_work_parsers(commands)
    register_command_modules(domains, DATA_DIR)
    return parser


def main(version):
    parser = build_parser(version)
    args = parser.parse_args()
    args.handler(args)
