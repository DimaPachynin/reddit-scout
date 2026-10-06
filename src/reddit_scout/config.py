"""Configuration loading.

Config lives in a TOML file (``config.toml`` by default, ignored by Git).
The project uses no network source; all input comes from local files.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

DEFAULT_WEIGHTS = {
    "applicability": 0.30,
    "specificity": 0.20,
    "evidence": 0.20,
    "originality": 0.10,
    "interest": 0.20,
    # Reddit votes are an auxiliary signal only; keep this weight small.
    "votes": 0.05,
}


class ConfigError(ValueError):
    pass


@dataclass
class Interest:
    name: str
    keywords: list[str]
    weight: float = 1.0


@dataclass
class Period:
    start: datetime  # inclusive, UTC
    end: datetime  # exclusive, UTC

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    def label(self) -> str:
        return f"{self.start.date().isoformat()} — {(self.end - timedelta(seconds=1)).date().isoformat()}"


@dataclass
class RetentionConfig:
    # Content not confirmed by a newer import within this many days is purged by
    # ``reddit-scout purge --expired`` (and by ``run``). 0 = no automatic expiry.
    # Without an API there is no way to learn about deletions on Reddit except
    # a newer saved copy of the thread, so this is a user decision.
    max_days_since_check: int = 0


@dataclass
class ClassifierConfig:
    backend: str = "rules"  # only "rules" is implemented; external backends are opt-in
    select_threshold: float = 0.45
    min_chars: int = 40
    max_per_thread: int = 8
    max_per_collection: int = 40


@dataclass
class ObsidianConfig:
    vault_path: str = ""
    folder: str = "Reddit Scout"
    include_authors: bool = False
    # How much original Reddit text goes into notes: "full", "excerpt" or "none"
    # ("none" keeps only the link, the system summary and the assessment).
    quote_mode: str = "excerpt"
    excerpt_chars: int = 400


@dataclass
class Config:
    subreddit: str
    db_path: Path
    period: Period
    comment_period: Period | None
    retention: RetentionConfig
    classifier: ClassifierConfig
    obsidian: ObsidianConfig
    interests: list[Interest]
    weights: dict[str, float]
    store_author_names: bool = False
    base_dir: Path = Path(".")

    def classifier_fingerprint(self) -> str:
        payload = {
            "interests": [vars(i) for i in self.interests],
            "weights": self.weights,
            "classifier": vars(self.classifier),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _parse_day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{name}: expected YYYY-MM-DD, got {value!r}") from exc


def _minus_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 - months, 12)
    year, month = d.year + y, m + 1
    day = d.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1  # e.g. 29 Feb -> 28 Feb


def default_period(today: date | None = None) -> Period:
    """Previous 12 months: [today - 12 months, today) in UTC."""
    today = today or datetime.now(UTC).date()
    start = _minus_months(today, 12)
    return Period(
        datetime.combine(start, time.min, UTC),
        datetime.combine(today, time.min, UTC),
    )


def make_period(start: str | None, end: str | None, today: date | None = None) -> Period:
    """Build a period. ``end`` is inclusive as a calendar day."""
    if not start and not end:
        return default_period(today)
    default = default_period(today)
    s = datetime.combine(_parse_day(start, "start"), time.min, UTC) if start else default.start
    e = (
        datetime.combine(_parse_day(end, "end") + timedelta(days=1), time.min, UTC)
        if end
        else default.end
    )
    if s >= e:
        raise ConfigError(f"period start {s.date()} must be before end {e.date()}")
    return Period(s, e)


def normalize_subreddit(name: str) -> str:
    name = name.strip()
    for prefix in ("https://www.reddit.com/r/", "https://reddit.com/r/", "/r/", "r/"):
        if name.lower().startswith(prefix):
            name = name[len(prefix):]
    name = name.strip("/")
    if not name or not all(c.isalnum() or c == "_" for c in name) or len(name) > 21:
        raise ConfigError(f"invalid subreddit name: {name!r}")
    return name


def _section(raw: dict, key: str) -> dict:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _fill(cls, values: dict, section: str):
    known = cls.__dataclass_fields__.keys()
    unknown = set(values) - set(known)
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    return cls(**values)


def load_config(
    path: str | Path | None = None,
    *,
    overrides: dict | None = None,
    today: date | None = None,
) -> Config:
    raw: dict = {}
    base_dir = Path(".")
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config file not found: {p}")
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
        base_dir = p.parent
    overrides = overrides or {}

    project = _section(raw, "project")
    subreddit = overrides.get("subreddit") or project.get("subreddit") or ""
    if not subreddit:
        raise ConfigError("subreddit is not set: use [project].subreddit or --subreddit")
    subreddit = normalize_subreddit(subreddit)

    db_path = Path(overrides.get("db_path") or project.get("db_path") or "data/scout.sqlite3")
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    period_raw = _section(raw, "period")
    period = make_period(
        overrides.get("start") or period_raw.get("start") or None,
        overrides.get("end") or period_raw.get("end") or None,
        today,
    )
    comments_raw = _section(raw, "comments")
    c_start = overrides.get("comments_start") or comments_raw.get("start") or None
    c_end = overrides.get("comments_end") or comments_raw.get("end") or None
    comment_period = None
    if c_start or c_end:
        comment_period = Period(
            datetime.combine(_parse_day(c_start, "comments.start"), time.min, UTC)
            if c_start
            else datetime.min.replace(tzinfo=UTC),
            datetime.combine(_parse_day(c_end, "comments.end") + timedelta(days=1), time.min, UTC)
            if c_end
            else datetime.max.replace(tzinfo=UTC),
        )

    retention = _fill(RetentionConfig, _section(raw, "retention"), "retention")
    classifier = _fill(ClassifierConfig, _section(raw, "classifier"), "classifier")
    obsidian_raw = dict(_section(raw, "obsidian"))
    if overrides.get("vault_path"):
        obsidian_raw["vault_path"] = overrides["vault_path"]
    obsidian = _fill(ObsidianConfig, obsidian_raw, "obsidian")

    interests = []
    for item in raw.get("interests", []):
        if not item.get("name") or not item.get("keywords"):
            raise ConfigError("each [[interests]] entry needs name and keywords")
        interests.append(
            Interest(item["name"], [k.lower() for k in item["keywords"]], float(item.get("weight", 1.0)))
        )

    weights = dict(DEFAULT_WEIGHTS)
    for k, v in _section(raw, "weights").items():
        if k not in DEFAULT_WEIGHTS:
            raise ConfigError(f"[weights] unknown criterion: {k}")
        weights[k] = float(v)

    privacy = _section(raw, "privacy")
    return Config(
        subreddit=subreddit,
        db_path=db_path,
        period=period,
        comment_period=comment_period,
        retention=retention,
        classifier=classifier,
        obsidian=obsidian,
        interests=interests,
        weights=weights,
        store_author_names=bool(privacy.get("store_author_names", False)),
        base_dir=base_dir,
    )
