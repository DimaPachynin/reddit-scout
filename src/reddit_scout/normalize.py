"""Convert Reddit API JSON ("things") into normalized records.

Accepted input shapes (same as the official Data API responses):

* thread: ``[post_listing, comment_listing]`` as returned by ``/comments/{id}``
* listing of posts: ``{"kind": "Listing", "data": {"children": [t3, ...]}}``
* ``/api/morechildren`` result: ``{"json": {"data": {"things": [...]}}}``

All text from Reddit is untrusted data. This module never interprets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

DELETED_MARKERS = {"[deleted]", "[removed]", "[ Removed by Reddit ]", "[removed by reddit]"}


class FormatError(ValueError):
    pass


@dataclass
class Post:
    id: str  # fullname, e.g. t3_abc123
    subreddit: str
    title: str
    selftext: str
    url: str
    permalink: str
    created_utc: datetime
    score: int | None
    num_comments: int | None
    author: str | None
    author_deleted: bool
    status: str  # active | deleted | removed
    edited: bool
    flair: str | None


@dataclass
class Comment:
    id: str  # t1_...
    post_id: str  # t3_...
    parent_id: str  # t1_... or t3_...
    subreddit: str
    body: str
    permalink: str
    created_utc: datetime
    score: int | None
    depth: int
    author: str | None
    author_deleted: bool
    is_submitter: bool
    status: str
    edited: bool


@dataclass
class MoreStub:
    post_id: str
    parent_id: str
    children: list[str]  # bare ids (without t1_)
    count: int


@dataclass
class Thread:
    post: Post
    comments: list[Comment] = field(default_factory=list)
    stubs: list[MoreStub] = field(default_factory=list)


def _ts(value) -> datetime:
    if value is None:
        raise FormatError("created_utc missing")
    return datetime.fromtimestamp(float(value), UTC)


def _fullname(prefix: str, data: dict) -> str:
    name = data.get("name")
    if name:
        return name
    if data.get("id"):
        return f"{prefix}_{data['id']}"
    raise FormatError("thing without id/name")


def _text_status(text: str | None, removed_by_category=None) -> str:
    t = (text or "").strip()
    if removed_by_category:
        return "removed"
    if t == "[deleted]":
        return "deleted"
    if t in DELETED_MARKERS:
        return "removed"
    return "active"


def _permalink(p: str | None) -> str:
    if not p:
        return ""
    return p if p.startswith("http") else f"https://www.reddit.com{p}"


def parse_post(thing: dict) -> Post:
    if thing.get("kind") != "t3":
        raise FormatError(f"expected t3, got {thing.get('kind')!r}")
    d = thing["data"]
    selftext = d.get("selftext") or ""
    status = _text_status(selftext, d.get("removed_by_category"))
    if status == "active" and d.get("title") in DELETED_MARKERS:
        status = "removed"
    author = d.get("author")
    return Post(
        id=_fullname("t3", d),
        subreddit=d.get("subreddit") or "",
        title=d.get("title") or "",
        selftext="" if status != "active" else selftext,
        url=d.get("url") or "",
        permalink=_permalink(d.get("permalink")),
        created_utc=_ts(d.get("created_utc")),
        score=d.get("score"),
        num_comments=d.get("num_comments"),
        author=author if author not in (None, "[deleted]") else None,
        author_deleted=author == "[deleted]",
        status=status,
        edited=bool(d.get("edited")),
        flair=d.get("link_flair_text"),
    )


def parse_comment(thing: dict, post_id: str | None = None) -> Comment:
    d = thing["data"]
    body = d.get("body") or ""
    status = _text_status(body)
    author = d.get("author")
    link_id = d.get("link_id") or post_id
    if not link_id:
        raise FormatError("comment without link_id")
    return Comment(
        id=_fullname("t1", d),
        post_id=link_id,
        parent_id=d.get("parent_id") or link_id,
        subreddit=d.get("subreddit") or "",
        body="" if status != "active" else body,
        permalink=_permalink(d.get("permalink")),
        created_utc=_ts(d.get("created_utc")),
        score=d.get("score"),
        depth=int(d.get("depth") or 0),
        author=author if author not in (None, "[deleted]") else None,
        author_deleted=author == "[deleted]",
        is_submitter=bool(d.get("is_submitter")),
        status=status,
        edited=bool(d.get("edited")),
    )


def _walk(things: list, post_id: str, comments: list[Comment], stubs: list[MoreStub], depth: int = 0):
    for thing in things:
        kind = thing.get("kind")
        d = thing.get("data", {})
        if kind == "t1":
            c = parse_comment(thing, post_id)
            if "depth" not in d:
                c.depth = depth
            comments.append(c)
            replies = d.get("replies")
            if isinstance(replies, dict):
                _walk(replies.get("data", {}).get("children", []), post_id, comments, stubs, depth + 1)
        elif kind == "more":
            children = list(d.get("children") or [])
            # A "more" with no children and id "_" is a "continue this thread" link;
            # it still marks the tree as incomplete.
            stubs.append(
                MoreStub(
                    post_id=post_id,
                    parent_id=d.get("parent_id") or post_id,
                    children=children,
                    count=int(d.get("count") or len(children)),
                )
            )
        else:
            raise FormatError(f"unexpected thing kind in comment tree: {kind!r}")


def parse_thread(payload) -> Thread:
    if not (isinstance(payload, list) and len(payload) == 2):
        raise FormatError("thread payload must be [post_listing, comment_listing]")
    post_children = payload[0].get("data", {}).get("children", [])
    if len(post_children) != 1:
        raise FormatError("thread payload must contain exactly one post")
    post = parse_post(post_children[0])
    thread = Thread(post)
    _walk(payload[1].get("data", {}).get("children", []), post.id, thread.comments, thread.stubs)
    return thread


def parse_post_listing(payload: dict) -> tuple[list[Post], str | None]:
    if payload.get("kind") != "Listing":
        raise FormatError("expected Listing")
    data = payload.get("data", {})
    posts = [parse_post(t) for t in data.get("children", []) if t.get("kind") == "t3"]
    return posts, data.get("after")


def parse_morechildren(payload: dict, post_id: str) -> tuple[list[Comment], list[MoreStub]]:
    things = payload.get("json", {}).get("data", {}).get("things", [])
    comments: list[Comment] = []
    stubs: list[MoreStub] = []
    for thing in things:
        if thing.get("kind") == "t1":
            comments.append(parse_comment(thing, post_id))
        elif thing.get("kind") == "more":
            d = thing.get("data", {})
            stubs.append(
                MoreStub(post_id, d.get("parent_id") or post_id, list(d.get("children") or []),
                         int(d.get("count") or 0))
            )
    return comments, stubs
