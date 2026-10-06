"""Parser for Reddit thread pages saved manually from a browser (optional source).

The user opens a thread in their own browser, expands the branches they care
about and saves the page (Ctrl+S: "Webpage, complete" / "Webpage, HTML only"
or a single-file .mhtml). This module only reads such files; it never fetches
anything. A saved page contains only what was loaded on screen, so collapsed
branches ("N more replies", "continue this thread") are recorded as gaps.

Supported layouts:
* current www.reddit.com ("shreddit" web components: <shreddit-post>, <shreddit-comment>);
* old.reddit.com (div.thing with data-fullname).

Reddit changes its markup without notice. Parsing is tolerant: unknown or
missing attributes become empty values and are reported, never guessed.
Page text is untrusted data and is only converted to plain text.
"""

from __future__ import annotations

import email
import email.policy
import re
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from ..normalize import DELETED_MARKERS, Comment, FormatError, MoreStub, Post, Thread

HTML_SUFFIXES = (".html", ".htm", ".mhtml", ".mht")
BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table"}


class ParseReport:
    def __init__(self):
        self.comments_without_date = 0
        self.comments_without_id = 0
        self.layout = ""


# ---- file loading -----------------------------------------------------------
def read_html(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix.lower() in (".mhtml", ".mht"):
        msg = email.message_from_bytes(data, policy=email.policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_content()
        raise FormatError(f"{path.name}: no text/html part in MHTML file")
    for enc in ("utf-8", "cp1251", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ---- helpers ----------------------------------------------------------------
def html_to_text(el: Tag | None) -> str:
    """Plain text with paragraph breaks; link targets kept in parentheses."""
    if el is None:
        return ""
    out: list[str] = []

    def walk(node):
        if isinstance(node, NavigableString):
            if node.__class__.__name__ in ("Comment", "Script", "Stylesheet", "Doctype", "Declaration",
                                           "ProcessingInstruction", "CData", "TemplateString"):
                return
            out.append(str(node))
            return
        if not isinstance(node, Tag) or node.name in ("script", "style", "noscript", "svg", "button"):
            return
        if node.name == "br":
            out.append("\n")
            return
        if node.name == "li":
            out.append("\n- ")
        elif node.name in BLOCK_TAGS:
            out.append("\n")
        for child in node.children:
            walk(child)
        if node.name == "a":
            href = node.get("href") or ""
            text = node.get_text(" ", strip=True)
            if href.startswith("http") and href not in text:
                out.append(f" ({href})")
        if node.name in BLOCK_TAGS:
            out.append("\n")

    walk(el)
    text = "".join(out).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def parse_count(value: str | None) -> int | None:
    """'1.2k' -> 1200, '15' -> 15, 'Vote' / '•' -> None."""
    if value is None:
        return None
    v = value.strip().lower().replace(" ", "").replace(" ", "")
    m = re.search(r"(-?\d[\d,]*(?:\.\d+)?)\s*([km](?![a-z]))?", v)
    if not m:
        return None
    num = float(m.group(1).replace(",", "")) * {None: 1, "k": 1000, "m": 1_000_000}[m.group(2)]
    return int(num)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip()
    if re.fullmatch(r"\d{12,14}", v):  # epoch milliseconds (old.reddit data-timestamp)
        return datetime.fromtimestamp(int(v) / 1000, UTC)
    if re.fullmatch(r"\d{9,11}(\.\d+)?", v):
        return datetime.fromtimestamp(float(v), UTC)
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def full_url(href: str | None) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return re.sub(r"^https?://(old|new|np)\.reddit\.com", "https://www.reddit.com", href)
    return "https://www.reddit.com" + (href if href.startswith("/") else "/" + href)


def status_of(body: str, author: str | None, flags: set[str] = frozenset()) -> str:
    b = body.strip()
    if "removed" in flags or b in DELETED_MARKERS - {"[deleted]"}:
        return "removed"
    if "deleted" in flags or b == "[deleted]":
        return "deleted"
    return "active"


def fullname(prefix: str, value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"t[13]_[a-z0-9]+", value):
        return value
    if re.fullmatch(r"[a-z0-9]+", value):
        return f"{prefix}_{value}"
    return None


def _attr(el: Tag, *names: str) -> str | None:
    for n in names:
        v = el.get(n)
        if v not in (None, ""):
            return v if isinstance(v, str) else " ".join(v)
    return None


def _subreddit_from(value: str | None) -> str:
    if not value:
        return ""
    m = re.search(r"r/([A-Za-z0-9_]+)", value)
    return m.group(1) if m else value.strip().removeprefix("r/")


# ---- current reddit (shreddit) ----------------------------------------------
def _own(el: Tag, ancestor_name: str, node: Tag) -> bool:
    return node.find_parent(ancestor_name) is el


def _parse_shreddit(soup: BeautifulSoup, rep: ParseReport) -> Thread:
    rep.layout = "shreddit"
    pe = soup.find("shreddit-post")
    pid = fullname("t3", _attr(pe, "id", "thingid", "post-id"))
    if not pid:
        raise FormatError("post id not found on the page")
    created = parse_time(_attr(pe, "created-timestamp", "created"))
    if created is None:
        raise FormatError("post creation time not found on the page")
    subreddit = _subreddit_from(_attr(pe, "subreddit-prefixed-name", "subreddit-name"))
    title = _attr(pe, "post-title") or ""
    if not title:
        h = pe.find(attrs={"slot": "title"}) or soup.find("h1")
        title = h.get_text(" ", strip=True) if h else ""
    body_el = pe.find(attrs={"slot": "text-body"})
    selftext = html_to_text(body_el)
    author = _attr(pe, "author")
    flags = {f for f in ("deleted", "removed") if pe.has_attr(f) or pe.has_attr(f"is-{f}")}
    status = status_of(selftext, author, flags)
    if status == "active" and title in DELETED_MARKERS:
        status = "removed"
    permalink = full_url(_attr(pe, "permalink"))
    post = Post(
        id=pid, subreddit=subreddit, title=title, selftext=selftext if status == "active" else "",
        url=full_url(_attr(pe, "content-href")) or permalink, permalink=permalink, created_utc=created,
        score=parse_count(_attr(pe, "score")), num_comments=parse_count(_attr(pe, "comment-count")),
        author=None if author in (None, "[deleted]") else author, author_deleted=author == "[deleted]",
        status=status, edited=False, flair=None,
    )
    thread = Thread(post)

    for ce in soup.find_all("shreddit-comment"):
        cid = fullname("t1", _attr(ce, "thingid", "id"))
        if not cid:
            rep.comments_without_id += 1
            continue
        parent = fullname("t1", _attr(ce, "parentid", "parent-id")) or pid
        ts_el = next((t for t in ce.find_all("faceplate-timeago") if _own(ce, "shreddit-comment", t)), None)
        created_c = parse_time(_attr(ce, "created", "created-timestamp") or (ts_el.get("ts") if ts_el else None))
        if created_c is None:
            rep.comments_without_date += 1
            continue
        body_el = next((b for b in ce.find_all(attrs={"slot": "comment"}) if _own(ce, "shreddit-comment", b)), None)
        body = html_to_text(body_el)
        c_author = _attr(ce, "author")
        c_flags = {f for f in ("deleted", "removed") if ce.has_attr(f) or ce.has_attr(f"is-{f}")}
        if c_author == "[deleted]" and not body:
            c_flags.add("deleted")
        st = status_of(body, c_author, c_flags)
        depth = parse_count(_attr(ce, "depth"))
        thread.comments.append(Comment(
            id=cid, post_id=pid, parent_id=parent, subreddit=subreddit, body=body if st == "active" else "",
            permalink=full_url(_attr(ce, "permalink")), created_utc=created_c,
            score=parse_count(_attr(ce, "score")), depth=depth or 0,
            author=None if c_author in (None, "[deleted]") else c_author, author_deleted=c_author == "[deleted]",
            is_submitter=bool(post.author and c_author == post.author), status=st, edited=False,
        ))

    # Branches not loaded on the page.
    matched: list[Tag] = []
    for el in soup.find_all(["faceplate-partial", "shreddit-comment-tree-more", "a", "button"]):
        if any(m in el.parents for m in matched):
            continue
        src = el.get("src") or el.get("href") or ""
        text = el.get_text(" ", strip=True).lower()
        is_more = ("more-comments" in src or "morecomments" in src
                   or re.search(r"\d+\s+more\s+repl", text) or "continue this thread" in text
                   or el.name == "shreddit-comment-tree-more")
        if not is_more:
            continue
        holder = el.find_parent("shreddit-comment")
        parent = fullname("t1", _attr(holder, "thingid")) if holder else pid
        count = parse_count(text) or 0
        matched.append(el)
        thread.stubs.append(MoreStub(pid, parent or pid, [], count))
    return thread


# ---- old.reddit.com -------------------------------------------------------------
def _parse_old(soup: BeautifulSoup, rep: ParseReport) -> Thread:
    rep.layout = "old.reddit"
    pe = soup.select_one("div.thing.link[data-fullname^=t3_]")
    pid = pe["data-fullname"]
    created = parse_time(_attr(pe, "data-timestamp"))
    if created is None:
        t = pe.find("time")
        created = parse_time(t.get("datetime") if t else None)
    if created is None:
        raise FormatError("post creation time not found on the page")
    subreddit = _attr(pe, "data-subreddit") or ""
    title_el = pe.select_one("a.title")
    title = title_el.get_text(" ", strip=True) if title_el else ""
    body_el = pe.select_one(".expando .usertext-body .md") or pe.select_one(".usertext-body .md")
    selftext = html_to_text(body_el)
    author = _attr(pe, "data-author")
    flags = {"deleted"} if "deleted" in (pe.get("class") or []) else set()
    status = status_of(selftext, author, flags)
    permalink = full_url(_attr(pe, "data-permalink"))
    post = Post(
        id=pid, subreddit=subreddit, title=title, selftext=selftext if status == "active" else "",
        url=full_url(_attr(pe, "data-url")) or permalink, permalink=permalink, created_utc=created,
        score=parse_count(_attr(pe, "data-score")), num_comments=parse_count(_attr(pe, "data-comments-count")),
        author=None if author in (None, "[deleted]") else author, author_deleted=author == "[deleted]",
        status=status, edited=False, flair=None,
    )
    thread = Thread(post)

    for ce in soup.select("div.thing.comment"):
        cid = fullname("t1", _attr(ce, "data-fullname"))
        if not cid:
            rep.comments_without_id += 1
            continue
        entry = ce.find("div", class_="entry", recursive=False) or ce.find("div", class_="entry")
        parent_thing = ce.find_parent("div", class_="comment")
        parent = parent_thing.get("data-fullname") if parent_thing else pid
        time_el = entry.find("time") if entry else None
        created_c = parse_time(time_el.get("datetime") if time_el else None)
        if created_c is None:
            rep.comments_without_date += 1
            continue
        body_el = entry.select_one(".usertext-body .md") if entry else None
        body = html_to_text(body_el)
        c_author = _attr(ce, "data-author")
        c_flags = {"deleted"} if "deleted" in (ce.get("class") or []) else set()
        st = status_of(body, c_author, c_flags)
        score_el = entry.select_one(".score.unvoted") if entry else None
        score = parse_count(score_el.get("title") or score_el.get_text()) if score_el else None
        depth = len(ce.find_parents("div", class_="comment"))
        author_a = entry.select_one("a.author") if entry else None
        thread.comments.append(Comment(
            id=cid, post_id=pid, parent_id=parent, subreddit=subreddit, body=body if st == "active" else "",
            permalink=full_url(_attr(ce, "data-permalink")), created_utc=created_c, score=score, depth=depth,
            author=None if c_author in (None, "[deleted]") else c_author, author_deleted=c_author == "[deleted]",
            is_submitter=bool(author_a and "submitter" in (author_a.get("class") or [])),
            status=st, edited=False,
        ))

    matched: list[Tag] = []
    for el in soup.select("div.thing.morechildren, span.morecomments, span.deepthread"):
        if any(m in el.parents for m in matched):
            continue
        matched.append(el)
        a = el.find("a") or el
        onclick = a.get("onclick") or ""
        m = re.search(r"morechildren\(\s*this\s*,\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'([^']*)'", onclick)
        children = [c for c in (m.group(1).split(",") if m else []) if c]
        holder = el.find_parent("div", class_="comment")
        parent = holder.get("data-fullname") if holder else pid
        count = parse_count(a.get_text(" ", strip=True)) or len(children)
        thread.stubs.append(MoreStub(pid, parent, children, count))
    return thread


def parse_html(html: str) -> tuple[Thread, ParseReport]:
    soup = BeautifulSoup(html, "html.parser")
    rep = ParseReport()
    if soup.find("shreddit-post"):
        return _parse_shreddit(soup, rep), rep
    if soup.select_one("div.thing.link[data-fullname^=t3_]"):
        return _parse_old(soup, rep), rep
    raise FormatError("not a saved Reddit thread page (no <shreddit-post> or old.reddit post found)")


def parse_file(path: Path) -> tuple[Thread, ParseReport]:
    try:
        return parse_html(read_html(path))
    except FormatError as exc:
        raise FormatError(f"{path.name}: {exc}") from exc
