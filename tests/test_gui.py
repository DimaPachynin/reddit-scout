"""Config writer and a smoke test of the desktop UI (skipped without a display)."""

import time
import tomllib

import pytest

from reddit_scout.config import load_config
from reddit_scout.config_io import DEFAULTS, dumps, load_raw, save_raw
from reddit_scout.synthetic import DEMO_PERIOD, SUBREDDIT, demo_saved_pages


def test_config_roundtrip(tmp_path):
    raw = load_raw(tmp_path / "missing.toml")  # falls back to the example
    raw["project"]["subreddit"] = SUBREDDIT
    raw["obsidian"]["vault_path"] = 'C:\\Users\\me\\Vault "quoted"'
    raw["interests"] = [{"name": "Полив", "keywords": ["drip", "полив"], "weight": 1.5}]
    raw["gui"] = {"basis": "мои страницы", "paths": ["C:\\in"]}
    path = tmp_path / "config.toml"
    save_raw(path, raw)
    again = tomllib.loads(path.read_text(encoding="utf-8"))
    assert again["obsidian"]["vault_path"] == raw["obsidian"]["vault_path"]
    assert again["interests"][0]["keywords"] == ["drip", "полив"]
    cfg = load_config(path)
    assert cfg.subreddit == SUBREDDIT and cfg.interests[0].weight == 1.5
    assert set(DEFAULTS) <= set(load_raw(path))
    assert "[[interests]]" in dumps(raw)


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    yield root
    root.destroy()


def wait(app, root, timeout=30):
    end = time.time() + timeout
    while app.busy and time.time() < end:
        root.update()
        time.sleep(0.02)
    root.update()
    assert not app.busy


def test_gui_full_run(tk_root, tmp_path, monkeypatch):
    from reddit_scout import gui

    errors = []
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a, **k: errors.append(a))
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: errors.append(a))
    pages = tmp_path / "pages"
    pages.mkdir()
    for name, html in demo_saved_pages().items():
        (pages / name).write_text(html, encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()

    app = gui.App(tk_root, tmp_path / "config.toml")
    app.v["subreddit"].set(SUBREDDIT)
    app.v["start"].set(DEMO_PERIOD[0])
    app.v["end"].set(DEMO_PERIOD[1])
    app.v["db_path"].set(str(tmp_path / "db.sqlite3"))
    app.v["vault"].set(str(vault))
    app.paths.insert("end", str(pages))
    app.basis_var.set("pages I saved")
    assert app.save_settings() is not None
    assert (tmp_path / "config.toml").exists()

    app.do_run_all()
    wait(app, tk_root)
    assert not errors, errors
    notes = list(vault.rglob("*.md"))
    assert any("dm0006" in n.name for n in notes)
    app.refresh_report()
    assert "r/ScoutDemo" in app.report_text.get("1.0", "end")

    app.query_var.set("rainwater")
    app.do_search()
    assert app.results.get_children()

    # settings survive a restart, including the import list and basis
    app2 = gui.App(tk_root, tmp_path / "config.toml")
    assert app2.v["subreddit"].get() == SUBREDDIT
    assert app2.basis_var.get() == "pages I saved"
    assert app2.paths.get(0, "end") == (str(pages),)
