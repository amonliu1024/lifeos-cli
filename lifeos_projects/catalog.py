"""Dynamic discovery of project-owned ``lifeos-project.json`` manifests."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lifeos_config.core import LifeOSConfig, load_config

from .manifest import MANIFEST_NAME, ProjectManifestError, load_manifest


HARD_FINDING_CODES = {
    "config_error",
    "invalid_manifest",
    "key_conflict",
    "missing_root",
    "scan_error",
}


@dataclass(frozen=True)
class CatalogFinding:
    code: str
    message: str
    paths: tuple[str, ...] = ()
    project_key: str | None = None

    @property
    def severity(self) -> str:
        return "error" if self.code in HARD_FINDING_CODES else "warning"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "paths": list(self.paths),
            "project_key": self.project_key,
        }


@dataclass(frozen=True)
class CatalogProject:
    project_key: str
    name: str
    aliases: tuple[str, ...]
    scope: str
    sources: dict[str, Any]
    manifest_path: str
    root: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_key": self.project_key,
            "name": self.name,
            "aliases": list(self.aliases),
            "scope": self.scope,
            "sources": self.sources,
            "manifest_path": self.manifest_path,
            "root": self.root,
        }


@dataclass(frozen=True)
class ProjectCatalog:
    roots: tuple[str, ...]
    projects: tuple[CatalogProject, ...]
    findings: tuple[CatalogFinding, ...]
    complete: bool

    @property
    def by_key(self) -> dict[str, CatalogProject]:
        return {item.project_key: item for item in self.projects}

    @property
    def hard_findings(self) -> tuple[CatalogFinding, ...]:
        return tuple(
            item for item in self.findings if item.code in HARD_FINDING_CODES
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "complete": self.complete,
            "roots": list(self.roots),
            "projects": [item.to_dict() for item in self.projects],
            "findings": [item.to_dict() for item in self.findings],
            "summary": {
                "roots": len(self.roots),
                "projects": len(self.projects),
                "findings": len(self.findings),
                "errors": len(self.hard_findings),
            },
        }


def _manifest_paths(root: Path, excludes: set[str]) -> Iterable[Path]:
    def raise_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(
        root, followlinks=False, onerror=raise_error
    ):
        directories[:] = sorted(
            name
            for name in directories
            if name not in excludes and not (Path(current) / name).is_symlink()
        )
        if MANIFEST_NAME in files:
            yield Path(current) / MANIFEST_NAME


def discover_projects(config: LifeOSConfig | None = None) -> ProjectCatalog:
    resolved = config or load_config()
    roots = tuple(resolved.project_roots)
    excludes = set(resolved.project_excludes)
    findings: list[CatalogFinding] = []
    candidates: list[CatalogProject] = []
    seen_manifest_paths: set[str] = set()
    complete = True

    for raw_root in roots:
        root = Path(raw_root)
        if not root.is_dir():
            complete = False
            findings.append(CatalogFinding(
                "missing_root",
                f"项目发现根不存在或不是目录：{root}",
                (str(root),),
            ))
            continue
        try:
            paths = list(_manifest_paths(root, excludes))
        except OSError as exc:
            complete = False
            findings.append(CatalogFinding(
                "scan_error",
                f"项目发现根扫描失败：{root}（{exc}）",
                (str(root),),
            ))
            continue
        for path in paths:
            normalized_path = str(path.absolute())
            if normalized_path in seen_manifest_paths:
                continue
            seen_manifest_paths.add(normalized_path)
            try:
                manifest = load_manifest(path)
            except ProjectManifestError as exc:
                findings.append(CatalogFinding(
                    "invalid_manifest",
                    str(exc),
                    (str(path),),
                ))
                continue
            candidates.append(CatalogProject(
                project_key=manifest["project_key"],
                name=manifest["name"],
                aliases=tuple(manifest["aliases"]),
                scope=manifest["scope"],
                sources=manifest["sources"],
                manifest_path=normalized_path,
                root=str(path.parent.absolute()),
            ))

    by_key: dict[str, list[CatalogProject]] = {}
    for project in candidates:
        by_key.setdefault(project.project_key, []).append(project)
    valid: list[CatalogProject] = []
    for key, values in sorted(by_key.items()):
        if len(values) == 1:
            valid.append(values[0])
            continue
        paths = tuple(sorted(item.manifest_path for item in values))
        findings.append(CatalogFinding(
            "key_conflict",
            f"project_key 同时存在于多个当前清单：{key}",
            paths,
            key,
        ))

    name_owners: dict[str, list[CatalogProject]] = {}
    for project in valid:
        for name in (project.name, *project.aliases):
            name_owners.setdefault(name.casefold(), []).append(project)
    for ambiguous_key, values in name_owners.items():
        keys = sorted({item.project_key for item in values})
        if len(keys) < 2:
            continue
        display = next(
            name
            for item in values
            for name in (item.name, *item.aliases)
            if name.casefold() == ambiguous_key
        )
        findings.append(CatalogFinding(
            "name_ambiguous",
            f"项目名称或别名存在歧义：{display}（{', '.join(keys)}）",
            tuple(sorted({item.manifest_path for item in values})),
        ))

    return ProjectCatalog(
        roots=roots,
        projects=tuple(sorted(valid, key=lambda item: item.project_key)),
        findings=tuple(findings),
        complete=complete,
    )


__all__ = [
    "CatalogFinding",
    "CatalogProject",
    "HARD_FINDING_CODES",
    "ProjectCatalog",
    "discover_projects",
]
