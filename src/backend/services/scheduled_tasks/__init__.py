"""Scheduled Tasks subsystem (#1137, docs/design/scheduled-tasks.md).

DB-defined recurring jobs (interval OR cron, start/end dates, enable toggle) run
by a single engine loop that spawns each due task as its own asyncio.Task.
"""
