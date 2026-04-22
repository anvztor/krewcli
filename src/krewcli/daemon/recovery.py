"""Orphan recovery — fail stuck tasks on daemon startup.

When the daemon crashes mid-task, tasks may be left in ``working``
status with no process to complete them. On startup, this module
queries krewhub for such orphaned tasks and marks them ``blocked``
so they can be retried or investigated.

Follows multica's ``RecoverOrphans()`` pattern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from krewcli.client.krewhub_client import KrewHubClient

logger = logging.getLogger(__name__)


async def recover_orphans(
    client: "KrewHubClient",
    agent_ids: list[str],
) -> int:
    """Fail tasks stuck in 'working' status from a prior crash.

    Queries krewhub for tasks claimed by our agent_ids that are
    still in 'working' status. Marks them 'blocked' with reason
    'daemon_crash_recovery'.

    Returns the number of recovered tasks.
    """
    recovered = 0
    working_tasks = await client.get_working_tasks(agent_ids)

    for task in working_tasks:
        task_id = task.get("id", "")
        claimed_by = task.get("claimed_by_agent_id", "")
        if claimed_by not in agent_ids:
            continue
        try:
            await client.update_task_status(
                task_id,
                status="blocked",
                blocked_reason="daemon_crash_recovery: task was in-flight when daemon stopped",
            )
            recovered += 1
            logger.info(
                "recovery: marked orphaned task %s as blocked (was claimed by %s)",
                task_id, claimed_by,
            )
        except Exception:
            logger.warning(
                "recovery: failed to recover orphaned task %s", task_id,
            )

    if recovered:
        logger.info("recovery: recovered %d orphaned task(s)", recovered)

    return recovered
