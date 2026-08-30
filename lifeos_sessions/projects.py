"""Resolve a slice ``workspace`` to a manifest-derived project identity.

``workspace`` is the directory an Agent application happened to run in.  It is
not a project: one project appears under several paths (a rename, a case
variant, a git worktree, a subdirectory), and many paths are throwaway chat
directories that are not projects at all.  Grouping a report by raw workspace
therefore both splits real projects and invents fake ones.

Two kinds of rule live here and they are deliberately not equal:

*Structural* rules are derivable from the path itself — a worktree checkout
belongs to its project, a subdirectory belongs to its root, a scratch chat
directory is not a project.  Those are applied automatically.

*Identity* rules come from ``lifeos-project.json`` manifests dynamically found
under the configured project roots. Sessions consumes the derived roots in
memory and does not own a second project map or any project mutation path.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

SCHEMA_VERSION = 1

AD_HOC_KEY = "ad-hoc"
UNMAPPED_KIND = "unmapped"

# Directories an Agent application creates per chat rather than per project.
DEFAULT_AD_HOC_ROOTS = (
    "~/Documents/Codex",
    "~/Documents/Claude",
    "~/Documents/SmartWork",
)
# Checkout roots whose children are copies of some other project.
DEFAULT_WORKTREE_ROOTS = (
    "~/.codex/worktrees",
    "~/.claude/worktrees",
)


def normalize_path(value: Any) -> str:
    """Return a comparable absolute path without resolving symlinks.

    ``realpath`` is deliberately avoided: a workspace recorded months ago may
    no longer exist, and resolving it would silently rewrite history.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    text = os.path.expanduser(text)
    text = posixpath.normpath(text)
    if len(text) > 1:
        text = text.rstrip("/")
    return text


def _fold(value: str) -> str:
    # APFS is case-insensitive by default, so two case variants of one path
    # are the same directory and must never become two projects.
    return normalize_path(value).casefold()


def _is_within(path: str, root: str) -> bool:
    if not path or not root:
        return False
    folded_path = _fold(path)
    folded_root = _fold(root)
    return folded_path == folded_root or folded_path.startswith(folded_root + "/")


@dataclass(frozen=True)
class Resolution:
    """How one workspace was attributed."""

    project_key: str
    kind: str  # "project" | "ad_hoc" | "unmapped"
    title: Optional[str] = None
    matched_rule: Optional[str] = None
    workspace: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_key": self.project_key,
            "kind": self.kind,
            "title": self.title,
            "matched_rule": self.matched_rule,
        }


@dataclass
class ProjectMap:
    """Manifest-derived identity rules plus the structural defaults."""

    projects: List[Dict[str, Any]] = field(default_factory=list)
    ad_hoc_roots: List[str] = field(default_factory=lambda: list(DEFAULT_AD_HOC_ROOTS))
    worktree_roots: List[str] = field(default_factory=lambda: list(DEFAULT_WORKTREE_ROOTS))
    catalog_complete: bool = True
    catalog_findings: List[Dict[str, Any]] = field(default_factory=list)
    @classmethod
    def default(cls) -> "ProjectMap":
        return cls()

    @classmethod
    def from_dict(cls, payload: Any) -> "ProjectMap":
        """Validate one complete manifest-derived project-map payload."""

        if not isinstance(payload, Mapping):
            raise ValueError("derived project map must be an object")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(f"derived project map schema_version must be {SCHEMA_VERSION}")
        projects: List[Dict[str, Any]] = []
        for index, entry in enumerate(payload.get("projects") or []):
            if not isinstance(entry, Mapping):
                raise ValueError(f"project map projects[{index}] must be an object")
            key = str(entry.get("key") or "").strip()
            if not key:
                raise ValueError(f"project map projects[{index}].key is required")
            roots = [normalize_path(value) for value in entry.get("roots") or []]
            roots = [value for value in roots if value]
            if not roots:
                raise ValueError(f"project map projects[{index}].roots must not be empty")
            projects.append({
                "key": key,
                "title": str(entry["title"]) if entry.get("title") else None,
                "roots": roots,
            })
        keys = [entry["key"] for entry in projects]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(f"project map has duplicate keys: {', '.join(duplicates)}")
        claimed: Dict[str, str] = {}
        for entry in projects:
            for root in entry["roots"]:
                folded = _fold(root)
                if folded in claimed and claimed[folded] != entry["key"]:
                    raise ValueError(
                        f"project map root {root} is claimed by both "
                        f"{claimed[folded]} and {entry['key']}"
                    )
                claimed[folded] = entry["key"]
        ad_hoc = payload.get("ad_hoc_roots")
        worktrees = payload.get("worktree_roots")
        catalog = payload.get("catalog") or {}
        return cls(
            projects=projects,
            ad_hoc_roots=[normalize_path(v) for v in ad_hoc] if ad_hoc is not None else list(DEFAULT_AD_HOC_ROOTS),
            worktree_roots=[normalize_path(v) for v in worktrees] if worktrees is not None else list(DEFAULT_WORKTREE_ROOTS),
            catalog_complete=bool(catalog.get("complete", True)),
            catalog_findings=[dict(item) for item in catalog.get("findings", [])],
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "projects": [
                {"key": entry["key"], "title": entry.get("title"), "roots": list(entry["roots"])}
                for entry in sorted(self.projects, key=lambda item: item["key"])
            ],
            "ad_hoc_roots": list(self.ad_hoc_roots),
            "worktree_roots": list(self.worktree_roots),
            "catalog": {
                "complete": self.catalog_complete,
                "findings": [dict(item) for item in self.catalog_findings],
            },
        }

    # -- resolution ---------------------------------------------------------

    def resolve(self, workspace: Any) -> Resolution:
        """Attribute one workspace path to a project identity."""

        path = normalize_path(workspace)
        if not path:
            return Resolution(AD_HOC_KEY, "ad_hoc", matched_rule="empty_workspace", workspace=path)

        for root in self.ad_hoc_roots:
            if _is_within(path, root):
                return Resolution(AD_HOC_KEY, "ad_hoc", matched_rule=f"ad_hoc_root:{root}", workspace=path)

        candidate = path
        rule_prefix = ""
        for root in self.worktree_roots:
            if _is_within(path, root):
                # ``<worktrees>/<name>`` and ``<worktrees>/<id>/<name>`` both
                # end at the project's own directory name.
                leaf = posixpath.basename(path)
                candidate = leaf
                rule_prefix = f"worktree_root:{root}|"
                break

        best: Optional[Dict[str, Any]] = None
        best_root = ""
        for entry in self.projects:
            for root in entry["roots"]:
                if rule_prefix:
                    matched = _fold(posixpath.basename(root)) == _fold(candidate)
                else:
                    matched = _is_within(candidate, root)
                if not matched:
                    continue
                if len(root) > len(best_root):
                    best, best_root = entry, root
        if best is not None:
            return Resolution(
                best["key"],
                "project",
                title=best.get("title"),
                matched_rule=f"{rule_prefix}root:{best_root}",
                workspace=path,
            )
        return Resolution(path, UNMAPPED_KIND, matched_rule=None, workspace=path)

def resolve_all(project_map: ProjectMap, workspaces: Sequence[Any]) -> Dict[str, Resolution]:
    """Resolve many workspaces once, keyed by the normalized path."""

    result: Dict[str, Resolution] = {}
    for workspace in workspaces:
        path = normalize_path(workspace)
        if path in result:
            continue
        result[path] = project_map.resolve(path)
    return result
