"""Argument parsing and presentation for the LifeOS sessions domain."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .core import SessionError, SessionsService, TimeWindow, canonical_json
from .pack import (
    DEFAULT_INDEX_MAX_BYTES,
    DEFAULT_MAX_BYTES,
    MAX_ACTIVITY_BLOCKS,
    MAX_BLOCK_TEXT,
    build_activity_index,
    build_analysis_pack,
)
from lifeos_projects.manifest import ProjectManifestError
from lifeos_projects.registry import project_map_payload

from .projects import ProjectMap
from .retention import CONFIG_NAME as RETENTION_CONFIG, RetentionPolicy

from .store import SessionNotFound, SessionsStore, StoreError
from .adapters import (
    SESSION_SOURCE_NAMES,
    build_adapters,
    resolve_selected_sources,
)
from lifeos_config.core import ConfigError, load_config


SOURCES = SESSION_SOURCE_NAMES


def _fail(message: str, exit_code: int = 2) -> None:
    print(f"lifeos: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _bound_ms(value: str) -> int:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail("时间必须使用带时区的 ISO 8601 格式")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail("时间必须显式包含时区")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _sources(values: Iterable[str]) -> List[str]:
    try:
        enabled = load_config().session_sources
        selected = list(resolve_selected_sources(values, enabled))
        if not selected:
            raise ValueError("没有启用的 Sessions source")
        return selected
    except (ConfigError, ValueError) as exc:
        _fail(str(exc))
    return []


def _includes(values: Iterable[str], selected: Iterable[str]) -> List[str]:
    allowed = set(selected)
    result: List[str] = []
    for value in values:
        source, separator, conversation_id = value.partition(":")
        if not separator or not source or not conversation_id:
            _fail("--include 必须使用 source:conversation-id 格式")
        if source not in SOURCES:
            _fail(f"--include source 必须是 {', '.join(SOURCES)} 之一")
        if source not in allowed:
            _fail("--include source 必须同时出现在 --source 中")
        if value not in result:
            result.append(value)
    return result


def _service(root: Path, selected: Iterable[str]) -> SessionsService:
    return SessionsService(build_adapters(selected), SessionsStore(root))


def command_scan(args: Any) -> None:
    try:
        window = TimeWindow.from_values(args.from_value, args.to_value)
    except (TypeError, ValueError) as exc:
        _fail(str(exc))
    selected = _sources(args.source)
    includes = _includes(args.include, selected)
    try:
        result = _service(args.sessions_root, selected).scan(
            window=window,
            sources=selected,
            includes=tuple(includes),
        )
    except (OSError, SessionError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        _json(result)
    else:
        print(f"{result.get('scan_id', 'unknown')} 会话扫描 {result.get('status', 'unknown')}")
        source_results = result.get("sources") or result.get("source_results") or []
        if isinstance(source_results, dict):
            source_results = source_results.values()
        for item in source_results:
            stats = item.get("stats") or {}
            print(
                f"- {item.get('source')}: {item.get('status')}；"
                f"matched {stats.get('matched', 0)}，created {stats.get('created', 0)}，"
                f"reused {stats.get('reused', 0)}，revised {stats.get('revised', 0)}"
            )
    if result.get("status") != "complete":
        raise SystemExit(1)


def command_scans(args: Any) -> None:
    try:
        window = TimeWindow.from_values(args.from_value, args.to_value)
    except (TypeError, ValueError) as exc:
        _fail(str(exc))
    if args.limit < 1:
        _fail("--limit 必须是正整数")
    try:
        scans = SessionsStore(args.sessions_root).list_scans(
            window=window,
            status=args.status,
            limit=args.limit,
        )
    except (OSError, SessionError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        _json(scans)
        return
    for item in scans:
        source_statuses = ", ".join(
            f"{source.get('source')}: {source.get('status')}"
            for source in item.get("sources") or []
        ) or "-"
        print(
            f"{item.get('scan_id')} · {item.get('status')} · "
            f"{item.get('created_at')} · {source_statuses}"
        )


def command_rebuild(args: Any) -> None:
    try:
        window = TimeWindow.from_values(args.from_value, args.to_value)
    except (TypeError, ValueError) as exc:
        _fail(str(exc))
    selected = _sources(args.source)
    includes = _includes(args.include, selected)
    configured = set(_sources(["all"]))
    if args.apply and (set(selected) != configured or includes):
        _fail("rebuild --apply 必须同时重扫全部已启用来源，且不能使用 --include")
    service = _service(args.sessions_root, selected)
    try:
        result = service.rebuild(
            window,
            sources=selected,
            includes=tuple(includes),
            apply=bool(args.apply),
        )
    except (OSError, SessionError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        _json(result)
    else:
        rebuild = result.get("rebuild") or {}
        mode = "已原子切换" if rebuild.get("applied") else "预演/未切换"
        print(f"{result.get('scan_id', 'unknown')} 全量重建 {mode} · 扫描 {result.get('status', 'unknown')}")
        if rebuild.get("backup"):
            print(f"旧 Sessions 备份：{rebuild['backup']}")
        for error in rebuild.get("validation_errors") or []:
            print(f"! {error}", file=sys.stderr)
    rebuild = result.get("rebuild") or {}
    if (
        result.get("status") == "failed"
        or rebuild.get("validation_errors")
        or rebuild.get("failed_sources")
        or (rebuild.get("apply_requested") and not rebuild.get("applied"))
    ):
        raise SystemExit(1)


def command_list(args: Any) -> None:
    filters: Dict[str, Any] = {}
    for argument, key in (
        (args.source, "source"),
        (args.conversation, "conversation"),
        (args.workspace, "workspace"),
        (args.query, "query"),
        (args.completeness, "content_completeness"),
        (args.adapter_version, "adapter_version"),
        (args.scan, "scan"),
    ):
        if argument is not None:
            filters[key] = argument
    if args.from_value:
        filters["from_ms"] = _bound_ms(args.from_value)
    if args.to_value:
        filters["to_ms"] = _bound_ms(args.to_value)
    if filters.get("from_ms") is not None and filters.get("to_ms") is not None:
        if filters["from_ms"] >= filters["to_ms"]:
            _fail("--from 必须早于 --to")
    store = SessionsStore(args.sessions_root)
    try:
        items = store.list_slices(filters)
    except (FileNotFoundError, SessionError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        _json(items)
        return
    for item in items:
        conversation = item.get("conversation") or {}
        title = conversation.get("title") or item.get("title") or "-"
        print(
            f"{item.get('slice_id')}  {item.get('source')}  "
            f"{item.get('started_at')}  {conversation.get('id') or item.get('conversation_id')}  "
            f"{item.get('content_completeness')}  {title}"
        )


def command_show(args: Any) -> None:
    try:
        item = SessionsStore(args.sessions_root).show(args.slice_id, args.revision)
    except (FileNotFoundError, SessionNotFound, SessionError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        _json(item)
        return
    conversation = item.get("conversation") or {}
    evidence = item.get("execution_evidence") or {}
    print(f"{item['slice_id']} · {item['source']} · {conversation.get('title') or conversation.get('id')}")
    trimmed = "，引用已裁剪" if item.get("provenance_trimmed") else ""
    print(f"{item['started_at']} → {item['ended_at']} · 内容 {item.get('content_completeness')}{trimmed}")
    for label, key in (("改动文件", "changed_targets"), ("其他目标", "other_targets"),
                       ("验证", "verifications"), ("失败", "failures"),
                       ("用户中断", "user_interrupts")):
        if evidence.get(key):
            print(f"{label}：{'、'.join(evidence[key])}")
    for block in item.get("blocks") or []:
        role = block.get("author_role") or "unknown"
        context = "（上下文）" if block.get("context") else ""
        marker = "（应用注入，非本人）" if block.get("origin") == "system_injected" else ""
        print(f"\n[{block.get('kind')}] {role}{context}{marker}\n{block.get('text', '')}")


def command_validate(args: Any) -> None:
    try:
        errors = SessionsStore(args.sessions_root).validate(args.scan)
    except (OSError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if errors:
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        raise SystemExit(1)
    print("LifeOS sessions 派生存储校验通过")


def _mb(value: Any) -> str:
    return f"{int(value) / 1048576:.1f} MB"


def command_usage(args: Any) -> None:
    store = SessionsStore(args.sessions_root)
    try:
        report = store.usage()
        policy = RetentionPolicy.load(args.sessions_root)
    except (OSError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    report["retention_policy"] = policy.to_dict()
    if args.json:
        _json(report)
        return
    print(f"{report['root']}\n合计 {_mb(report['total_bytes'])}")
    for name, value in sorted(report["components"].items(), key=lambda item: -item[1]):
        print(f"  {name:<16} {_mb(value)}")
    breakdown = report.get("index_breakdown") or {}
    if breakdown:
        print("  索引内部：" + "，".join(f"{k} {_mb(v)}" for k, v in breakdown.items()))
    slices = report.get("slices") or {}
    if slices.get("current"):
        print(
            f"\n切片 {slices['current']}（修订 {slices['revisions']}）"
            f" · {slices['earliest']} → {slices['latest']}"
        )
    for item in report.get("by_source") or []:
        print(f"  {item['source']:<10} {item['slices']}")
    projection = report.get("projection")
    if projection:
        print(
            f"\n观测增速：{_mb(projection['bytes_per_day'])}/天"
            f"（跨度 {projection['observed_span_days']} 天）"
            f" → 不设上界约 {_mb(projection['bytes_per_year_unbounded'])}/年"
        )
    if policy.bounded:
        print("保留期：" + "，".join(
            f"{k} {v}" for k, v in policy.to_dict().items()
            if k != "schema_version" and v is not None))
    else:
        print(f"保留期：未设置上界（{args.sessions_root / RETENTION_CONFIG}）")
    if report.get("pruned_days"):
        print(f"已裁剪的来源-天：{report['pruned_days']}")


def command_compact(args: Any) -> None:
    store = SessionsStore(args.sessions_root)
    try:
        result = store.compact()
    except (OSError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        _json(result)
        return
    print(
        f"索引 {_mb(result['index_bytes_before'])} → {_mb(result['index_bytes_after'])}"
        f"，回收 {_mb(result['reclaimed_bytes'])}；未删除任何事实"
    )


def command_prune(args: Any) -> None:
    store = SessionsStore(args.sessions_root)
    try:
        policy = RetentionPolicy.load(args.sessions_root)
    except ValueError as exc:
        _fail(str(exc), 1)
    if not policy.bounded:
        _fail(
            f"未声明保留期上界；先写入 {args.sessions_root / RETENTION_CONFIG}"
            f"（字段：fts_days、keep_slices_days）"
        )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    try:
        result = store.prune(
            slices_before_ms=policy.cutoff_ms("keep_slices_days", now_ms),
            fts_before_ms=policy.cutoff_ms("fts_days", now_ms),
            dry_run=not args.apply,
        )
    except (OSError, StoreError, ValueError) as exc:
        _fail(str(exc), 1)
    result["policy"] = policy.to_dict()
    if args.json:
        _json(result)
        return
    mode = "已执行" if args.apply else "预演（加 --apply 才会真正删除）"
    print(f"{mode}")
    print(f"  取消全文索引的切片：{result['fts_rows']}")
    print(f"  删除正文的切片：{result['slices']}，释放 {_mb(result['bytes_freed'])}")
    if result["pruned_days"]:
        span = sorted({item["day"] for item in result["pruned_days"]})
        print(f"  覆盖 {len(span)} 天：{span[0]} → {span[-1]}")
    if not args.apply and (result["fts_rows"] or result["slices"]):
        print("\n删除后原始会话若已被来源应用轮转，则无法再恢复。")


def _project_map(data_dir: Path) -> ProjectMap:
    try:
        return ProjectMap.from_dict(project_map_payload(data_dir))
    except (ConfigError, ProjectManifestError, ValueError) as exc:
        _fail(str(exc), 1)


def command_index(args: Any) -> None:
    try:
        window = TimeWindow.from_values(args.from_value, args.to_value)
        project_map = _project_map(args.data_dir)
        result = build_activity_index(
            SessionsStore(args.sessions_root),
            window,
            sources=tuple(args.source or ()),
            conversation=args.conversation,
            workspace=args.workspace,
            project=args.project,
            query=args.query,
            max_bytes=args.max_bytes,
            gap_ms=args.gap_minutes * 60 * 1000,
            project_map=project_map,
        )
        result["project_catalog"] = project_map.to_dict()["catalog"]
    except (FileNotFoundError, SessionError, StoreError, TypeError, ValueError) as exc:
        _fail(str(exc), 1)
    if args.json:
        sys.stdout.write(canonical_json(result))
        return
    dropped = sum(item["activities"] for item in result["dropped_by_project"])
    print(
        f"{result['index_id']} · {len(result['activities'])}/{result['activity_total']} activities · "
        f"{result['byte_size']} bytes"
        + (f" · 预算丢弃 {dropped}" if dropped else "")
    )
    print("\n项目          活动  切片  改动  失败  天数  归属")
    for item in result["project_summary"]:
        print(
            f"{item['activities']:6d}  {item['slices']:6d}  {item['changed_targets']:4d}  "
            f"{item['failures']:4d}  {item['active_days']:4d}  [{item['project_kind']}] {item['project_key']}"
        )
    print("\n按天：" + "，".join(
        f"{item['day']} {item['activities']}活动/{item['projects']}项目" for item in result["day_summary"]))
    if result["excerpts_included"]:
        print("\n活动（时间 · 项目 · signal · 切片 · 开场 → 结果）")
        for item in result["activities"]:
            head = item.get("opening") or item.get("title") or ""
            tail = item.get("outcome") or ""
            if head and tail:
                body = f"{head}  →  {tail}"
            elif tail:
                # Only an outcome is the delegated case: the request lives in
                # the parent conversation, so an arrow would point at nothing.
                body = f"→  {tail}"
            else:
                body = head or "（无可引正文）"
            print(
                f"  {item['started_at'][11:16]}–{item['ended_at'][11:16]} "
                f"{item['project_key']:<14} s{item['signal_score']:<3} "
                f"{item['slice_count']:>3}片  {body}"
            )
            print(f"        {item['activity_id']}")
    suppressed = result.get("suppressed_activities") or {}
    labels = (
        ("mechanism_only", "只含审批裁决、无执行证据"),
        ("no_quotable_text", "无可引正文、无执行证据"),
    )
    for key, label in labels:
        if suppressed.get(key):
            print(f"\n另有 {suppressed[key]} 个活动{label}，已不列出")
    for item in result["dropped_by_project"]:
        print(f"! 预算丢弃 {item['activities']} 个活动：{item['project_key']}", file=sys.stderr)


def command_projects(args: Any) -> None:
    project_map = _project_map(args.data_dir)
    if args.json:
        _json({"authority": "project-catalog/lifeos-project.json", **project_map.to_dict()})
        return
    print("项目身份：由配置发现根中的 lifeos-project.json 动态派生")
    print(f"一次性会话目录：{'、'.join(project_map.ad_hoc_roots) or '（无）'}")
    print(f"Worktree 根：{'、'.join(project_map.worktree_roots) or '（无）'}")
    if not project_map.projects:
        print("\n当前发现根中没有有效项目清单。")
        return
    print("\n已发现项目：")
    for entry in sorted(project_map.projects, key=lambda item: item["key"]):
        print(f"  {entry['key']}{' · ' + entry['title'] if entry.get('title') else ''}")
        for root_value in entry["roots"]:
            print(f"    {root_value}")


def command_pack(args: Any) -> None:
    try:
        window = TimeWindow.from_values(args.from_value, args.to_value)
        result = build_analysis_pack(
            SessionsStore(args.sessions_root),
            window,
            sources=tuple(args.source or ()),
            conversation=args.conversation,
            workspace=args.workspace,
            project=args.project,
            query=args.query,
            activities_wanted=tuple(args.activity or ()),
            blocks=args.blocks,
            block_chars=args.block_chars,
            max_bytes=args.max_bytes,
            gap_ms=args.gap_minutes * 60 * 1000,
            project_map=_project_map(args.data_dir),
        )
    except (FileNotFoundError, SessionError, StoreError, TypeError, ValueError) as exc:
        _fail(str(exc), 1)
    for missing in result.get("missing_activities") or []:
        print(
            f"lifeos: 窗口内没有活动 {missing}；"
            f"活动 id 由 --from/--to/--gap-minutes 决定，请用取得该 id 的同一组参数",
            file=sys.stderr,
        )
    if args.json:
        # AnalysisPack's byte budget applies to its public JSON representation,
        # so emit the same canonical form used to calculate ``byte_size``.
        sys.stdout.write(canonical_json(result))
        return
    omitted = sum(item["activities"] for item in result["omitted_by_project"])
    print(
        f"{result['pack_id']} · {len(result['activities'])} activities · "
        f"{result['byte_size']} bytes"
        + (f" · 预算外 {omitted} 个活动未取正文" if omitted else "")
    )
    for item in result["activities"]:
        conversation = item.get("conversation") or {}
        print(
            f"\n[{item['source']}] {item['project_key']} · "
            f"{item['started_at']} → {item['ended_at']} · "
            f"{conversation.get('title') or conversation.get('id')}"
        )
        for block in item.get("content") or []:
            print(f"- {block.get('kind')}/{block.get('author_role')}: {block.get('text', '')}")
    for item in result["omitted_by_project"]:
        print(f"! 未取正文 {item['activities']} 个活动：{item['project_key']}", file=sys.stderr)
    for item in result.get("omitted_activities") or []:
        print(
            f"!   {item['started_at']} → {item['ended_at']} · "
            f"{item['project_key']} · {item['slice_ref_count']} 个切片 · "
            f"signal {item['signal_score']} · --conversation {item['conversation_id']}",
            file=sys.stderr,
        )


def register_sessions_parser(domains: Any, data_dir: Path) -> None:
    sessions = domains.add_parser(
        "sessions",
        help="读取 Agent 应用会话来源并维护私有派生索引",
        description=(
            "Sessions 只读 Codex、Claude Code 与 SmartWork Agent 应用会话来源，"
            "不写入来源日志、Work 或日报。scan 会更新私有派生存储；"
            "rebuild 先在临时 staging 中重建，只有 --apply 才替换 active Sessions。"
        ),
        epilog=(
            "窗口使用带时区的 ISO 8601 半开区间 [from, to)：来源原生单位是 Turn，"
            "每个 Turn 形成一个 ConversationSlice；list/show 操作 Slice，"
            "index/pack 将同一来源、同一会话的 Slice 按时间间隔聚成 Activity。"
            "index 返回窗口内可展示 Activity 骨架，pack 返回选中 Activity 的有界正文。"
            "compact 会改写索引但不删事实；prune 只有 --apply 才会删除派生索引或正文。"
        ),
    )
    sessions.set_defaults(sessions_root=data_dir / "sessions", data_dir=data_dir)
    commands = sessions.add_subparsers(dest="command", required=True)

    command = commands.add_parser(
        "scan",
        help="读取来源并写入窗口内的 Slice（Turn 粒度）",
        description=(
            "写入私有 Sessions 派生存储，不改来源日志。必须提供带时区 ISO 8601 的"
            "半开窗口 [from, to)；--source 可重复，all 等价于三个 Agent 来源。"
            "--include 仅限制本次窄扫描，不建立持久 scope。"
        ),
        epilog=(
            "结果是 scan manifest 和每个来源的状态/计数；explicit_abort_without_work"
            " 不生成 Slice，只计入 omission，带结果的中断保留为 interrupted_with_result。"
            "来源 partial/failed 时仍保留其他有效结果并以非零状态返回。"
        ),
    )
    command.add_argument(
        "--source", action="append", choices=[*SOURCES, "all"], required=True,
        help="可重复；all 展开为 codex、claude、smartwork、deepseek",
    )
    command.add_argument(
        "--from", dest="from_value", required=True, metavar="ISO-8601",
        help="窗口下界（含），必须带时区",
    )
    command.add_argument(
        "--to", dest="to_value", required=True, metavar="ISO-8601",
        help="窗口上界（不含），必须带时区",
    )
    command.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="SOURCE:CONVERSATION-ID",
        help="仅本次扫描限制指定 source 的原生会话；不保存 scope",
    )
    command.add_argument(
        "--json", action="store_true",
        help="输出 manifest JSON（状态、窗口、每来源计数与清洗计数）",
    )
    command.set_defaults(handler=command_scan)

    command = commands.add_parser(
        "scans",
        help="只读查询已有 scan manifest",
        description=(
            "只读当前私有 Sessions 派生存储，按精确的带时区 ISO 8601 半开窗口查询"
            "已有 scan manifest；不回源、不创建扫描。"
        ),
        epilog=(
            "结果包含 scan 总状态、manifest 完整性和各来源状态；空结果只表示没有匹配的扫描记录，"
            "不表示该窗口没有会话。"
        ),
    )
    command.add_argument(
        "--from", dest="from_value", required=True, metavar="ISO-8601",
        help="窗口下界（含），必须带时区",
    )
    command.add_argument(
        "--to", dest="to_value", required=True, metavar="ISO-8601",
        help="窗口上界（不含），必须带时区",
    )
    command.add_argument(
        "--status", choices=("complete", "partial", "failed"),
        help="只返回指定数据库状态的扫描",
    )
    command.add_argument(
        "--limit", type=int, default=20,
        help="最多返回的扫描记录数（默认 20）",
    )
    command.add_argument("--json", action="store_true", help="输出 scan manifest 摘要 JSON")
    command.set_defaults(handler=command_scans)

    command = commands.add_parser(
        "rebuild",
        help="全量重扫到 staging；默认预演，--apply 才替换 active Sessions",
        description=(
            "从来源文件构建临时 staging Store 并 validate；不加 --apply 时 active Sessions"
            " 保持不变。apply 必须同时覆盖 codex、claude、smartwork、deepseek（或 --source all），"
            "且不能使用 --include；校验或任一来源失败都不切换。"
        ),
        epilog=(
            "--apply 成功后目录级原子替换 active Sessions，并将旧树整体备份；"
            "来源日志始终只读。窗口为带时区 ISO 8601 半开区间 [from, to)。"
        ),
    )
    command.add_argument(
        "--source", action="append", choices=[*SOURCES, "all"], required=True,
        help="可重复；all 展开为 codex、claude、smartwork、deepseek；--apply 要求三者全量",
    )
    command.add_argument(
        "--from", dest="from_value", required=True, metavar="ISO-8601",
        help="窗口下界（含），必须带时区",
    )
    command.add_argument(
        "--to", dest="to_value", required=True, metavar="ISO-8601",
        help="窗口上界（不含），必须带时区",
    )
    command.add_argument(
        "--include", action="append", default=[], metavar="SOURCE:CONVERSATION-ID",
        help="仅本次重建限制指定 source；与 --apply 互斥",
    )
    command.add_argument(
        "--apply", action="store_true",
        help="危险写入：三来源全量且无 --include 时，校验通过后原子切换并备份旧树",
    )
    command.add_argument(
        "--json", action="store_true",
        help="输出重建/校验/切换结果 JSON",
    )
    command.set_defaults(handler=command_rebuild)

    command = commands.add_parser(
        "list",
        help="只读查询当前 Slice（一个 Agent Turn）",
        description=(
            "只读私有派生存储，不回源。返回当前 revision 的 ConversationSlice 摘要；"
            "可用带时区 ISO 8601 的半开窗口 [from, to) 与多个组合过滤器。"
            "--query 使用全文索引，--scan 限定某次 scan 的关联结果。"
        ),
        epilog="默认输出一行一条 Slice；--json 返回索引元数据，不包含来源回读。",
    )
    command.add_argument(
        "--from", dest="from_value", metavar="ISO-8601",
        help="窗口下界（含），必须带时区",
    )
    command.add_argument(
        "--to", dest="to_value", metavar="ISO-8601",
        help="窗口上界（不含），必须带时区",
    )
    command.add_argument("--source", choices=SOURCES, help="限定来源")
    command.add_argument("--conversation", metavar="CONVERSATION-ID", help="限定会话")
    command.add_argument("--workspace", metavar="PATH", help="限定 workspace 路径")
    command.add_argument("--query", metavar="TEXT", help="全文索引关键词")
    command.add_argument(
        "--completeness", choices=("complete", "partial", "truncated"),
        help="限定 content_completeness",
    )
    command.add_argument("--adapter-version", metavar="VERSION", help="限定采集 Adapter 版本")
    command.add_argument("--scan", metavar="SCAN-ID", help="限定某次 scan manifest")
    command.add_argument("--json", action="store_true", help="输出 Slice 元数据 JSON")
    command.set_defaults(handler=command_list)

    command = commands.add_parser(
        "show",
        help="只读查看一个 Slice（单个 Agent Turn）的当前或历史 revision",
        description=(
            "从私有派生存储读取指定 ConversationSlice，不回源。默认显示当前 revision；"
            "--revision 可回读不可变历史版本；默认文本展示正文块与执行证据，--json 返回"
            "包含生命周期字段的完整 Slice。"
        ),
    )
    command.add_argument("slice_id", metavar="SLICE-ID", help="Slice 稳定 ID")
    command.add_argument("--revision", metavar="REVISION", help="指定不可变 revision；省略时读取当前版本")
    command.add_argument("--json", action="store_true", help="输出完整 Slice JSON")
    command.set_defaults(handler=command_show)

    command = commands.add_parser(
        "validate",
        help="只读校验 active Sessions 派生存储、索引与权限",
        description=(
            "不创建、不修复、不回源；校验目录权限、SQLite 表与外键、Slice/omission"
            " revision、FTS 和 scan 关联。--scan 只聚焦指定 scan manifest。"
        ),
    )
    command.add_argument("--scan", metavar="SCAN-ID", help="只校验指定 scan manifest 的关联")
    command.set_defaults(handler=command_validate)

    command = commands.add_parser(
        "usage",
        help="只读报告派生存储占用、计数、增速与保留期",
        description=(
            "读取当前私有 Sessions store；输出各组成部分真实字节数、当前/历史 revision"
            " 计数、来源分布、观测增速外推与 retention.json。年度数字是外推，不是承诺。"
        ),
        epilog="只读诊断，不回源、不改写索引、不删除事实。",
    )
    command.add_argument("--json", action="store_true", help="输出占用报告 JSON")
    command.set_defaults(handler=command_usage)

    command = commands.add_parser(
        "compact",
        help="维护 FTS/SQLite 索引并回收空间；不删除任何事实",
        description=(
            "对私有派生数据库执行 FTS optimize 与 VACUUM。它会改写索引文件，"
            "但不删除或重写 Slice、omission、scan 等事实，也不读取来源日志。"
        ),
    )
    command.add_argument("--json", action="store_true", help="输出压缩前后占用 JSON")
    command.set_defaults(handler=command_compact)

    command = commands.add_parser(
        "prune",
        help="按 retention.json 预演或删除超期派生内容",
        description=(
            "默认 dry-run，只读取并报告将移除的 FTS 覆盖和 Slice 正文。"
            "必须先在 retention.json 声明 fts_days / keep_slices_days 上界；"
            "--apply 才会实际删除，来源日志和日报不受影响。"
        ),
        epilog=(
            "删除顺序先去掉全文索引、再删除 revision 正文；后一项不可逆，"
            "若来源应用已轮转，删除的会话证据无法恢复。"
        ),
    )
    command.add_argument(
        "--apply", action="store_true",
        help="危险写入：按已声明保留期实际删除 FTS 与超期 Slice 正文",
    )
    command.add_argument("--json", action="store_true", help="输出删除计划/结果 JSON")
    command.set_defaults(handler=command_prune)

    command = commands.add_parser(
        "index",
        help="只读生成窗口内可展示 Activity 骨架（不取正文）",
        description=(
            "可展示 Activity 是同一来源、同一会话的 Turn/Slice 按 --gap-minutes 聚合的证据导航单元；"
            "它不等同于任务或项目，每个 Activity 一行，覆盖带时区 ISO 8601 半开窗口 [from, to)。"
            "mechanism_only 或 no_quotable_text 的 Activity 可能不逐行输出，"
            "数量记入 suppressed_activities；另返回项目/日期汇总、coverage、"
            "cleaning_summary 与生命周期计数，不回源。"
        ),
        epilog=(
            "--max-bytes 是整个 JSON 视图预算：先去掉可选标题/摘录，再按项目记录被丢弃的"
            " Activity，避免把预算外活动误当成窗口没有发生。"
        ),
    )
    command.add_argument(
        "--from", dest="from_value", required=True, metavar="ISO-8601",
        help="窗口下界（含），必须带时区",
    )
    command.add_argument(
        "--to", dest="to_value", required=True, metavar="ISO-8601",
        help="窗口上界（不含），必须带时区",
    )
    command.add_argument("--source", action="append", choices=SOURCES, help="可重复限定来源")
    command.add_argument("--conversation", metavar="CONVERSATION-ID", help="限定会话")
    command.add_argument("--workspace", metavar="PATH", help="限定 workspace 路径")
    command.add_argument("--project", metavar="PROJECT", help="限定归一后的 project_key")
    command.add_argument("--query", metavar="TEXT", help="限定全文索引关键词")
    command.add_argument(
        "--max-bytes", type=int, default=DEFAULT_INDEX_MAX_BYTES, metavar="BYTES",
        help=f"整个 index JSON 的字节预算（默认 {DEFAULT_INDEX_MAX_BYTES}）",
    )
    command.add_argument(
        "--gap-minutes", type=int, default=90, metavar="MINUTES",
        help="同一来源/会话合并 Activity 的间隔（默认 90）",
    )
    command.add_argument("--json", action="store_true", help="输出完整 index JSON")
    command.set_defaults(handler=command_index)

    command = commands.add_parser(
        "projects",
        help="查看由项目清单派生的 workspace→project 身份映射",
        description=(
            "只读 Work 注册的 lifeos-project.json。项目根路径来自清单位置；"
            "Sessions 不再维护第二份私有项目映射。"
        ),
        epilog="读取 index/pack 时按当前项目清单即时解析 project_key。",
    )
    command.add_argument("--json", action="store_true", help="输出派生映射 JSON")
    command.set_defaults(handler=command_projects)

    command = commands.add_parser(
        "pack",
        help="只读生成选中 Activity 的有界正文（detail view）",
        description=(
            "Activity 与 index 使用同一来源/会话/时间间隔聚合；必须提供带时区 ISO 8601"
            " 半开窗口 [from, to)。--project、--query、--activity 均可选；无筛选时"
            "基于窗口内全部 facts，再按 --max-bytes 预算取有界正文。用 index 的"
            " --activity 可深读指定 Activity，结果仍可能因预算省略活动。"
        ),
        epilog=(
            "--max-bytes 是完整 pack JSON 的预算；--blocks/--block-chars 限制每个 Activity"
            " 的正文。预算外 Activity 按项目计数并列出 omission/cleaning_summary，"
            "不回源、不写入 Store。"
        ),
    )
    command.add_argument(
        "--from", dest="from_value", required=True, metavar="ISO-8601",
        help="窗口下界（含），必须带时区",
    )
    command.add_argument(
        "--to", dest="to_value", required=True, metavar="ISO-8601",
        help="窗口上界（不含），必须带时区",
    )
    command.add_argument("--source", action="append", choices=SOURCES, help="可重复限定来源")
    command.add_argument("--conversation", metavar="CONVERSATION-ID", help="限定会话")
    command.add_argument("--workspace", metavar="PATH", help="限定 workspace 路径")
    command.add_argument("--project", metavar="PROJECT", help="限定归一后的 project_key")
    command.add_argument("--query", metavar="TEXT", help="限定全文索引关键词")
    command.add_argument(
        "--activity",
        action="append",
        metavar="ACT-...",
        help="只取这些 Activity（来自 index，必须复用同一窗口与 --gap-minutes）",
    )
    command.add_argument(
        "--blocks", type=int, default=MAX_ACTIVITY_BLOCKS, metavar="COUNT",
        help=f"每个 Activity 最多取几段正文（默认 {MAX_ACTIVITY_BLOCKS}）",
    )
    command.add_argument(
        "--block-chars", type=int, default=MAX_BLOCK_TEXT, metavar="CHARS",
        help=f"每段正文最多保留多少字（默认 {MAX_BLOCK_TEXT}）",
    )
    command.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES, metavar="BYTES",
        help=f"整个 pack JSON 的字节预算（默认 {DEFAULT_MAX_BYTES}）",
    )
    command.add_argument(
        "--gap-minutes", type=int, default=90, metavar="MINUTES",
        help="同一来源/会话合并 Activity 的间隔（默认 90；需与 index 一致）",
    )
    command.add_argument("--json", action="store_true", help="输出完整 pack JSON")
    command.set_defaults(handler=command_pack)
