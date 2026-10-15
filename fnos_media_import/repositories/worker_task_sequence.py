from __future__ import annotations

import sqlite3


def preserve_worker_task_sequence(connection: sqlite3.Connection) -> None:
    """Keep new task IDs above retained notification fences in the caller's write transaction."""
    # Some isolated queue fixtures have no notification subsystem. Only that
    # absent table is optional; malformed schemas and other DB errors propagate.
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_deliveries'"
    ).fetchone() is None:
        return
    high_water = int(connection.execute(
        "SELECT MAX(value) FROM ("
        "SELECT COALESCE(MAX(task_id), 0) AS value FROM notification_deliveries "
        "WHERE typeof(task_id)='integer' UNION ALL "
        "SELECT COALESCE(MAX(id), 0) AS value FROM worker_tasks)"
    ).fetchone()[0] or 0)
    if high_water <= 0:
        return
    updated = connection.execute(
        "UPDATE sqlite_sequence SET seq=MAX(COALESCE(seq, 0), ?) WHERE name='worker_tasks'",
        (high_water,),
    )
    if updated.rowcount == 0:
        connection.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('worker_tasks', ?)", (high_water,))
