"""Export to an Obsidian vault as plain Markdown files.

Every generated note has the layout::

    ---
    <YAML properties>          managed keys are rewritten, keys added by the user are kept
    ---
    <!-- reddit-scout:start ... -->
    generated content            rewritten on every export
    <!-- reddit-scout:end -->

    ## Мои заметки
    user content                 never touched by the exporter

Safety rules:
* Only files listed in the export manifest are ever changed or removed.
* A file whose markers were removed by the user is left untouched (warning).
* Reddit text is untrusted. It is escaped so that it cannot create links,
  embeds, tags, HTML, Templater/Dataview code or fake exporter markers.
* When source content is purged, its text disappears from the notes; a note
  with user content is kept with a removal notice, an untouched note is deleted.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from . import __version__
from .classify import CATEGORY_LABELS, CRITERIA_LABELS
from .config import Config
from .report import coverage
from .storage import Store, parse_iso

START = "<!-- reddit-scout:start — этот блок перезаписывается при экспорте; пишите ниже маркера end -->"
END = "<!-- reddit-scout:end -->"
USER_HEADER = "## Мои заметки"
USER_PLACEHOLDER = "\n\n" + USER_HEADER + "\n\n"
MANIFEST = ".reddit-scout-manifest.json"
MANAGED_KEYS = {
    "type", "reddit_id", "subreddit", "source", "source_basis", "url", "created", "fetched", "last_checked",
    "expires", "categories", "topics", "completeness", "check_status", "comments_stored", "comments_reported",
    "selected_comments", "period", "scout_generated", "scout_version", "exported", "status", "seen_in",
}
WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


# ---- text safety ------------------------------------------------------------
def safe_text(text: str) -> str:
    """Escape untrusted Reddit text for inclusion in Markdown."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\\", "\\\\")
    t = t.replace("<", "&lt;").replace(">", "&gt;")
    t = t.replace("`", "\\`").replace("[[", "\\[\\[").replace("]]", "\\]\\]")
    t = t.replace("{{", "\\{\\{").replace("%%", "\\%\\%").replace("$", "\\$")
    t = re.sub(r"(^|\s)#(?=\w)", r"\1\\#", t)
    t = re.sub(r"^(\s*)(---+|\+\+\++|===+)\s*$", r"\1\\\2", t, flags=re.MULTILINE)
    return t


def safe_inline(text: str, limit: int = 200) -> str:
    t = " ".join(text.split())
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return safe_text(t).replace("|", "\\|")


def quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in safe_text(text).split("\n"))


def link_label(text: str) -> str:
    return re.sub(r"[\[\]|#^\n\r]", " ", text).strip()[:120] or "без названия"


def safe_filename(name: str, max_len: int = 90) -> str:
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f#^\[\]]', " ", name)
    n = re.sub(r"\s+", " ", n).strip().rstrip(". ")
    if not n:
        n = "untitled"
    if n.split(".")[0].upper() in WIN_RESERVED:
        n = "_" + n
    if len(n) > max_len:
        n = n[:max_len].rstrip(". ")
    return n


def excerpt(text: str, cfg: Config) -> str | None:
    mode = cfg.obsidian.quote_mode
    if mode == "none":
        return None
    if mode == "excerpt" and len(text) > cfg.obsidian.excerpt_chars:
        cut = text[: cfg.obsidian.excerpt_chars]
        cut = cut[: cut.rfind(" ")] if " " in cut[-40:] else cut
        return cut.rstrip() + " …"
    return text


# ---- note files -------------------------------------------------------------
@dataclass
class ExportResult:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    kept_with_notice: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def split_note(text: str) -> tuple[dict, str | None, str]:
    """Return (frontmatter, generated, user_part). generated is None without markers."""
    fm: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            try:
                fm = yaml.safe_load(text[4:end]) or {}
            except yaml.YAMLError:
                fm = {}
            body = text[end + 5:]
    s, e = body.find(START), body.find(END)
    if s == -1 or e == -1 or e < s:
        return fm, None, body
    return fm, body[s + len(START): e], body[e + len(END):]


def has_user_content(user_part: str) -> bool:
    rest = user_part.replace(USER_HEADER, "", 1).strip()
    return bool(rest)


def render_note(props: dict, generated: str, existing: str | None) -> str | None:
    """Merge with an existing note. Returns None if the note must not be touched."""
    user_part = USER_PLACEHOLDER
    merged = dict(props)
    if existing is not None:
        old_fm, old_gen, old_user = split_note(existing)
        if old_gen is None:
            return None
        user_part = old_user
        for k, v in old_fm.items():
            if k not in MANAGED_KEYS:
                merged[k] = v
    fm = yaml.safe_dump(merged, allow_unicode=True, sort_keys=False, default_flow_style=False, width=1000)
    return f"---\n{fm}---\n{START}\n{generated.strip()}\n{END}{user_part}"


class Exporter:
    def __init__(self, store: Store, cfg: Config, vault: Path, now: datetime | None = None):
        self.store, self.cfg = store, cfg
        self.vault = vault
        self.root = vault / safe_filename(cfg.obsidian.folder) / safe_filename(f"r_{cfg.subreddit}")
        self.now = now or datetime.now(UTC)
        self.result = ExportResult()
        self.manifest_path = self.root / MANIFEST
        self.old_manifest: dict = {}
        if self.manifest_path.exists():
            self.old_manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.new_manifest: dict = {}
        self.sources = {s["id"]: dict(s) for s in store.sources()}

    # ---- helpers --------------------------------------------------------
    def _write(self, rel: str, props: dict, generated: str, ids: list[str]):
        path = self.root / rel
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        if existing is not None and rel not in self.old_manifest:
            self.result.warnings.append(f"{rel}: файл существует, но создан не экспортёром — не изменён")
            return
        content = render_note(props, generated, existing)
        if content is None:
            self.result.warnings.append(f"{rel}: маркеры reddit-scout удалены вручную — файл не изменён")
            self.new_manifest[rel] = {"ids": ids, "user_edited": True}
            return
        self.new_manifest[rel] = {"ids": ids}
        if existing == content:
            self.result.unchanged.append(rel)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(path)
        self.result.written.append(rel)

    def _retire(self, rel: str, reason: str):
        """A previously generated note is no longer produced: remove its generated content."""
        path = self.root / rel
        if not path.exists():
            return
        fm, gen, user = split_note(path.read_text(encoding="utf-8"))
        if gen is None:
            self.result.warnings.append(f"{rel}: маркеры удалены вручную — файл оставлен без изменений")
            return
        if not has_user_content(user):
            path.unlink()
            self.result.removed.append(rel)
            return
        keep = {k: v for k, v in fm.items() if k not in MANAGED_KEYS}
        props = {"type": "removed", "status": "purged", "scout_generated": True, "exported": self._now(), **keep}
        notice = (f"> [!warning] Материал удалён\n> {reason}. Сгенерированный текст удалён; "
                  "ваши заметки ниже сохранены.")
        stem = path.stem
        m = re.search(r"\(([a-z0-9]+)\)$", stem)
        new_rel = rel
        if m and fm.get("type") == "thread":
            new_rel = str(Path(rel).parent / f"удалено ({m.group(1)}).md").replace("\\", "/")
        fmtext = yaml.safe_dump(props, allow_unicode=True, sort_keys=False, width=1000)
        new_path = self.root / new_rel
        new_path.write_text(f"---\n{fmtext}---\n{START}\n{notice}\n{END}{user}", encoding="utf-8", newline="\n")
        if new_path != path:
            path.unlink()
        self.new_manifest[new_rel] = {"ids": [], "retired": True}
        self.result.kept_with_notice.append(new_rel)

    def link(self, rel: str, label: str | None = None) -> str:
        """Wikilink by full vault-relative path, so equal note names in other subreddits never clash."""
        target = (self.root / rel).relative_to(self.vault).as_posix()
        target = target[:-3] if target.endswith(".md") else target
        return f"[[{target}|{link_label(label or Path(rel).stem)}]]"

    def _now(self) -> str:
        return self.now.replace(microsecond=0).isoformat()

    def _expires(self, last_checked: str, source_id: str) -> str | None:
        hours = self.sources.get(source_id, {}).get("max_hours_since_check")
        if hours is None:
            return None
        return (parse_iso(last_checked) + timedelta(hours=hours)).replace(microsecond=0).isoformat()

    def _check_status(self, last_checked: str, source_id: str) -> str:
        exp = self._expires(last_checked, source_id)
        if exp is None:
            return "срок хранения не задан"
        return "срок истёк — обновите сохранение или удалите" if parse_iso(exp) <= self.now else "в пределах срока"

    @staticmethod
    def thread_rel(post) -> str:
        bare = post["id"].split("_", 1)[1]
        title = safe_filename(post["title"], 70)
        return f"Темы/{post['created_utc'][:10]} {title} ({bare}).md"

    # ---- main -----------------------------------------------------------
    def run(self) -> ExportResult:
        cfg = self.cfg
        cov = coverage(self.store, cfg)
        posts = self.store.posts_in_period(cfg.subreddit, cfg.period.start, cfg.period.end)
        selected_rows = []
        thread_info = []
        for p in posts:
            rel = self.thread_rel(p)
            sel = self._thread_note(p, rel)
            selected_rows += [(p, rel, a) for a in sel]
            thread_info.append((p, rel, len(sel)))
        collections = self._collections(selected_rows)
        self._overview(selected_rows, thread_info, collections, cov)
        self._report(cov)
        self._index(thread_info, collections, cov)

        for rel in set(self.old_manifest) - set(self.new_manifest):
            entry = self.old_manifest[rel]
            if entry.get("retired"):
                if (self.root / rel).exists():
                    self.new_manifest[rel] = entry
                continue
            self._retire(rel, "Исходный материал удалён в источнике, истёк срок хранения или вышел за период")
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(self.new_manifest, ensure_ascii=False, indent=1, sort_keys=True),
                                      encoding="utf-8")
        return self.result

    def _assessments(self, post_id: str) -> dict:
        rows = self.store.db.execute("SELECT * FROM assessments WHERE post_id=?", (post_id,)).fetchall()
        return {r["comment_id"]: r for r in rows}

    def _thread_note(self, p, rel: str) -> list:
        cfg = self.cfg
        comments = {c["id"]: c for c in self.store.comments_for_post(p["id"])}
        children = defaultdict(list)
        for c in comments.values():
            children[c["parent_id"]].append(c)
        assess = self._assessments(p["id"])
        selected = sorted((a for a in assess.values() if a["selected"]), key=lambda a: -a["total"])
        stubs = self.store.stubs_for_post(p["id"])
        n_stored = self.store.db.execute("SELECT COUNT(*) FROM comments WHERE post_id=?", (p["id"],)).fetchone()[0]
        if not stubs:
            completeness = "complete"
        elif n_stored == 0:
            completeness = "no_comments_loaded"
        else:
            completeness = "partial"
        src = self.sources.get(p["source_id"], {})
        cat_counts = defaultdict(int)
        for a in assess.values():
            cat_counts[a["primary_category"]] += 1
        cats = sorted(cat_counts, key=lambda k: -cat_counts[k])
        topics = sorted({t for a in selected for t in json.loads(a["topics"])})
        props = {
            "type": "thread",
            "reddit_id": p["id"],
            "subreddit": p["subreddit"],
            "source": p["source_id"],
            "seen_in": [x for x in p["seen_in"].split(",") if x],
            "source_basis": src.get("basis", ""),
            "url": p["permalink"],
            "created": p["created_utc"],
            "fetched": p["fetched_at"],
            "last_checked": p["last_checked_at"],
            "expires": self._expires(p["last_checked_at"], p["source_id"]),
            "categories": [CATEGORY_LABELS[c] for c in cats],
            "topics": topics,
            "completeness": completeness,
            "check_status": self._check_status(p["last_checked_at"], p["source_id"]),
            "comments_stored": n_stored,
            "comments_reported": p["num_comments"],
            "selected_comments": len(selected),
            "scout_generated": True,
            "scout_version": __version__,
        }
        out = [f"# {safe_inline(p['title'], 300)}", ""]
        out.append(f"[Оригинал на Reddit]({p['permalink']}) · создано {p['created_utc'][:10]}"
                   + (f" · флэр: {safe_inline(p['flair'], 60)}" if p["flair"] else ""))
        out += ["", "## Публикация — исходный текст", ""]
        body = excerpt(p["selftext"], cfg) if p["selftext"] else None
        if body:
            out.append(quote(body))
        elif p["selftext"]:
            out.append("_Текст не включён (quote_mode = none); см. оригинал по ссылке._")
        else:
            out.append(f"_Без текста; ссылка публикации: {safe_inline(p['url'], 200)}_")
        out += ["", f"## Полезные комментарии ({len(selected)})", ""]
        if not selected:
            out.append("_Ни один комментарий не прошёл порог отбора._")
        for a in selected:
            c = comments.get(a["comment_id"])
            if c is None:
                continue
            out += self._comment_block(c, a, comments, children, assess)
        out += ["", "## Охват темы", ""]
        out.append(f"- Комментариев сохранено: {n_stored}; Reddit указывал: {p['num_comments']} "
                   "(включает удалённые, поэтому совпадение не обязательно).")
        if stubs:
            hidden = sum(s["count"] for s in stubs)
            out.append(f"- Дерево неполное: {len(stubs)} нераскрытых ветвей «more», около {hidden} комментариев не загружено.")
        counts = defaultdict(int)
        for a in assess.values():
            counts[a["primary_category"]] += 1
        if counts:
            out.append("- Категории всех оценённых комментариев: "
                       + ", ".join(f"{CATEGORY_LABELS[k]} — {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])))
        out += ["", f"_Источник: {safe_inline(src.get('description', p['source_id']), 200)}. "
                f"Основание: {safe_inline(src.get('basis', ''), 300)}._"]
        self._write(rel, props, "\n".join(out), [p["id"]] + [a["comment_id"] for a in selected])
        return selected

    def _comment_block(self, c, a, comments, children, assess) -> list[str]:
        cfg = self.cfg
        scores = json.loads(a["scores"])
        reasons = json.loads(a["reasons"])
        out = [f"### {CATEGORY_LABELS[a['primary_category']]} — оценка {a['total']:.2f}", ""]
        meta = f"[Комментарий на Reddit]({c['permalink']}) · {c['created_utc'][:10]}"
        if c["score"] is not None:
            meta += f" · голоса: {c['score']}"
        if c["is_submitter"]:
            meta += " · автор темы"
        if cfg.obsidian.include_authors and c["author"]:
            meta += f" · u/{safe_inline(c['author'], 40)}"
        out += [meta, ""]
        out.append("**Почему включён (оценка системы):** " + "; ".join(safe_inline(r, 200) for r in reasons))
        out.append("")
        out.append("**Оценки:** " + ", ".join(f"{CRITERIA_LABELS[k]} {v:.2f}" for k, v in scores.items()))
        out.append("")
        out.append("**Пересказ системы (извлечённый, без перевода):** " + safe_inline(a["summary"], 600))
        out.append("")
        body = excerpt(c["body"], cfg)
        if body:
            out += ["**Исходный текст:**", "", quote(body), ""]
        parent = comments.get(c["parent_id"])
        if parent is not None:
            ptxt = excerpt(parent["body"], cfg)
            out.append(f"**Контекст — ответ на [комментарий]({parent['permalink']}):**"
                       + (f" «{safe_inline(ptxt, 240)}»" if ptxt else ""))
            out.append("")
        objections = [ch for ch in children.get(c["id"], [])
                      if ch["id"] in assess and assess[ch["id"]]["primary_category"] == "reasoned_objection"]
        if objections:
            out.append("**Существенные возражения в ответах:**")
            for ch in objections:
                otxt = excerpt(ch["body"], cfg)
                out.append(f"- [возражение]({ch['permalink']})" + (f": «{safe_inline(otxt, 240)}»" if otxt else ""))
            out.append("")
        return out

    def _collections(self, selected_rows) -> dict[str, str]:
        cfg = self.cfg
        groups: dict[str, list] = defaultdict(list)
        for p, rel, a in selected_rows:
            topics = json.loads(a["topics"]) or ["Прочее полезное"]
            for t in topics:
                groups[t].append((p, rel, a))
        rels = {}
        for name, rows in sorted(groups.items()):
            rows = sorted(rows, key=lambda r: -r[2]["total"])[: cfg.classifier.max_per_collection]
            rel = f"Подборки/{safe_filename(name, 60)}.md"
            rels[name] = rel
            out = [f"# Подборка: {safe_inline(name, 80)}", "",
                   f"Полезные комментарии r/{cfg.subreddit} по теме, отсортированы по оценке системы. "
                   "Голоса Reddit учитываются только как вспомогательный сигнал.", ""]
            comments_cache = {}
            for p, trel, a in rows:
                c = comments_cache.get(a["comment_id"]) or self.store.db.execute(
                    "SELECT * FROM comments WHERE id=?", (a["comment_id"],)).fetchone()
                out.append(f"## {CATEGORY_LABELS[a['primary_category']]} · {a['total']:.2f}")
                out.append("")
                out.append(f"Тема: {self.link(trel, p['title'])} · [комментарий на Reddit]({c['permalink']})")
                out.append("")
                out.append("**Пересказ системы:** " + safe_inline(a["summary"], 500))
                out.append("")
                out.append("**Почему включён:** " + "; ".join(safe_inline(r, 160) for r in json.loads(a["reasons"])[:4]))
                out.append("")
            props = {"type": "collection", "subreddit": cfg.subreddit, "topics": [name],
                     "selected_comments": len(rows), "period": cfg.period.label(), "exported": self._now(),
                     "scout_generated": True, "scout_version": __version__}
            self._write(rel, props, "\n".join(out), [a["comment_id"] for _, _, a in rows])
        return rels

    def _overview(self, selected_rows, thread_info, collections, cov):
        cfg = self.cfg
        by_cat = defaultdict(int)
        for _, _, a in selected_rows:
            by_cat[a["primary_category"]] += 1
        out = ["# Обзор основных находок", "",
               f"r/{cfg.subreddit}, период {cfg.period.label()}. Отобрано комментариев: {len(selected_rows)} "
               f"из {cov['comments']} сохранённых. {self.link('Отчёт об охвате.md')} описывает пробелы данных.", ""]
        if by_cat:
            out += ["## По категориям", ""]
            out += [f"- {CATEGORY_LABELS[k]}: {v}" for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])]
            out.append("")
        if collections:
            out += ["## Подборки", ""]
            out += [f"- {self.link(rel, name)}" for name, rel in collections.items()]
            out.append("")
        top = sorted(selected_rows, key=lambda r: -r[2]["total"])[:15]
        if top:
            out += ["## Лучшие находки", ""]
            for p, rel, a in top:
                out.append(f"- **{a['total']:.2f}** {CATEGORY_LABELS[a['primary_category']]} — "
                           f"{self.link(rel, p['title'])}: {safe_inline(a['summary'], 220)}")
            out.append("")
        busy = sorted((t for t in thread_info if t[2]), key=lambda t: -t[2])[:10]
        if busy:
            out += ["## Темы с наибольшим числом полезных комментариев", ""]
            out += [f"- {self.link(rel, p['title'])} — {n}" for p, rel, n in busy]
        props = {"type": "overview", "subreddit": cfg.subreddit, "period": cfg.period.label(),
                 "selected_comments": len(selected_rows), "exported": self._now(), "scout_generated": True,
                 "scout_version": __version__}
        self._write("Обзор находок.md", props, "\n".join(out), [])

    def _report(self, cov):
        out = ["# Отчёт об охвате", "",
               f"- Запрошенный период публикаций: {cov['requested_period']}",
               f"- Фильтр дат комментариев: {cov['comment_period'] or 'нет'}",
               f"- Фактические даты публикаций: {' — '.join(cov['actual_post_dates']) if cov['actual_post_dates'] else 'нет данных'}",
               f"- Фактические даты комментариев: {' — '.join(cov['actual_comment_dates']) if cov['actual_comment_dates'] else 'нет данных'}",
               f"- Тем: {cov['posts']} (удалено/очищено: {cov['posts_purged']})",
               f"- Комментариев: {cov['comments']} (удалено/очищено: {cov['comments_purged']}; "
               f"Reddit указывал ≈{cov['comments_reported_by_reddit']} для этих тем)",
               f"- Полных деревьев комментариев: {cov['threads_complete']}, неполных: {cov['threads_partial']}, "
               f"без комментариев: {cov['threads_without_comments']}",
               "- Процент охвата публикаций: **неизвестен** — общее число публикаций за период не известно ни одному "
               "из использованных источников.", "", "## Известные пробелы", ""]
        out += [f"- {safe_inline(g, 400)}" for g in cov["gaps"]] or ["- не зафиксированы"]
        out += ["", "## Источники и условия", ""]
        for s in cov["sources"]:
            lim = (f"текст удаляется, если не подтверждён новым импортом за {s['max_hours_since_check'] // 24} дн."
                   if s["max_hours_since_check"] is not None
                   else "автоматический срок хранения не задан; удалённое в Reddit удаляется вручную или при импорте более новой копии")
            out.append(f"- **{safe_inline(s['id'], 60)}** ({s['kind']}): {safe_inline(s['description'], 200)}. "
                       f"Основание: {safe_inline(s['basis'], 300)}. {lim}.")
        out += ["", "## Запуски и причины остановки", ""]
        out += [f"- #{r['id']} {r['kind']} — {r['status']}: {safe_inline(r['stop_reason'] or '', 300)}"
                for r in cov["runs"]]
        props = {"type": "report", "subreddit": cov["subreddit"], "period": cov["requested_period"],
                 "exported": self._now(), "scout_generated": True, "scout_version": __version__}
        self._write("Отчёт об охвате.md", props, "\n".join(out), [])

    def _index(self, thread_info, collections, cov):
        cfg = self.cfg
        out = [f"# r/{cfg.subreddit} — индекс", "",
               "Материалы для личного использования. Тексты Reddit принадлежат их авторам; "
               "удалённое в источнике и просроченное удаляется при следующем экспорте.", "",
               f"- {self.link('Обзор находок.md')}", f"- {self.link('Отчёт об охвате.md')}", ""]
        by_month = defaultdict(list)
        for p, rel, n in thread_info:
            by_month[p["created_utc"][:7]].append((p, rel, n))
        for month in sorted(by_month, reverse=True):
            out += [f"## {month}", ""]
            for p, rel, n in by_month[month]:
                out.append(f"- {self.link(rel, p['title'])}" + (f" — полезных: {n}" if n else ""))
            out.append("")
        props = {"type": "index", "subreddit": cfg.subreddit, "period": cfg.period.label(),
                 "exported": self._now(), "scout_generated": True, "scout_version": __version__}
        self._write(f"r_{safe_filename(cfg.subreddit)} — индекс.md", props, "\n".join(out), [])


def export(store: Store, cfg: Config, vault: Path, now: datetime | None = None) -> ExportResult:
    if not vault:
        raise ValueError("Obsidian vault path is not set: use [obsidian].vault_path or --vault")
    vault = Path(vault)
    if not vault.exists():
        raise ValueError(f"vault folder does not exist: {vault}")
    return Exporter(store, cfg, vault, now).run()
