"""Private Git commit evidence for LifeOS daily reports.

The Git domain only reads explicitly registered local repositories and stores
bounded commit metadata in the LifeOS private runtime.  It never changes a
repository, contacts a remote, or writes Work facts.
"""

from .core import GitEvidenceError, GitScanner, GitWindow, Repository
from .store import GitStore, GitStoreError

__all__ = [
    "GitEvidenceError",
    "GitScanner",
    "GitStore",
    "GitStoreError",
    "GitWindow",
    "Repository",
]
