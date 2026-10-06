"""Import of local files the user is entitled to process.

The importer cannot verify rights. It requires an explicit, recorded basis
(``--basis``) and stores it as provenance next to every imported item.

Accepted files:
* .json / .jsonl in Reddit's JSON shapes (as found in data the user already has):
  - one thread: ``[post_listing, comment_listing]``
  - a list of threads: ``[[post_listing, comment_listing], ...]``
  - a bundle: ``{"threads": [...]}`` (optionally with a "note" field)
  - a post listing without comments: ``{"kind": "Listing", ...}``
  - JSON Lines, one thread per line
* optional: .html / .htm / .mhtml thread pages saved manually from a browser
  (see ``saved_html.py``). They are recorded as a separate source
  ``<source_id>-html`` so the notes show where each item came from.

Items from different files are merged by Reddit id; a newer file adds detail
(more comments, scores) and never duplicates.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterator

from ..config import Config
from ..normalize import FormatError, MoreStub, Post, Thread, parse_post_listing, parse_thread
from ..storage import Store, UpsertStats, now_iso
from . import saved_html

JSON_SUFFIXES = (".json", ".jsonl")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_thread(obj) -> bool:
    return (
        isinstance(obj, list)
        and len(obj) == 2
        and all(isinstance(x, dict) and x.get("kind") == "Listing" for x in obj)
    )


def iter_json_items(path: Path) -> Iterator[Thread | Post]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        for n, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    yield from _items(json.loads(line))
                except (json.JSONDecodeError, FormatError) as exc:
                    raise FormatError(f"{path.name}:{n}: {exc}") from exc
        return
    try:
        yield from _items(json.loads(text))
    except json.JSONDecodeError as exc:
        raise FormatError(f"{path.name}: invalid JSON: {exc}") from exc


def _items(obj) -> Iterator[Thread | Post]:
    if _is_thread(obj):
        yield parse_thread(obj)
    elif isinstance(obj, dict) and "threads" in obj:
        for t in obj["threads"]:
            yield parse_thread(t)
    elif isinstance(obj, dict) and obj.get("kind") == "Listing":
        posts, _ = parse_post_listing(obj)
        yield from posts
    elif isinstance(obj, list) and all(_is_thread(t) for t in obj):
        for t in obj:
            yield parse_thread(t)
    else:
        raise FormatError("unrecognised structure (expected Reddit thread/listing JSON)")


def collect_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            for x in sorted(p.rglob("*")):
                # Browsers put page assets into "<name>_files/"; they are not threads.
                if any(part.endswith("_files") for part in x.relative_to(p).parts[:-1]):
                    continue
                if x.is_file() and x.suffix.lower() in JSON_SUFFIXES + saved_html.HTML_SUFFIXES:
                    files.append(x)
        elif p.exists():
            files.append(p)
        else:
            raise FileNotFoundError(f"input not found: {p}")
    return files


def import_paths(
    store: Store,
    cfg: Config,
    paths: list[Path],
    *,
    source_id: str,
    description: str,
    basis: str,
    max_hours_since_check: int | None,
    kind: str = "local_file",
    force: bool = False,
    log: Callable[[str], None] = print,
    _fail_after_threads: int | None = None,  # test hook: simulate a crash
) -> dict:
    if not basis.strip():
        raise ValueError("a basis for use is required (who may process this data and why)")
    files = collect_files(paths)
    html_source = f"{source_id}-html"
    store.register_source(source_id, kind, description, basis, None, max_hours_since_check)
    if any(f.suffix.lower() in saved_html.HTML_SUFFIXES for f in files):
        store.register_source(html_source, "saved_html", f"{description} (pages saved manually from a browser)",
                              basis, None, max_hours_since_check)

    params = {"source_id": source_id, "files": [str(f) for f in files], "period": cfg.period.label()}
    run_id = store.start_run("import", cfg.subreddit, params)
    total = UpsertStats()
    counters = {"files_done": 0, "files_skipped": 0, "html_pages": 0, "html_not_thread": 0, "threads": 0,
                "posts_only": 0, "out_of_period": 0, "other_subreddit": 0, "html_comments_without_date": 0,
                "html_collapsed_branches": 0}
    problems: list[str] = []
    processed = 0
    fetched_at = now_iso()
    try:
        for f in files:
            digest = _sha256(f)
            seen = store.db.execute(
                "SELECT 1 FROM imported_files WHERE sha256=? AND subreddit=?", (digest, cfg.subreddit)
            ).fetchone()
            if seen and not force:
                counters["files_skipped"] += 1
                log(f"skip (already imported): {f}")
                continue
            is_html = f.suffix.lower() in saved_html.HTML_SUFFIXES
            if is_html:
                try:
                    thread, rep = saved_html.parse_file(f)
                except FormatError as exc:
                    counters["html_not_thread"] += 1
                    problems.append(str(exc))
                    log(f"skip (not a saved thread page): {f}: {exc}")
                    continue
                counters["html_pages"] += 1
                counters["html_comments_without_date"] += rep.comments_without_date
                counters["html_collapsed_branches"] += len(thread.stubs)
                if rep.comments_without_date:
                    problems.append(f"{f.name}: {rep.comments_without_date} comment(s) without a date were skipped")
                items, item_source = [thread], html_source
            else:
                items, item_source = iter_json_items(f), source_id
            for item in items:
                post = item.post if isinstance(item, Thread) else item
                if post.subreddit.lower() != cfg.subreddit.lower():
                    counters["other_subreddit"] += 1
                    continue
                if not cfg.period.contains(post.created_utc):
                    counters["out_of_period"] += 1
                    continue
                with store.transaction():
                    if isinstance(item, Thread):
                        total.add(store.upsert_thread(item, item_source, fetched_at, cfg.store_author_names))
                        counters["threads"] += 1
                    else:
                        total.add(store.upsert_post(item, item_source, fetched_at, cfg.store_author_names))
                        has_comments = store.db.execute(
                            "SELECT 1 FROM comments WHERE post_id=? LIMIT 1", (item.id,)
                        ).fetchone()
                        if not has_comments and item.status == "active":
                            # Comments were not part of this file: mark the tree as not loaded.
                            store.replace_stubs(item.id, [MoreStub(item.id, item.id, [], item.num_comments or 0)])
                            store.update_completeness(item.id)
                        counters["posts_only"] += 1
                processed += 1
                if _fail_after_threads is not None and processed >= _fail_after_threads:
                    raise RuntimeError("simulated crash")
            with store.transaction():
                store.db.execute(
                    "INSERT OR REPLACE INTO imported_files(sha256, subreddit, path, imported_at, run_id) "
                    "VALUES (?,?,?,?,?)",
                    (digest, cfg.subreddit, str(f), fetched_at, run_id),
                )
            counters["files_done"] += 1
            store.save_checkpoint(run_id, {"last_file": str(f)}, {**counters, **asdict(total)})
    except BaseException as exc:
        store.finish_run(run_id, "interrupted", f"{type(exc).__name__}: {exc}",
                         {**counters, **asdict(total), "gaps": problems})
        raise
    stats = {**counters, **asdict(total), "gaps": problems}
    store.finish_run(run_id, "completed", "all input files processed", stats)
    return stats
