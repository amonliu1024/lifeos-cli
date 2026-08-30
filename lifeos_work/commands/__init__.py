"""Command handlers for the LifeOS Work domain.

Handlers are grouped by the fact source they own.  The CLI composition root
imports these functions and remains responsible for argparse registration.
"""

from .achievements import (
    command_achievement_add,
    command_achievement_archive,
    command_achievement_supersede,
    command_achievement_update,
    command_achievements,
)
from .projects import command_project_track, command_project_update, command_projects
from .work_items import (
    command_work_item_add,
    command_work_item_milestone_add,
    command_work_item_milestone_update,
    command_work_item_milestones,
    command_work_item_update,
    command_work_items,
)

__all__ = [
    "command_achievement_add",
    "command_achievement_archive",
    "command_achievement_supersede",
    "command_achievement_update",
    "command_achievements",
    "command_project_track",
    "command_project_update",
    "command_projects",
    "command_work_item_add",
    "command_work_item_milestone_add",
    "command_work_item_milestone_update",
    "command_work_item_milestones",
    "command_work_item_update",
    "command_work_items",
]
