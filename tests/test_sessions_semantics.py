import json
import tempfile
import unittest
from pathlib import Path

from lifeos_sessions.projects import ProjectMap, normalize_path
from lifeos_sessions.retention import RetentionPolicy
from lifeos_sessions.semantics import (
    build_execution_evidence,
    classify_origin,
    classify_warnings,
    has_readable_content,
    is_approval_verdict,
    is_read_only_check,
    is_verifying_check,
    normalize_title,
    partition_targets,
    prose_excerpt,
    prose_lines,
    readable_blocks,
    text_shape,
)


def block(text, role="self", kind="message", context=False):
    return {
        "kind": kind,
        "author_role": role,
        "origin": classify_origin(role, text),
        "at": "2026-08-08T00:00:00Z",
        "text": text,
        "context": context,
    }


class BlockSemanticsTest(unittest.TestCase):
    def test_application_injected_text_is_not_treated_as_the_person_speaking(self):
        cases = {
            "<recommended_plugins>\nfoo": "system_injected",
            "<turn_aborted>\nThe user interrupted": "system_injected",
            "<environment_context>\n  <current_date>": "system_injected",
            "[Request interrupted by user for tool use]": "system_injected",
            "↪ 能不能自己开一个空间处理": "system_injected",
            "The following is the Codex agent history whose request action": "system_injected",
            "帮我把会议纪要同步到项目里": "user",
            "<不是标签开头的普通中文>也要当成用户内容": "user",
        }
        for text, expected in cases.items():
            self.assertEqual(expected, classify_origin("self", text), text[:24])
        # Only the transport role ``self`` can be injected; an agent block is
        # always the agent.
        self.assertEqual("agent", classify_origin("agent", "<recommended_plugins>"))

    def test_readable_blocks_drop_context_injected_and_empty_text(self):
        value = {"blocks": [
            block("真实提问"),
            block("<recommended_plugins>x"),
            block("上下文", context=True),
            block("   "),
            block("代理回复", role="agent", kind="agent_message"),
        ]}
        result = readable_blocks(value)
        self.assertEqual(["真实提问", "代理回复"], [item["text"] for item in result])
        self.assertEqual(["user", "agent"], [item["origin"] for item in result])
        self.assertEqual(["message", "agent_message"], [item["kind"] for item in result])

    def test_a_slice_of_only_injected_text_has_no_readable_content(self):
        self.assertFalse(has_readable_content({"blocks": [block("<turn_aborted>\nx")]}))
        self.assertTrue(has_readable_content({"blocks": [block("真实内容")]}))

    def test_known_application_headers_are_injected_but_similar_user_heading_stays_user(self):
        injected = (
            "The following is the Codex agent history added by the application",
            "# AGENTS.md instructions for /synthetic/workspace\n规则正文",
            "# AGENTS.md instructions\n\n<INSTRUCTIONS>\n规则正文",
            "# Files mentioned by the user:\n- /synthetic/file.py",
            "<codex_internal_context source=synthetic>",
            "# Applications mentioned by the user:\n- Synthetic App",
            "[Slock inbox notice: synthetic message]",
            "<subagent_notification>\n合成通知",
        )
        for text in injected:
            self.assertEqual("system_injected", classify_origin("self", text), text[:40])
            self.assertEqual([], readable_blocks({"blocks": [block(text)]}), text[:40])

        own_heading = "# AGENTS.md instructions 这个文件要不要改"
        self.assertEqual("user", classify_origin("self", own_heading))
        self.assertEqual([own_heading], [item["text"] for item in readable_blocks({"blocks": [block(own_heading)]})])


class ExecutionEvidenceTest(unittest.TestCase):
    def test_read_only_classification_uses_the_exact_command_basename_set(self):
        read_only_basenames = (
            "sed", "nl", "rg", "cat", "head", "tail", "wc", "ls", "find", "grep",
            "less", "file", "stat", "tree", "du", "pwd", "echo", "printf", "which",
            "type", "jq", "column", "sort", "uniq", "cut", "awk", "diff", "basename",
            "dirname", "realpath", "readlink",
        )
        for basename in read_only_basenames:
            self.assertTrue(is_read_only_check(f"{basename} synthetic: passed"), basename)
        self.assertTrue(is_read_only_check("/usr/bin/sed -n 1,20p README.md: passed"))
        self.assertFalse(is_read_only_check("git status: passed"))
        self.assertFalse(is_read_only_check("case_lab_browser.py: passed"))

    def test_known_write_forms_of_read_oriented_commands_are_substantive(self):
        self.assertTrue(is_read_only_check("sed -n 1,20p README.md: passed"))
        for command in (
            "sed -i x file: passed",
            "sed --in-place=.bak file: passed",
            "sed -ni x file: passed",
            "find . -delete: passed",
            "find . -exec touch output \\;: passed",
            "find . -execdir touch output \\;: passed",
            "find . -ok rm output \\;: passed",
            "find . -okdir rm output \\;: passed",
            "find . -fprint output: passed",
            "find . -fprint0 output: passed",
            "find . -fprintf output '%p\\n': passed",
            "find . -fls output: passed",
            "sort -o output input: passed",
            "sort --output=output input: passed",
            "sort -ooutput input: passed",
            "awk -i inplace '{ print }' file: passed",
        ):
            self.assertFalse(is_read_only_check(command), command)
        # A compound command is retained when any segment has an unprovable
        # write form, even if its other segment is a known read.
        self.assertFalse(is_read_only_check("cat file && find . -delete: passed"))
        self.assertFalse(is_read_only_check("cat file && echo $(touch output): passed"))

    def test_tool_calls_are_not_reported_as_verifications(self):
        evidence = build_execution_evidence(
            checks=["Read: passed", "exec: passed", "pytest -q: passed", "lint: passed"])
        self.assertEqual(["Read: passed", "exec: passed"], evidence["tool_calls"])
        self.assertEqual(["pytest -q: passed", "lint: passed"], evidence["verifications"])
        self.assertTrue(is_verifying_check("pytest: passed"))
        self.assertFalse(is_verifying_check("Read: passed"))

    def test_verification_commands_scan_the_full_shell_command_without_help_false_positives(self):
        positives = (
            '/bin/zsh -lc "cd /x && python3 -m unittest discover": passed',
            "npm test: passed",
            "ruff check .: passed",
        )
        negatives = (
            "cat tests/test_foo.py: passed",
            "ls tests: passed",
            "git status: passed",
            "lifeos --help: passed",
            # ``test`` is a verifying *tool name*, but as a shell word it is
            # the file predicate -- both of these appear in the real corpus and
            # neither ran a test.  A command must not inherit the name rule.
            "test -f ../scripts/parse.groovy && echo present: passed",
            "test ! -d .git: passed",
            # Installing tools named after checkers runs none of them.
            "pip3 install orjson pyyaml mypy ruff lxml: passed",
            # Naming a runner is not running it: the first printed the words,
            # the second searched for a script by name.
            'echo "=== AGENTS make test line ===": passed',
            "rg -n 'test.sh|ci_swift_test' Scripts/compile_and_run.sh: passed",
        )
        # An install followed by a real run still counts -- each segment is
        # judged on its own rather than poisoning the whole line.
        self.assertTrue(is_verifying_check("npm install && npm test: passed"))
        # A leading environment assignment is not the command.
        self.assertTrue(is_verifying_check("SUPPRESS_KEYCHAIN=1 swift test --filter X: passed"))
        for value in positives:
            self.assertTrue(is_verifying_check(value), value)
        for value in negatives:
            self.assertFalse(is_verifying_check(value), value)

    def test_script_paths_are_checked_only_in_command_or_interpreter_script_position(self):
        expected = {
            "python3 scripts/run_cases.py: passed": False,
            "groovy scripts/local/verify-x.groovy: passed": True,
            "./scripts/test.sh: passed": True,
            "nl -ba src/foo_test.py: passed": False,
            "rg -n 'test.sh': passed": False,
            "pip3 install pytest: passed": False,
        }
        for value, result in expected.items():
            self.assertEqual(result, is_verifying_check(value), value)

    def test_bare_failure_labels_are_omitted_but_descriptive_failures_remain(self):
        descriptive = "Traceback: command failed after checking the synthetic fixture at line 160"
        evidence = build_execution_evidence(
            failures=["error", "patch_apply_end", "replaced", descriptive]
        )
        self.assertEqual([descriptive], evidence["failures"])
        self.assertEqual(3, evidence["omitted_count"])

    def test_titles_drop_placeholders_and_bound_readable_text(self):
        for value in (None, "", "   ", "未命名会话", "Untitled", "New Chat", "新会话"):
            self.assertIsNone(normalize_title(value), value)
        self.assertEqual("真实 标题", normalize_title("  真实\n标题  "))
        self.assertEqual(120, len(normalize_title("题" * 121)))

    def test_cache_screenshot_and_uuid_targets_are_excluded(self):
        evidence = build_execution_evidence(changed_targets=[
            "src/real.py",
            "/Users/tester/.SmartWork/cache/images/screenshots/tab-1.png",
            "019fda48-129e-7a42-8561-544b6a9c7810",
            "/private/tmp/scratch.txt",
            "src/real.py",
        ])
        self.assertEqual(["src/real.py"], evidence["changed_targets"])
        # Dropped noise is counted, not silently discarded.
        self.assertEqual(3, evidence["omitted_count"])

    def test_sub_agent_names_are_separated_from_changed_files(self):
        changed, other, dropped = partition_targets(
            ["src/real.py", "explore_codex_adapter", "Makefile", "docs/a.md"])
        # A bare identifier cannot be proven to be a file, so it is set aside
        # rather than deleted or counted as a change.
        self.assertEqual(["src/real.py", "docs/a.md"], changed)
        self.assertEqual(["explore_codex_adapter", "Makefile"], other)
        self.assertEqual(0, dropped)

    def test_user_interrupt_is_separated_from_a_real_failure(self):
        evidence = build_execution_evidence(
            failures=["interrupted", "pytest: 3 failed", "turn_aborted"])
        self.assertEqual(["pytest: 3 failed"], evidence["failures"])
        self.assertEqual(["interrupted", "turn_aborted"], evidence["user_interrupts"])


class ClassifyWarningsTest(unittest.TestCase):
    def test_trimmed_references_do_not_count_as_missing_content(self):
        detail = classify_warnings(["execution_evidence_source_refs_omitted:4"])
        self.assertEqual("complete", detail["content_completeness"])
        self.assertTrue(detail["provenance_trimmed"])

    def test_no_warnings_means_complete_and_untrimmed(self):
        detail = classify_warnings([])
        self.assertEqual("complete", detail["content_completeness"])
        self.assertFalse(detail["provenance_trimmed"])

    def test_a_broken_turn_or_an_unrecognised_record_does_count(self):
        broken = classify_warnings(["incomplete_turn session=x"])
        self.assertEqual("partial", broken["content_completeness"])
        self.assertEqual(["incomplete_turn"], broken["content_loss_reasons"])

        # Every type the adapters actually see is classified, so this now
        # means the source emitted something genuinely new.
        unknown = classify_warnings(["unknown_record_type:mystery_kind"])
        self.assertEqual("partial", unknown["content_completeness"])

        corrupt = classify_warnings(["source_contains_malformed_record"], source_corrupt=True)
        self.assertEqual("truncated", corrupt["content_completeness"])


class ProjectMapTest(unittest.TestCase):
    def test_derived_map_requires_an_explicit_schema1(self):
        for payload in ({"projects": []}, {"schema_version": 2, "projects": []}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, "schema_version must be 1"):
                    ProjectMap.from_dict(payload)

    def test_scratch_chat_directories_are_not_projects(self):
        resolved = ProjectMap.default().resolve("~/Documents/Codex/2026-08-08/new-chat")
        self.assertEqual("ad_hoc", resolved.kind)
        self.assertEqual("ad-hoc", resolved.project_key)

    def test_an_unmapped_path_stays_itself_rather_than_being_guessed(self):
        resolved = ProjectMap.default().resolve("/Users/x/Desktop/thing")
        self.assertEqual("unmapped", resolved.kind)
        self.assertEqual("/Users/x/Desktop/thing", resolved.project_key)

    def test_confirmed_roots_absorb_case_variants_subdirectories_and_worktrees(self):
        project_map = ProjectMap(projects=[{
            "key": "quotation",
            "title": "报价单",
            "roots": [
                normalize_path("/Users/tester/Desktop/Ai/crm/fxiaoke-crm/quotation"),
                normalize_path("/Users/tester/Desktop/Ai/CRM/quotation"),
            ],
        }])
        for path in (
            "/Users/tester/Desktop/Ai/crm/fxiaoke-crm/quotation",
            "/Users/tester/Desktop/Ai/CRM/quotation",
            "/Users/tester/Desktop/Ai/crm/fxiaoke-crm/quotation/docs/evidence/run-results",
            "/Users/tester/Desktop/Ai/CRM/QUOTATION",
        ):
            self.assertEqual("quotation", project_map.resolve(path).project_key, path)
        self.assertEqual("报价单", project_map.resolve("/Users/tester/Desktop/Ai/CRM/quotation").title)

        worktree = ProjectMap(projects=[{
            "key": "rms", "title": None, "roots": [normalize_path("/Users/tester/Desktop/Ai/rms")],
        }])
        self.assertEqual("rms", worktree.resolve("~/.codex/worktrees/6477/rms").project_key)

    def test_the_longest_matching_root_wins_so_nested_projects_stay_apart(self):
        project_map = ProjectMap(projects=[
            {"key": "life-os", "title": None, "roots": ["/Users/tester/Desktop/Ai/personal/life-os"]},
            {"key": "lifeos-cli", "title": None,
             "roots": ["/Users/tester/Desktop/Ai/personal/life-os/lifeos-cli"]},
        ])
        self.assertEqual("life-os", project_map.resolve("/Users/tester/Desktop/Ai/personal/life-os/docs").project_key)
        self.assertEqual(
            "lifeos-cli",
            project_map.resolve("/Users/tester/Desktop/Ai/personal/life-os/lifeos-cli/tests").project_key,
        )

class TextShapeTest(unittest.TestCase):
    """A person's request and a pasted log arrive on the same channel."""

    LOG = "\n".join([
        "Traceback (most recent call last):",
        '  File "/Users/x/project/app/service.py", line 214, in handle',
        "    return self._dispatch(request, timeout=30)",
        "ValueError: unexpected token at offset 1288",
    ])

    def test_a_request_is_prose_in_either_language(self):
        self.assertEqual("prose", text_shape("把报价模板的评价体系重新校准一遍，先给我看方案"))
        self.assertEqual(
            "prose",
            text_shape("Please recalibrate the quotation template before we ship it."),
        )

    def test_a_pasted_log_is_not_prose(self):
        self.assertEqual("machine", text_shape(self.LOG))
        self.assertEqual("machine", text_shape("```\nprint(1)\nprint(2)\n```"))

    def test_a_request_with_a_log_pasted_under_it_keeps_only_the_request(self):
        text = "这个报错看一下，是不是模板取值错了\n\n" + self.LOG
        self.assertEqual("mixed", text_shape(text))
        excerpt, dropped = prose_excerpt(text)
        self.assertEqual("这个报错看一下，是不是模板取值错了", excerpt)
        self.assertEqual(4, dropped)

    def test_cutting_from_the_middle_is_marked_rather_than_silent(self):
        text = "先看这段\n" + self.LOG + "\n后面这句才是重点"
        excerpt, dropped = prose_excerpt(text)
        self.assertEqual("先看这段\n…\n后面这句才是重点", excerpt)
        self.assertEqual(4, dropped)

    def test_a_block_that_is_only_machine_text_yields_nothing_to_quote(self):
        excerpt, dropped = prose_excerpt(self.LOG)
        self.assertEqual("", excerpt)
        self.assertEqual(4, dropped)

    VERDICT = (
        '{"risk_level":"medium","user_authorization":"high","outcome":"allow",'
        '"rationale":"用户明确授权在 test 环境使用指定商机进行只读验证，不提交报价。"}'
    )

    def test_a_one_line_json_payload_is_machine_even_with_chinese_inside(self):
        # The indented form was already machine.  Compacting the same payload
        # onto one line used to make it prose, because the Chinese inside its
        # string values answered the language test before anything looked at
        # the shape of the line.
        self.assertEqual("machine", text_shape(self.VERDICT))
        self.assertEqual("machine", text_shape(json.dumps(json.loads(self.VERDICT), indent=2)))
        self.assertEqual([], prose_lines(self.VERDICT))
        self.assertEqual(("", 1), prose_excerpt(self.VERDICT))

    def test_prose_that_merely_contains_braces_or_json_is_still_prose(self):
        self.assertEqual("prose", text_shape("这个 {risk_level} 字段要不要保留，你怎么看"))
        self.assertEqual(
            "mixed",
            text_shape("裁决长这样，先看一眼：\n" + self.VERDICT),
        )
        excerpt, dropped = prose_excerpt("裁决长这样，先看一眼：\n" + self.VERDICT)
        self.assertEqual("裁决长这样，先看一眼：", excerpt)
        self.assertEqual(1, dropped)

    def test_an_approval_verdict_is_recognised_by_its_schema_not_its_wording(self):
        self.assertTrue(is_approval_verdict(self.VERDICT))
        # An extra field means the object is carrying something besides the
        # verdict, and the corpus has no such record: all 8,745 of them use a
        # subset of the four keys.  Requiring the subset is what keeps the
        # terse form below from matching an ordinary tool result.
        self.assertFalse(is_approval_verdict(json.dumps(
            {"risk_level": "low", "user_authorization": "high",
             "outcome": "deny", "rationale": "x", "extra": 1})))
        # The reviewer also answers tersely, with any subset that still
        # carries the verdict itself.
        self.assertTrue(is_approval_verdict('{"outcome":"allow"}'))
        self.assertTrue(is_approval_verdict('{"outcome":"deny","rationale":"未授权"}'))
        # A JSON object that is not the approval schema, and prose that merely
        # talks about one, are both somebody's content.  A tool result keeps
        # its own fields, so it can never be a subset of this vocabulary.
        self.assertFalse(is_approval_verdict('{"risk_level":"low"}'))
        self.assertFalse(is_approval_verdict('{"outcome":"success","files_written":3}'))
        self.assertFalse(is_approval_verdict("outcome allow, risk_level medium"))
        self.assertFalse(is_approval_verdict(""))

    def test_a_verdict_wrapped_in_a_markdown_fence_is_still_a_verdict(self):
        self.assertTrue(is_approval_verdict("```json\n" + self.VERDICT + "\n```"))
        self.assertTrue(is_approval_verdict("```\n" + self.VERDICT + "\n```"))
        # A fence around anything else is still content.
        self.assertFalse(is_approval_verdict('```json\n{"total": 3}\n```'))

    def test_named_lines_can_be_dropped_as_window_boilerplate(self):
        text = "# Agent 工作原则\n本文件是跨项目的系统级工作规则。\n把这段脚本改成幂等的"
        self.assertEqual(
            ["# Agent 工作原则", "本文件是跨项目的系统级工作规则。", "把这段脚本改成幂等的"],
            prose_lines(text),
        )
        excerpt, dropped = prose_excerpt(
            text, {"# Agent 工作原则", "本文件是跨项目的系统级工作规则。"})
        self.assertEqual("把这段脚本改成幂等的", excerpt)
        self.assertEqual(2, dropped)


class RetentionPolicyTest(unittest.TestCase):
    def test_default_policy_keeps_everything(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = RetentionPolicy.load(Path(temporary))
            self.assertFalse(policy.bounded)
            self.assertIsNone(policy.cutoff_ms("keep_slices_days", 1_000_000))

    def test_a_policy_that_would_orphan_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "fts_days"):
            RetentionPolicy(keep_slices_days=30, fts_days=90)
        with self.assertRaisesRegex(ValueError, "positive integer"):
            RetentionPolicy(keep_slices_days=0)

    def test_cutoff_is_the_declared_number_of_days_back(self):
        policy = RetentionPolicy(keep_slices_days=2)
        self.assertEqual(1_000_000_000 - 2 * 86_400_000, policy.cutoff_ms("keep_slices_days", 1_000_000_000))

    def test_unknown_fields_are_rejected_instead_of_silently_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "retention.json").write_text(
                json.dumps({"schema_version": 1, "keep_slice_days": 30}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                RetentionPolicy.load(root)

    def test_existing_policy_requires_an_explicit_schema1(self):
        for version in (None, 2):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                payload = {"keep_slices_days": 30}
                if version is not None:
                    payload["schema_version"] = version
                (root / "retention.json").write_text(
                    json.dumps(payload), encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "schema_version must be 1"):
                    RetentionPolicy.load(root)


if __name__ == "__main__":
    unittest.main()
