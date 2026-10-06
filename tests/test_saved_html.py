"""Optional source: thread pages saved manually from a browser."""

import re
from email.message import EmailMessage

import pytest

from reddit_scout.classify import classify_all
from reddit_scout.normalize import parse_thread
from reddit_scout.obsidian import export
from reddit_scout.report import coverage
from reddit_scout.sources.local import import_paths
from reddit_scout.sources.saved_html import parse_count, parse_html
from reddit_scout.synthetic import (comment, demo_saved_pages, demo_threads, more, thread, to_old_reddit_html,
                                    to_shreddit_html)

from .test_pipeline import do_import, thread_note, write


def write_html(tmp_path, name, html):
    p = tmp_path / name
    p.write_text(html, encoding="utf-8")
    return p


def _ws(text):
    # paragraphs become blank-line separated in HTML; compare content, not spacing
    return re.sub(r"\s+", " ", text).strip()


def as_tuples(t):
    return (
        (t.post.id, t.post.subreddit, t.post.title, t.post.selftext, t.post.created_utc, t.post.num_comments),
        sorted((c.id, c.parent_id, c.depth, _ws(c.body), c.created_utc, c.score, c.status) for c in t.comments),
        sorted((s.parent_id, tuple(s.children)) for s in t.stubs),
    )


@pytest.mark.parametrize("render", [to_shreddit_html, to_old_reddit_html])
def test_saved_page_parses_like_json(render):
    payload = demo_threads()[0]  # nested replies, spam, "more" stub
    from_json = parse_thread(payload)
    from_html, rep = parse_html(render(payload))
    j, h = as_tuples(from_json), as_tuples(from_html)
    assert h[0] == j[0]
    assert h[1] == j[1]
    # shreddit pages do not expose the ids hidden behind "N more replies"
    assert [p for p, _ in h[2]] == [p for p, _ in j[2]]
    if render is to_old_reddit_html:
        assert h[2] == j[2]


def test_parse_count():
    assert parse_count("1.2k") == 1200
    assert parse_count("15 more replies") == 15
    assert parse_count("Vote") is None
    assert parse_count(None) is None


def test_html_only_pipeline(store, cfg, tmp_path, vault):
    """No JSON at all: saved pages alone are enough to run the whole pipeline."""
    pages = tmp_path / "pages"
    pages.mkdir()
    for name, html in demo_saved_pages().items():
        write_html(pages, name, html)
    stats = import_paths(store, cfg, [pages], source_id="manual", description="saved pages",
                         basis="pages I saved while reading", max_hours_since_check=None, log=lambda m: None)
    assert stats["html_pages"] == 2 and stats["threads"] == 2
    classify_all(store, cfg)
    export(store, cfg, vault)
    note = thread_note(vault, "dm0006").read_text(encoding="utf-8")
    assert "source: manual-html" in note
    assert "completeness: partial" in note  # "load more comments" on the page
    assert "Use a fine mesh" in note


def test_html_adds_detail_to_json_base(store, cfg, demo_file, tmp_path):
    do_import(store, cfg, demo_file)
    assert store.db.execute("SELECT comments_complete FROM posts WHERE id='t3_dm0001'").fetchone()[0] == 0
    n_before = store.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    pages = tmp_path / "pages"
    pages.mkdir()
    write_html(pages, "a.html", demo_saved_pages()["2026-07-05 balcony tomatoes - expanded.html"])
    do_import(store, cfg, pages)
    # the two hidden comments arrived, nothing duplicated, the "more" branch is resolved
    assert store.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == n_before + 2
    assert store.db.execute("SELECT comments_complete FROM posts WHERE id='t3_dm0001'").fetchone()[0] == 1
    post = store.db.execute("SELECT * FROM posts WHERE id='t3_dm0001'").fetchone()
    assert post["source_id"] == "synthetic" and post["seen_in"] == "synthetic,synthetic-html"
    assert post["score"] == 152  # newer value from the page
    assert post["flair"] == "Watering"  # not on the page: kept from JSON


def test_missing_values_in_page_do_not_erase_known_ones(store, cfg, demo_file, tmp_path):
    do_import(store, cfg, demo_file)
    html = to_shreddit_html(demo_threads()[0]).replace('score="87"', 'score="Vote"')
    do_import(store, cfg, write_html(tmp_path, "p.html", html))
    assert store.db.execute("SELECT score FROM comments WHERE id='t1_c101'").fetchone()[0] == 87


def test_newer_saved_page_with_deleted_comment_purges_it(store, cfg, demo_file, tmp_path, vault):
    do_import(store, cfg, demo_file)
    payload = demo_threads()[0]
    payload[1]["data"]["children"][0]["data"]["body"] = "[deleted]"
    payload[1]["data"]["children"][0]["data"]["author"] = "[deleted]"
    do_import(store, cfg, write_html(tmp_path, "newer.html", to_old_reddit_html(payload)))
    row = store.db.execute("SELECT body, status FROM comments WHERE id='t1_c101'").fetchone()
    assert row["body"] == "" and row["status"] == "purged"
    classify_all(store, cfg)
    export(store, cfg, vault)
    assert not any("Water early" in f.read_text(encoding="utf-8") for f in vault.rglob("*.md"))


def test_non_thread_pages_and_asset_folders_are_skipped(store, cfg, tmp_path):
    pages = tmp_path / "pages"
    (pages / "thread_files").mkdir(parents=True)
    write_html(pages, "thread.html", to_shreddit_html(demo_threads()[2]))
    write_html(pages / "thread_files", "frame.html", to_shreddit_html(demo_threads()[1]))  # asset folder: ignored
    write_html(pages, "front-page.html", "<html><body><h1>r/ScoutDemo</h1></body></html>")
    stats = do_import(store, cfg, pages)
    assert stats["html_pages"] == 1 and stats["html_not_thread"] == 1
    assert {p["id"] for p in store.posts_in_period(cfg.subreddit, cfg.period.start, cfg.period.end)} == {"t3_dm0003"}
    cov = coverage(store, cfg)
    assert any("not a saved Reddit thread page" in g for g in cov["gaps"])


def test_mhtml_single_file(store, cfg, tmp_path):
    msg = EmailMessage()
    msg["Subject"] = "saved page"
    msg.set_content(to_shreddit_html(demo_threads()[2]), subtype="html", cte="quoted-printable")
    p = tmp_path / "thread.mhtml"
    p.write_bytes(bytes(msg))
    stats = do_import(store, cfg, p)
    assert stats["threads"] == 1
    assert len(store.comments_for_post("t3_dm0003")) == 3


def test_page_markup_is_not_executed_or_kept(store, cfg, tmp_path, vault):
    t = thread("hx0001", "Markup test", "x", "2026-02-02", [
        comment("h1", "hx0001", None, "You should try this tip, because it works: 2 l per pot.", "2026-02-02", score=5),
    ])
    html = to_shreddit_html(t).replace(
        "<p>You should", '<script>alert(1)</script><img src=x onerror="alert(2)"><p>You should')
    do_import(store, cfg, write_html(tmp_path, "x.html", html))
    body = store.db.execute("SELECT body FROM comments WHERE id='t1_h1'").fetchone()["body"]
    assert "alert" not in body and body.startswith("You should")


def test_example_pages_match_generator():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "examples" / "saved-pages"
    for name, html in demo_saved_pages().items():
        assert (root / name).read_text(encoding="utf-8") == html
