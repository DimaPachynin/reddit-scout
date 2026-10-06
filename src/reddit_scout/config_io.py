"""Read and write the TOML config as a plain dict (used by the GUI).

``tomllib`` only reads TOML, so this module has a small writer for the value
types the config uses: strings, numbers, booleans, string lists, tables and
the ``[[interests]]`` array of tables. Comments of a hand-edited file are not
preserved; the GUI says so before saving.
"""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = PROJECT_ROOT / "config.example.toml"

DEFAULTS: dict = {
    "project": {"subreddit": "", "db_path": "data/scout.sqlite3"},
    "period": {"start": "", "end": ""},
    "comments": {"start": "", "end": ""},
    "retention": {"max_days_since_check": 0},
    "privacy": {"store_author_names": False},
    "classifier": {"backend": "rules", "select_threshold": 0.45, "min_chars": 40, "max_per_thread": 8,
                   "max_per_collection": 40},
    "weights": {"applicability": 0.30, "specificity": 0.20, "evidence": 0.20, "originality": 0.10,
                "interest": 0.20, "votes": 0.05},
    "obsidian": {"vault_path": "", "folder": "Reddit Scout", "include_authors": False, "quote_mode": "excerpt",
                 "excerpt_chars": 400},
    "interests": [],
}


def load_raw(path: Path) -> dict:
    """Config file as dict, completed with defaults. Falls back to the example file."""
    source = path if path.exists() else EXAMPLE
    raw: dict = {}
    if source.exists():
        with source.open("rb") as fh:
            raw = tomllib.load(fh)
    merged = copy.deepcopy(DEFAULTS)
    for key, value in raw.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"unsupported config value: {v!r}")


def dumps(data: dict) -> str:
    lines = ["# reddit-scout configuration (written by the GUI).", ""]
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"[{key}]")
            for k, v in value.items():
                lines.append(f"{k} = {_value(v)}")
            lines.append("")
    for key, value in data.items():
        if isinstance(value, list) and value and all(isinstance(x, dict) for x in value):
            for item in value:
                lines.append(f"[[{key}]]")
                for k, v in item.items():
                    lines.append(f"{k} = {_value(v)}")
                lines.append("")
    return "\n".join(lines)


def save_raw(path: Path, data: dict) -> None:
    text = dumps(data)
    tomllib.loads(text)  # never write a file that cannot be read back
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)
