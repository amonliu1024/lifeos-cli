import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from lifeos_projects.manifest import ProjectManifestError, load_manifest, normalize_manifest
from lifeos_config.core import default_payload, normalize_config
from lifeos_projects.catalog import discover_projects


def manifest(**overrides):
    payload = {
        "schema_version": 1,
        "project_key": "synthetic-project",
        "name": "合成项目",
        "aliases": ["Synthetic"],
        "scope": "project",
        "sources": {
            "dchat": {"groups": [{
                    "vid": "123456",
                    "name": "合成项目群",
                    "description": "项目核心协作群",
                }]},
            "cooper": {"resources": [{
                    "link": "https://cooper.didichuxing.com/docs2/sheet/123456",
                    "name": "需求大盘",
                    "description": "项目需求总入口",
                }]},
        },
    }
    payload.update(overrides)
    return payload


class ProjectManifestTest(unittest.TestCase):
    def test_exact_contract_round_trips(self):
        self.assertEqual(manifest(), normalize_manifest(manifest()))

    def test_underscore_scope_is_rejected(self):
        with self.assertRaisesRegex(ProjectManifestError, "project-group"):
            normalize_manifest(manifest(scope="project_group"))

    def test_alias_cannot_repeat_the_canonical_name(self):
        with self.assertRaisesRegex(ProjectManifestError, "项目 name"):
            normalize_manifest(manifest(aliases=["合成项目"]))

    def test_unknown_fields_are_rejected_at_every_level(self):
        payload = manifest(content_scope="project")
        with self.assertRaisesRegex(ProjectManifestError, "未知字段"):
            normalize_manifest(payload)
        payload = manifest()
        payload["sources"]["dchat"]["groups"][0]["since"] = "2026-01-01"
        with self.assertRaisesRegex(ProjectManifestError, "未知字段"):
            normalize_manifest(payload)

    def test_cooper_must_be_an_internal_cooper_link(self):
        for link in (
            "https://example.com/sheet/123",
            "https://cooper.evil-didichuxing.com/phish",
        ):
            with self.subTest(link=link):
                payload = manifest()
                payload["sources"]["cooper"]["resources"][0]["link"] = link
                with self.assertRaisesRegex(ProjectManifestError, "Cooper"):
                    normalize_manifest(payload)

    def test_directory_input_reads_fixed_manifest_name(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lifeos-project.json"
            path.write_text(json.dumps(manifest(), ensure_ascii=False), encoding="utf-8")
            self.assertEqual("synthetic-project", load_manifest(directory)["project_key"])

    def test_unknown_schema_is_rejected(self):
        with self.assertRaisesRegex(ProjectManifestError, "schema_version 必须为 1"):
            normalize_manifest(manifest(schema_version=2))


class ProjectCatalogTest(unittest.TestCase):
    def config(self, root: Path):
        payload = default_payload()
        payload["modules"]["projects"]["roots"] = [str(root)]
        return normalize_config(payload, root / "config.json", exists=True)

    def write_manifest(self, root: Path, key: str, name: Optional[str] = None):
        root.mkdir(parents=True, exist_ok=True)
        payload = manifest(
            project_key=key,
            name=name or key,
            aliases=[],
            sources={"dchat": {"groups": []}, "cooper": {"resources": []}},
        )
        (root / "lifeos-project.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_discovers_new_and_nested_projects_without_work_registration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_manifest(root / "one", "one")
            self.write_manifest(root / "group" / "child", "child")
            catalog = discover_projects(self.config(root))
            self.assertEqual(["child", "one"], sorted(catalog.by_key))
            self.assertFalse(catalog.findings)

    def test_duplicate_project_key_is_isolated_from_other_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_manifest(root / "one", "duplicate")
            self.write_manifest(root / "two", "duplicate")
            self.write_manifest(root / "healthy", "healthy")
            catalog = discover_projects(self.config(root))
            self.assertEqual(["healthy"], list(catalog.by_key))
            self.assertEqual("key_conflict", catalog.hard_findings[0].code)

    def test_invalid_manifest_does_not_hide_valid_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_manifest(root / "healthy", "healthy")
            invalid = root / "invalid"
            invalid.mkdir()
            (invalid / "lifeos-project.json").write_text("{}", encoding="utf-8")
            catalog = discover_projects(self.config(root))
            self.assertEqual(["healthy"], list(catalog.by_key))
            self.assertEqual("invalid_manifest", catalog.hard_findings[0].code)

    def test_overlapping_discovery_roots_do_not_duplicate_one_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "group"
            self.write_manifest(child / "project", "one")
            payload = default_payload()
            payload["modules"]["projects"]["roots"] = [str(root), str(child)]
            config = normalize_config(payload, root / "config.json", exists=True)
            catalog = discover_projects(config)
            self.assertEqual(["one"], list(catalog.by_key))
            self.assertFalse(catalog.findings)


if __name__ == "__main__":
    unittest.main()
