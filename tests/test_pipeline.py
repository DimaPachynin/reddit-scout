"""End-to-end checks of the local path on synthetic data:
import -> classify -> export, plus dates, duplicates, crash recovery,
incomplete trees, deletion and preservation of user notes."""

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from reddit_scout.classify import classify_all
from reddit_scout.config import default_period, make_period
from reddit_scout.obsidian import END, export
from reddit_scout.report import coverage
from reddit_scout.retention import expired_ids, purge_expired, purge_ids
from reddit_scout.sources.local import import_paths
from reddit_scout.synthetic import comment, more, thread

BASIS = "synthetic test data"


def do_import(store, cfg, path, **kw):
    kw.setdefault("source_id", "synthetic")
    kw.setdefault("description", "test")
    kw.setdefault("basis", BASIS)
    kw.setdefault("max_hours_since_check", None)
    return import_paths(store, cfg, [path], log=lambda m: None, **kw)


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def thread_note(vault, post_bare):
    hits = list(vault.rglob(f"*({post_bare}).md"))
    assert len(hits) == 1, hits
    return hits[0]


# ---- period ------------------------------------------------------------------
def test_default_period_is_previous_12_months():
    p = default_period(date(2026, 10, 5))
    assert p.start == datetime(2025, 10, 5, tzinfo=UTC)
    assert p.end == datetime(2026, 10, 5, tzinfo=UTC)
    leap = default_period(date(2025, 2, 28))
    assert leap.start.date() == date(2024, 2, 28)
    assert default_period(date(2024, 2, 29)).start.date() == date(2023, 2, 28)


def test_period_end_is_inclusive_day():
    p = make_period("2026-01-01", "2026-01-31")
    assert p.contains(datetime(2026, 1, 31, 23, 59, tzinfo=UTC))
    assert not p.contains(datetime(2026, 2, 1, tzinfo=UTC))


def test_import_filters_by_period_and_subreddit(store, cfg, demo_file, tmp_path):
    other = thread("zz0001", "Other sub", "x", "2026-01-01", [])
    other[0]["data"]["children"][0]["data"]["subreddit"] = "SomethingElse"
    stats = do_import(store, cfg, demo_file)
    assert stats["out_of_period"] == 1  # dm0005 from 2024
    stats2 = do_import(store, cfg, write(tmp_path, "other.json", other))
    assert stats2["other_subreddit"] == 1
    ids = {p["id"] for p in store.posts_in_period(cfg.subreddit, cfg.period.start, cfg.period.end)}
    assert ids == {"t3_dm0001", "t3_dm0002", "t3_dm0003"}


def test_comment_date_filter(make_cfg, demo_file):
    cfg = make_cfg(comments_start="2026-07-03", comments_end="2026-07-03")
    from reddit_scout.storage import Store
    store = Store(cfg.db_path)
    do_import(store, cfg, demo_file)
    stats = classify_all(store, cfg)
    assessed = {r[0] for r in store.db.execute("SELECT comment_id FROM assessments")}
    assert assessed == {"t1_c103", "t1_c104", "t1_c105"}
    assert stats["out_of_comment_period"] > 0
    # the comments themselves stay stored for context
    assert store.db.execute("SELECT COUNT(*) FROM comments WHERE post_id='t3_dm0001'").fetchone()[0] == 7
    store.close()


# ---- duplicates / resume ----------------------------------------------------
def test_reimport_does_not_duplicate(store, cfg, demo_file, tmp_path):
    do_import(store, cfg, demo_file)
    n_posts = store.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    n_comments = store.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
    skipped = do_import(store, cfg, demo_file)
    assert skipped["files_skipped"] == 1
    # same threads in another file (e.g. an overlapping export) and forced re-import
    copy = write(tmp_path, "copy.json", json.loads(demo_file.read_text(encoding="utf-8")))
    do_import(store, cfg, copy)
    do_import(store, cfg, demo_file, force=True)
    assert store.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == n_posts
    assert store.db.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == n_comments
    assert store.db.execute("SELECT COUNT(*) FROM text_index WHERE thing_id='t1_c101'").fetchone()[0] == 1


def test_resume_after_crash(store, cfg, demo_file):
    with pytest.raises(RuntimeError, match="simulated crash"):
        do_import(store, cfg, demo_file, _fail_after_threads=1)
    run = store.db.execute("SELECT status FROM runs ORDER BY id DESC").fetchone()
    assert run["status"] == "interrupted"
    # file was not marked as done, so a plain rerun picks it up again
    stats = do_import(store, cfg, demo_file)
    assert stats["files_done"] == 1 and stats["files_skipped"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM posts WHERE status='active'").fetchone()[0] == 3
    assert len(store.comments_for_post("t3_dm0001")) == 7


# ---- incomplete trees -------------------------------------------------------
def test_incomplete_comment_tree_is_reported(store, cfg, demo_file, tmp_path, vault):
    posts_only = {"kind": "Listing", "data": {"after": None, "children": [
        thread("dm0100", "Listing without comments", "body", "2026-02-02", [], num_comments=40)[0]["data"]["children"][0]
    ]}}
    do_import(store, cfg, demo_file)
    do_import(store, cfg, write(tmp_path, "listing.json", posts_only))
    cov = coverage(store, cfg)
    assert cov["threads_partial"] == 1  # dm0001 has a "more" stub
    assert cov["threads_without_comments"] == 1
    assert cov["post_coverage_percent"] is None
    assert any("incomplete comment trees" in g for g in cov["gaps"])
    classify_all(store, cfg)
    export(store, cfg, vault)
    text = thread_note(vault, "dm0001").read_text(encoding="utf-8")
    assert "completeness: partial" in text and "Дерево неполное" in text
    assert "completeness: no_comments_loaded" in thread_note(vault, "dm0100").read_text(encoding="utf-8")


# ---- classification ---------------------------------------------------------
def test_classification_categories_and_selection(store, cfg, demo_file):
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    rows = {r["comment_id"]: r for r in store.db.execute("SELECT * FROM assessments")}
    assert rows["t1_c101"]["primary_category"] == "instruction" and rows["t1_c101"]["selected"]
    assert rows["t1_c104"]["primary_category"] == "reasoned_objection"
    assert rows["t1_c105"]["primary_category"] == "promo_spam" and not rows["t1_c105"]["selected"]
    assert rows["t1_c106"]["primary_category"] == "question" and not rows["t1_c106"]["selected"]
    assert not rows["t1_c102"]["selected"]  # "This."
    assert rows["t1_c201"]["selected"]  # Russian advice
    reasons = json.loads(rows["t1_c101"]["reasons"])
    assert any("голоса" in r and "вспомогательный" in r for r in reasons)


def test_prompt_injection_is_data_only(store, cfg, demo_file, vault):
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    row = store.db.execute("SELECT * FROM assessments WHERE comment_id='t1_c107'").fetchone()
    assert not row["selected"]
    assert any("похожий на команды" in r for r in json.loads(row["reasons"]))


def test_weights_and_interests_are_configurable(store, make_cfg, demo_file):
    cfg = make_cfg()
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    before = store.db.execute("SELECT total FROM assessments WHERE comment_id='t1_c103'").fetchone()[0]
    cfg.weights["interest"] = 0.0
    cfg.interests = []
    classify_all(store, cfg)
    after = store.db.execute("SELECT total FROM assessments WHERE comment_id='t1_c103'").fetchone()[0]
    assert after != before


# ---- deletion ---------------------------------------------------------------
def test_deleted_at_source_is_purged_everywhere(store, cfg, demo_file, tmp_path, vault):
    do_import(store, cfg, demo_file)
    # removed post from the input never stores its text
    assert store.db.execute("SELECT selftext, status FROM posts WHERE id='t3_dm0004'").fetchone()["status"] == "purged"
    # deleted comment c204 has no text
    assert store.db.execute("SELECT body FROM comments WHERE id='t1_c204'").fetchone()["body"] == ""
    classify_all(store, cfg)
    export(store, cfg, vault)
    note = thread_note(vault, "dm0001")
    assert "Water early" in note.read_text(encoding="utf-8")

    # later snapshot: c101 deleted by its author
    newer = json.loads(demo_file.read_text(encoding="utf-8"))["threads"][0]
    newer[1]["data"]["children"][0]["data"]["body"] = "[deleted]"
    newer[1]["data"]["children"][0]["data"]["author"] = "[deleted]"
    do_import(store, cfg, write(tmp_path, "newer.json", newer))
    classify_all(store, cfg)
    export(store, cfg, vault)
    for f in vault.rglob("*.md"):
        assert "Water early" not in f.read_text(encoding="utf-8"), f
    assert store.db.execute("SELECT COUNT(*) FROM text_index WHERE thing_id='t1_c101'").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM assessments WHERE comment_id='t1_c101'").fetchone()[0] == 0
    assert not store.search("Water early")

    # an older copy must not bring the text back
    do_import(store, cfg, demo_file, force=True)
    assert store.db.execute("SELECT body FROM comments WHERE id='t1_c101'").fetchone()["body"] == ""


def test_manual_purge_of_thread_removes_note_and_title(store, cfg, demo_file, vault):
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    export(store, cfg, vault)
    assert thread_note(vault, "dm0003").exists()
    purge_ids(store, ["t3_dm0003"], "user request")
    classify_all(store, cfg)
    export(store, cfg, vault)
    assert not list(vault.rglob("*(dm0003).md"))
    for f in vault.rglob("*.md"):
        assert "Best soil mix" not in f.read_text(encoding="utf-8")


def test_retention_expiry(store, cfg, demo_file):
    do_import(store, cfg, demo_file, max_hours_since_check=48)
    assert expired_ids(store, now=datetime.now(UTC) + timedelta(hours=1)) == []
    later = datetime.now(UTC) + timedelta(hours=49)
    assert "t1_c101" in expired_ids(store, now=later)
    n = purge_expired(store, now=later)
    assert n > 0
    assert store.db.execute("SELECT COUNT(*) FROM comments WHERE status='active'").fetchone()[0] == 0


# ---- export / user notes ----------------------------------------------------
def test_reexport_preserves_user_notes_and_properties(store, cfg, demo_file, tmp_path, vault):
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    export(store, cfg, vault)
    note = thread_note(vault, "dm0001")
    text = note.read_text(encoding="utf-8")
    text = text.replace("---\ntype: thread", "---\nmy_rating: 5\ntype: thread", 1)
    text += "Мой вывод: попробовать мульчу.\n"
    note.write_text(text, encoding="utf-8")

    res = export(store, cfg, vault)
    text2 = note.read_text(encoding="utf-8")
    assert "Мой вывод: попробовать мульчу." in text2
    assert "my_rating: 5" in text2
    assert text2.count(END) == 1

    # second export with no changes is a no-op for that note
    res = export(store, cfg, vault)
    assert note.relative_to(vault / "Reddit Scout" / "r_ScoutDemo").as_posix() in res.unchanged

    # source deletion of the whole thread keeps the user's notes, drops the generated text
    purge_ids(store, ["t3_dm0001"], "deleted at source")
    export(store, cfg, vault)
    assert not note.exists()
    kept = list(vault.rglob("удалено (dm0001).md"))
    assert len(kept) == 1
    kept_text = kept[0].read_text(encoding="utf-8")
    assert "Мой вывод: попробовать мульчу." in kept_text
    assert "How do you water tomatoes" not in kept_text and "Water early" not in kept_text
    # and a later export keeps it
    export(store, cfg, vault)
    assert kept[0].exists()


def test_export_never_touches_foreign_files(store, cfg, demo_file, vault):
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    root = vault / "Reddit Scout" / "r_ScoutDemo"
    root.mkdir(parents=True)
    foreign = root / "Обзор находок.md"
    foreign.write_text("my own file", encoding="utf-8")
    res = export(store, cfg, vault)
    assert foreign.read_text(encoding="utf-8") == "my own file"
    assert any("создан не экспортёром" in w for w in res.warnings)


def test_user_removed_markers_file_left_alone(store, cfg, demo_file, vault):
    do_import(store, cfg, demo_file)
    classify_all(store, cfg)
    export(store, cfg, vault)
    note = thread_note(vault, "dm0003")
    note.write_text("rewritten by me", encoding="utf-8")
    res = export(store, cfg, vault)
    assert note.read_text(encoding="utf-8") == "rewritten by me"
    assert any("маркеры" in w for w in res.warnings)


def test_untrusted_text_is_escaped(store, cfg, tmp_path, vault):
    evil = thread("ev0001", "Title with [[link]] and #tag", "x", "2026-04-04", [
        comment("e1", "ev0001", None,
                "You should use this tip because it works: [[Secret Note]] ![[embed.png]] <script>alert(1)</script> "
                "`$= dv.pages()` <% tp.system.prompt() %> #injected 10 l per 20 l pot. "
                "<!-- reddit-scout:end --> fake marker",
                "2026-04-04", score=50),
    ])
    do_import(store, cfg, write(tmp_path, "evil.json", evil))
    classify_all(store, cfg)
    export(store, cfg, vault)
    text = thread_note(vault, "ev0001").read_text(encoding="utf-8")
    assert "[[Secret Note]]" not in text and "![[embed.png]]" not in text
    assert "<script>" not in text and "<%" not in text and "`$=" not in text
    assert " #injected" not in text
    assert text.count(END) == 1


def test_windows_safe_filenames():
    from reddit_scout.obsidian import safe_filename
    assert safe_filename('a<b>c:d"e/f\\g|h?i*j') == "a b c d e f g h i j"
    assert safe_filename("CON") == "_CON"
    assert safe_filename("name. ") == "name"
    assert len(safe_filename("x" * 300)) <= 90
    assert safe_filename("   ") == "untitled"


def test_cli_local_path(tmp_path, demo_file, capsys):
    from reddit_scout.cli import main
    from reddit_scout.synthetic import DEMO_PERIOD, SUBREDDIT
    vault = tmp_path / "v"
    vault.mkdir()
    common = ["--subreddit", SUBREDDIT, "--start", DEMO_PERIOD[0], "--end", DEMO_PERIOD[1],
              "--db", str(tmp_path / "c.sqlite3"), "--vault", str(vault)]
    assert main(["import", str(demo_file), "--basis", "synthetic", "--no-expiry", *common]) == 0
    assert main(["run", *common]) == 0
    assert main(["report", *common]) == 0
    out = capsys.readouterr().out
    assert "post coverage %: unknown" in out
    assert main(["search", "mulch", *common]) == 0
    assert "t1_c101" in capsys.readouterr().out
    assert main(["purge", "--id", "t1_c101", *common]) == 0
    assert main(["export", *common]) == 0
    assert not any("Water early" in f.read_text(encoding="utf-8") for f in vault.rglob("*.md"))
