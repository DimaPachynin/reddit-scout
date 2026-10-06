"""Coverage report: what was requested, what is actually stored, known gaps.

No coverage percentage is computed for posts: the total number of posts in a
subreddit for a period is not known from any source used here. For comments,
Reddit's own ``num_comments`` gives an approximate per-thread reference.
"""

from __future__ import annotations

import json

from .config import Config
from .storage import Store


def coverage(store: Store, cfg: Config) -> dict:
    sub, start, end = cfg.subreddit, cfg.period.start, cfg.period.end
    all_posts = store.posts_in_period(sub, start, end, include_purged=True)
    active = [p for p in all_posts if p["status"] == "active"]
    purged_posts = len(all_posts) - len(active)
    ids = [p["id"] for p in active]

    comments_active = comments_purged = 0
    reported = 0
    complete = partial = not_loaded = 0
    stub_pending = stub_abandoned = hidden_estimate = 0
    comment_dates = []
    for p in active:
        row = store.db.execute(
            "SELECT SUM(status='active'), SUM(status!='active'), MIN(created_utc), MAX(created_utc) "
            "FROM comments WHERE post_id=?", (p["id"],)
        ).fetchone()
        a, d = row[0] or 0, row[1] or 0
        comments_active += a
        comments_purged += d
        if row[2]:
            comment_dates += [row[2], row[3]]
        reported += p["num_comments"] or 0
        stubs = store.stubs_for_post(p["id"])
        if any(s["parent_id"] == p["id"] and json.loads(s["children"]) == [] and a + d == 0 for s in stubs):
            not_loaded += 1
        elif stubs:
            partial += 1
        else:
            complete += 1
        for s in stubs:
            stub_pending += s["status"] == "pending"
            stub_abandoned += s["status"] == "abandoned"
            hidden_estimate += s["count"]

    runs = [dict(r) for r in store.runs(sub)]
    gaps = []
    latest = {}
    for r in runs:  # only the latest run of each kind describes the current state
        latest[(r["kind"], json.loads(r["params"]).get("source_id"))] = r
    for r in latest.values():
        stats = json.loads(r["stats"] or "{}")
        gaps += [f"run #{r['id']} ({r['kind']}): {g}" for g in stats.get("gaps", [])]
        if r["status"] in ("denied", "interrupted", "failed", "running"):
            gaps.append(f"run #{r['id']} ({r['kind']}) ended as {r['status']}: {r['stop_reason'] or 'no reason recorded'}")
    if not_loaded:
        gaps.append(f"{not_loaded} thread(s) have no comments loaded (posts-only input)")
    if partial:
        gaps.append(f"{partial} thread(s) have incomplete comment trees ({stub_pending} pending, "
                    f"{stub_abandoned} abandoned 'more' stubs, about {hidden_estimate} comments not loaded)")
    if not runs:
        gaps.append("no collection or import runs recorded yet")

    sources = [dict(s) for s in store.sources()]
    used_sources = {r[0] for r in store.db.execute(
        "SELECT DISTINCT source_id FROM posts WHERE lower(subreddit)=lower(?)", (sub,))}
    return {
        "subreddit": sub,
        "requested_period": cfg.period.label(),
        "comment_period": cfg.comment_period.label() if cfg.comment_period else None,
        "actual_post_dates": (active[0]["created_utc"][:10], active[-1]["created_utc"][:10]) if active else None,
        "actual_comment_dates": (min(comment_dates)[:10], max(comment_dates)[:10]) if comment_dates else None,
        "posts": len(active),
        "posts_purged": purged_posts,
        "comments": comments_active,
        "comments_purged": comments_purged,
        "comments_reported_by_reddit": reported,
        "threads_complete": complete,
        "threads_partial": partial,
        "threads_without_comments": not_loaded,
        "post_ids": ids,
        "gaps": gaps,
        "runs": [{k: r[k] for k in ("id", "kind", "status", "stop_reason", "started_at", "finished_at")} for r in runs],
        "sources": [s for s in sources if s["id"] in used_sources],
        "post_coverage_percent": None,  # unknown denominator; never invented
    }


def render_text(cov: dict) -> str:
    lines = [
        f"r/{cov['subreddit']}",
        f"  requested period:     {cov['requested_period']}",
        f"  comment date filter:  {cov['comment_period'] or 'none'}",
        f"  actual post dates:    {' — '.join(cov['actual_post_dates']) if cov['actual_post_dates'] else 'no data'}",
        f"  actual comment dates: {' — '.join(cov['actual_comment_dates']) if cov['actual_comment_dates'] else 'no data'}",
        f"  posts: {cov['posts']} (purged: {cov['posts_purged']})",
        f"  comments: {cov['comments']} (purged: {cov['comments_purged']}; "
        f"Reddit reported ≈{cov['comments_reported_by_reddit']} for these threads)",
        f"  threads: complete {cov['threads_complete']}, partial {cov['threads_partial']}, "
        f"without comments {cov['threads_without_comments']}",
        "  post coverage %: unknown (total number of posts in the period is not known)",
        "  known gaps:",
    ]
    lines += [f"    - {g}" for g in cov["gaps"]] or ["    - none recorded"]
    lines.append("  runs:")
    lines += [f"    #{r['id']} {r['kind']} {r['status']}: {r['stop_reason']}" for r in cov["runs"]]
    return "\n".join(lines)
