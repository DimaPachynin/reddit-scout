"""SQLite storage: normalized posts/comments, provenance, runs, assessments.

Design rules:
* Reddit fullnames (t3_/t1_) are primary keys, so repeated imports never duplicate.
* Content deleted at the source is purged: text is blanked, derived data
  (assessments, full-text index) is removed, and a tombstone prevents an older
  copy of the same item from being re-imported later.
* The deletion log keeps only ids and reasons, never text.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .normalize import Comment, MoreStub, Post, Thread

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- local_file | saved_html | synthetic
    description TEXT NOT NULL,
    basis TEXT NOT NULL,             -- declared basis for use (terms, licence, own data)
    terms_url TEXT,
    max_hours_since_check INTEGER,   -- retention rule; NULL = no automatic expiry
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    title TEXT NOT NULL,
    selftext TEXT NOT NULL,
    url TEXT NOT NULL,
    permalink TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    score INTEGER,
    num_comments INTEGER,
    author TEXT,
    author_deleted INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,            -- active | deleted | removed | purged
    edited INTEGER NOT NULL DEFAULT 0,
    flair TEXT,
    source_id TEXT NOT NULL REFERENCES sources(id),  -- first source the item came from
    seen_in TEXT NOT NULL DEFAULT '', -- all sources that contained the item, comma-separated
    fetched_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,   -- last import that contained the item
    comments_complete INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS posts_sub_created ON posts(subreddit, created_utc);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    body TEXT NOT NULL,
    permalink TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    score INTEGER,
    depth INTEGER NOT NULL DEFAULT 0,
    author TEXT,
    author_deleted INTEGER NOT NULL DEFAULT 0,
    is_submitter INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0,
    source_id TEXT NOT NULL REFERENCES sources(id),
    seen_in TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS comments_parent ON comments(parent_id);

CREATE TABLE IF NOT EXISTS more_stubs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    children TEXT NOT NULL,          -- JSON list of bare ids
    count INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | resolved | abandoned
    reason TEXT
);
CREATE INDEX IF NOT EXISTS more_stubs_post ON more_stubs(post_id);

CREATE TABLE IF NOT EXISTS tombstones (
    thing_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    purged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    params TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,            -- running | completed | failed | denied | interrupted
    stop_reason TEXT,
    checkpoint TEXT,
    stats TEXT
);

CREATE TABLE IF NOT EXISTS imported_files (
    sha256 TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    path TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    run_id INTEGER,
    PRIMARY KEY (sha256, subreddit)
);

CREATE TABLE IF NOT EXISTS assessments (
    comment_id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    primary_category TEXT NOT NULL,
    categories TEXT NOT NULL,
    scores TEXT NOT NULL,
    total REAL NOT NULL,
    selected INTEGER NOT NULL,
    reasons TEXT NOT NULL,
    summary TEXT NOT NULL,
    topics TEXT NOT NULL,
    classifier TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    assessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assessments_post ON assessments(post_id);

CREATE VIRTUAL TABLE IF NOT EXISTS text_index USING fts5(thing_id UNINDEXED, post_id UNINDEXED, content);
"""


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class UpsertStats:
    posts_new: int = 0
    posts_updated: int = 0
    comments_new: int = 0
    comments_updated: int = 0
    purged: int = 0
    skipped_tombstoned: int = 0

    def add(self, other: "UpsertStats"):
        for k in vars(self):
            setattr(self, k, getattr(self, k) + getattr(other, k))


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self.db.commit()

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    # ---- sources -------------------------------------------------------
    def register_source(
        self,
        source_id: str,
        kind: str,
        description: str,
        basis: str,
        terms_url: str | None,
        max_hours_since_check: int | None,
    ):
        self.db.execute(
            """INSERT INTO sources(id, kind, description, basis, terms_url, max_hours_since_check, registered_at)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, description=excluded.description,
                   basis=excluded.basis, terms_url=excluded.terms_url,
                   max_hours_since_check=excluded.max_hours_since_check""",
            (source_id, kind, description, basis, terms_url, max_hours_since_check, now_iso()),
        )
        self.db.commit()

    def sources(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM sources ORDER BY id").fetchall()

    # ---- tombstones / purge ---------------------------------------------
    def is_tombstoned(self, thing_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM tombstones WHERE thing_id=?", (thing_id,)).fetchone() is not None

    def purge(self, thing_id: str, reason: str, *, commit: bool = True) -> bool:
        """Remove the text of a post/comment and everything derived from it.

        Purging a post also purges all its comments (they lose their context
        and the thread note is removed from the export)."""
        purged = False
        if thing_id.startswith("t3_"):
            cur = self.db.execute(
                "UPDATE posts SET title='', selftext='', url='', author=NULL, status='purged' WHERE id=?",
                (thing_id,),
            )
            purged = cur.rowcount > 0
            for (cid,) in self.db.execute("SELECT id FROM comments WHERE post_id=?", (thing_id,)).fetchall():
                self._purge_comment(cid, f"parent post purged: {reason}")
            self.db.execute("DELETE FROM more_stubs WHERE post_id=?", (thing_id,))
        else:
            purged = self._purge_comment(thing_id, reason)
        self.db.execute("DELETE FROM text_index WHERE thing_id=?", (thing_id,))
        self.db.execute(
            "INSERT OR REPLACE INTO tombstones(thing_id, reason, purged_at) VALUES (?,?,?)",
            (thing_id, reason, now_iso()),
        )
        if commit:
            self.db.commit()
        return purged

    def _purge_comment(self, comment_id: str, reason: str) -> bool:
        cur = self.db.execute(
            "UPDATE comments SET body='', author=NULL, status='purged' WHERE id=?", (comment_id,)
        )
        self.db.execute("DELETE FROM assessments WHERE comment_id=?", (comment_id,))
        self.db.execute("DELETE FROM text_index WHERE thing_id=?", (comment_id,))
        self.db.execute(
            "INSERT OR REPLACE INTO tombstones(thing_id, reason, purged_at) VALUES (?,?,?)",
            (comment_id, reason, now_iso()),
        )
        return cur.rowcount > 0

    # ---- upsert ----------------------------------------------------------
    def upsert_post(self, p: Post, source_id: str, fetched_at: str, store_authors: bool) -> UpsertStats:
        st = UpsertStats()
        if self.is_tombstoned(p.id):
            st.skipped_tombstoned += 1
            return st
        existing = self.db.execute("SELECT status FROM posts WHERE id=?", (p.id,)).fetchone()
        author = p.author if store_authors else None
        if existing is None:
            self.db.execute(
                """INSERT INTO posts(id, subreddit, title, selftext, url, permalink, created_utc, score,
                    num_comments, author, author_deleted, status, edited, flair, source_id, seen_in, fetched_at,
                    last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (p.id, p.subreddit, p.title, p.selftext, p.url, p.permalink, iso(p.created_utc), p.score,
                 p.num_comments, author, int(p.author_deleted), p.status, int(p.edited), p.flair, source_id,
                 source_id, fetched_at, fetched_at),
            )
            st.posts_new += 1
        else:
            # Sources differ in detail (a saved page may lack the score or the text of a
            # link post): a missing value never overwrites a known one.
            self.db.execute(
                """UPDATE posts SET title=COALESCE(NULLIF(?, ''), title), selftext=COALESCE(NULLIF(?, ''), selftext),
                    url=COALESCE(NULLIF(?, ''), url), permalink=COALESCE(NULLIF(?, ''), permalink),
                    score=COALESCE(?, score), num_comments=COALESCE(?, num_comments), author=COALESCE(?, author),
                    author_deleted=?, status=?, edited=MAX(edited, ?), flair=COALESCE(?, flair),
                    seen_in=CASE WHEN instr(',' || seen_in || ',', ',' || ? || ',') THEN seen_in
                                 ELSE seen_in || ',' || ? END,
                    last_checked_at=MAX(last_checked_at, ?) WHERE id=?""",
                (p.title, p.selftext, p.url, p.permalink, p.score, p.num_comments, author, int(p.author_deleted),
                 p.status, int(p.edited), p.flair, source_id, source_id, fetched_at, p.id),
            )
            st.posts_updated += 1
        if p.status != "active":
            # Deleted/removed at the source: keep only the id-level record.
            self.purge(p.id, f"post {p.status} at source", commit=False)
            st.purged += 1
        else:
            self.db.execute("DELETE FROM text_index WHERE thing_id=?", (p.id,))
            self.db.execute(
                "INSERT INTO text_index(thing_id, post_id, content) VALUES (?,?,?)",
                (p.id, p.id, f"{p.title}\n{p.selftext}"),
            )
        return st

    def upsert_comment(self, c: Comment, source_id: str, fetched_at: str, store_authors: bool) -> UpsertStats:
        st = UpsertStats()
        if self.is_tombstoned(c.id):
            st.skipped_tombstoned += 1
            return st
        existing = self.db.execute("SELECT body FROM comments WHERE id=?", (c.id,)).fetchone()
        author = c.author if store_authors else None
        if existing is None:
            self.db.execute(
                """INSERT INTO comments(id, post_id, parent_id, subreddit, body, permalink, created_utc, score,
                    depth, author, author_deleted, is_submitter, status, edited, source_id, seen_in, fetched_at,
                    last_checked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (c.id, c.post_id, c.parent_id, c.subreddit, c.body, c.permalink, iso(c.created_utc), c.score,
                 c.depth, author, int(c.author_deleted), int(c.is_submitter), c.status, int(c.edited), source_id,
                 source_id, fetched_at, fetched_at),
            )
            st.comments_new += 1
        else:
            self.db.execute(
                """UPDATE comments SET body=?, permalink=COALESCE(NULLIF(?, ''), permalink),
                    score=COALESCE(?, score), depth=?, author=COALESCE(?, author), author_deleted=?,
                    is_submitter=MAX(is_submitter, ?), status=?, edited=MAX(edited, ?),
                    seen_in=CASE WHEN instr(',' || seen_in || ',', ',' || ? || ',') THEN seen_in
                                 ELSE seen_in || ',' || ? END,
                    last_checked_at=MAX(last_checked_at, ?) WHERE id=?""",
                (c.body, c.permalink, c.score, c.depth, author, int(c.author_deleted), int(c.is_submitter),
                 c.status, int(c.edited), source_id, source_id, fetched_at, c.id),
            )
            if existing["body"] != c.body:
                # Text changed (edit): the old assessment no longer describes it.
                self.db.execute("DELETE FROM assessments WHERE comment_id=?", (c.id,))
            st.comments_updated += 1
        if c.status != "active":
            self.purge(c.id, f"comment {c.status} at source", commit=False)
            st.purged += 1
        else:
            self.db.execute("DELETE FROM text_index WHERE thing_id=?", (c.id,))
            self.db.execute(
                "INSERT INTO text_index(thing_id, post_id, content) VALUES (?,?,?)", (c.id, c.post_id, c.body)
            )
        return st

    def replace_stubs(self, post_id: str, stubs: list[MoreStub]):
        self.db.execute("DELETE FROM more_stubs WHERE post_id=?", (post_id,))
        self.add_stubs(stubs)

    def add_stubs(self, stubs: list[MoreStub]):
        for s in stubs:
            self.db.execute(
                "INSERT INTO more_stubs(post_id, parent_id, children, count) VALUES (?,?,?,?)",
                (s.post_id, s.parent_id, json.dumps(s.children), s.count),
            )

    def upsert_thread(self, t: Thread, source_id: str, fetched_at: str, store_authors: bool) -> UpsertStats:
        st = UpsertStats()
        st.add(self.upsert_post(t.post, source_id, fetched_at, store_authors))
        if self.is_tombstoned(t.post.id) or t.post.status != "active":
            return st
        for c in t.comments:
            st.add(self.upsert_comment(c, source_id, fetched_at, store_authors))
        self.replace_stubs(t.post.id, t.stubs)
        self.update_completeness(t.post.id)
        return st

    def update_completeness(self, post_id: str):
        # A "more" stub is resolved once its comments are stored, whichever source
        # supplied them (e.g. a fuller saved page or an earlier file).
        for stub in self.db.execute(
            "SELECT * FROM more_stubs WHERE post_id=? AND status!='resolved'", (post_id,)
        ).fetchall():
            children = json.loads(stub["children"])
            if children:
                marks = ",".join("?" * len(children))
                have = self.db.execute(
                    f"SELECT COUNT(*) FROM comments WHERE id IN ({marks})", [f"t1_{c}" for c in children]
                ).fetchone()[0]
                done = have >= len(children)
            else:
                column = "post_id" if stub["parent_id"] == post_id else "parent_id"
                have = self.db.execute(
                    f"SELECT COUNT(*) FROM comments WHERE {column}=?", (stub["parent_id"],)
                ).fetchone()[0]
                done = stub["count"] > 0 and have >= stub["count"]
            if done:
                self.db.execute("UPDATE more_stubs SET status='resolved' WHERE id=?", (stub["id"],))
        pending = self.db.execute(
            "SELECT COUNT(*) FROM more_stubs WHERE post_id=? AND status!='resolved'", (post_id,)
        ).fetchone()[0]
        self.db.execute("UPDATE posts SET comments_complete=? WHERE id=?", (int(pending == 0), post_id))

    def mark_checked(self, thing_id: str, checked_at: str):
        table = "posts" if thing_id.startswith("t3_") else "comments"
        self.db.execute(f"UPDATE {table} SET last_checked_at=? WHERE id=?", (checked_at, thing_id))

    # ---- runs ------------------------------------------------------------
    def start_run(self, kind: str, subreddit: str, params: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(kind, subreddit, params, started_at, status) VALUES (?,?,?,?, 'running')",
            (kind, subreddit, json.dumps(params, sort_keys=True), now_iso()),
        )
        self.db.commit()
        return cur.lastrowid

    def find_resumable_run(self, kind: str, subreddit: str, params: dict) -> sqlite3.Row | None:
        return self.db.execute(
            """SELECT * FROM runs WHERE kind=? AND subreddit=? AND params=?
               AND status IN ('running', 'interrupted', 'failed') ORDER BY id DESC LIMIT 1""",
            (kind, subreddit, json.dumps(params, sort_keys=True)),
        ).fetchone()

    def save_checkpoint(self, run_id: int, checkpoint: dict, stats: dict | None = None):
        self.db.execute(
            "UPDATE runs SET checkpoint=?, stats=COALESCE(?, stats) WHERE id=?",
            (json.dumps(checkpoint), json.dumps(stats) if stats is not None else None, run_id),
        )
        self.db.commit()

    def finish_run(self, run_id: int, status: str, stop_reason: str, stats: dict | None = None):
        self.db.execute(
            "UPDATE runs SET status=?, stop_reason=?, finished_at=?, stats=COALESCE(?, stats) WHERE id=?",
            (status, stop_reason, now_iso(), json.dumps(stats) if stats is not None else None, run_id),
        )
        self.db.commit()

    def runs(self, subreddit: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM runs WHERE subreddit=? ORDER BY id", (subreddit,)).fetchall()

    # ---- queries ---------------------------------------------------------
    def posts_in_period(self, subreddit: str, start: datetime, end: datetime, include_purged=False):
        sql = "SELECT * FROM posts WHERE lower(subreddit)=lower(?) AND created_utc>=? AND created_utc<?"
        if not include_purged:
            sql += " AND status='active'"
        return self.db.execute(sql + " ORDER BY created_utc", (subreddit, iso(start), iso(end))).fetchall()

    def comments_for_post(self, post_id: str, active_only=True):
        sql = "SELECT * FROM comments WHERE post_id=?"
        if active_only:
            sql += " AND status='active'"
        return self.db.execute(sql + " ORDER BY created_utc", (post_id,)).fetchall()

    def stubs_for_post(self, post_id: str, pending_only=True):
        sql = "SELECT * FROM more_stubs WHERE post_id=?"
        if pending_only:
            sql += " AND status!='resolved'"
        return self.db.execute(sql, (post_id,)).fetchall()

    def search(self, query: str, limit: int = 20):
        # Quote every term: user input is matched literally, never parsed as FTS syntax.
        query = " ".join('"' + w.replace('"', '""') + '"' for w in query.split())
        if not query:
            return []
        return self.db.execute(
            "SELECT thing_id, post_id, snippet(text_index, 2, '«', '»', '…', 12) AS snip FROM text_index "
            "WHERE text_index MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
