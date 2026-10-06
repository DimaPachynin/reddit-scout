"""Retention: decide which stored items have expired and purge them.

The rule comes from each item's source (``sources.max_hours_since_check``,
set from ``[retention].max_days_since_check`` or ``import --max-days``).
"Checked" means: contained in an import. An item not seen in any import for
longer than the limit is expired: its text is purged from the database, the
index and (on the next export) from the Obsidian notes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .storage import Store, iso


def _stale(store: Store, hours_expr: str, now: datetime, subreddit: str | None):
    rows = []
    for table in ("posts", "comments"):
        sql = f"""SELECT t.id FROM {table} t JOIN sources s ON s.id = t.source_id
                  WHERE t.status = 'active' AND {hours_expr} IS NOT NULL
                  AND t.last_checked_at < strftime('%Y-%m-%dT%H:%M:%S+00:00', ?, '-' || {hours_expr} || ' hours')"""
        args = [iso(now)]
        if subreddit:
            sql += " AND lower(t.subreddit) = lower(?)"
            args.append(subreddit)
        rows += [r[0] for r in store.db.execute(sql, args)]
    return rows


def expired_ids(store: Store, now: datetime | None = None, subreddit: str | None = None) -> list[str]:
    return _stale(store, "s.max_hours_since_check", now or datetime.now(UTC), subreddit)


def purge_expired(store: Store, now: datetime | None = None, subreddit: str | None = None) -> int:
    ids = expired_ids(store, now, subreddit)
    with store.transaction():
        for tid in ids:
            store.purge(tid, "retention limit: not confirmed by a newer import in time", commit=False)
    return len(ids)


def purge_ids(store: Store, ids: list[str], reason: str) -> int:
    n = 0
    with store.transaction():
        for tid in ids:
            tid = tid.strip()
            if not tid:
                continue
            if not (tid.startswith("t1_") or tid.startswith("t3_")):
                raise ValueError(f"expected a fullname like t1_xxx or t3_xxx, got {tid!r}")
            store.purge(tid, reason, commit=False)
            n += 1
    return n
