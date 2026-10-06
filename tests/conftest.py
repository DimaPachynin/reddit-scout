import json
from pathlib import Path

import pytest

from reddit_scout.config import load_config
from reddit_scout.storage import Store
from reddit_scout.synthetic import DEMO_PERIOD, SUBREDDIT, demo_bundle

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "examples" / "demo-config.toml"


@pytest.fixture
def make_cfg(tmp_path):
    def _make(**overrides):
        base = {"subreddit": SUBREDDIT, "start": DEMO_PERIOD[0], "end": DEMO_PERIOD[1],
                "db_path": str(tmp_path / "scout.sqlite3"), "vault_path": str(tmp_path / "vault")}
        base.update(overrides)
        return load_config(EXAMPLE_CONFIG, overrides=base)
    return _make


@pytest.fixture
def cfg(make_cfg):
    return make_cfg()


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


@pytest.fixture
def demo_file(tmp_path):
    p = tmp_path / "in" / "demo.json"
    p.parent.mkdir()
    p.write_text(json.dumps(demo_bundle(), ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v
