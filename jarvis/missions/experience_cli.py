"""Manage the local mission experience index without changing mission history.

Examples::

    python -m jarvis.missions.experience_cli stats
    python -m jarvis.missions.experience_cli forget --mission-id MISSION_ID
    python -m jarvis.missions.experience_cli clear --yes

The data path defaults to the application's data/missions.db location when
run from the repository, or can be passed explicitly with --db.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .experience import MissionExperienceMemory


async def _run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stats", "forget", "clear"))
    parser.add_argument("--db", type=Path, default=Path("data/mission-experience.db"))
    parser.add_argument("--mission-id", type=str, help="Mission to forget")
    parser.add_argument("--yes", action="store_true", help="Confirm deletion of learned patterns")
    args = parser.parse_args()

    memory = MissionExperienceMemory(args.db)
    if args.command == "stats":
        print(f"Learned successful mission patterns: {await memory.stats()}")
    elif args.command == "forget":
        if not args.mission_id:
            parser.error("Forget requires --mission-id")
        forgotten = await memory.forget(args.mission_id)
        print("Pattern forgotten." if forgotten else "No matching pattern found.")
    elif args.yes:
        count = await memory.stats()
        await memory.clear()
        print(f"Deleted {count} learned patterns. Existing mission history and notebooks unchanged.")
    else:
        parser.error("Clear requires --yes; no data has been changed")


if __name__ == "__main__":
    asyncio.run(_run())
