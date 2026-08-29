"""Daily report storage for LifeOS.

Reports are the one part of the private runtime whose body is written by a
model rather than by deterministic code.  This package therefore owns only the
parts that *can* be checked: where a report lives, what it is called, who may
read it, whether its frontmatter is well formed, and whether an already
confirmed day can be overwritten.  The prose itself belongs to the Agent skill.
"""

from .store import ReportError

__all__ = ["ReportError"]
