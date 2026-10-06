"""Desktop UI for Windows (tkinter): settings, import, processing, report, search, purge.

Start with ``reddit-scout-gui`` or ``start-gui.cmd``. All work happens locally;
long operations run in a background thread and write to the log at the bottom.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
import urllib.parse
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk

from . import __version__
from .classify import classify_all
from .config import ConfigError, load_config
from .config_io import PROJECT_ROOT, load_raw, save_raw
from .obsidian import export, safe_filename
from .report import coverage, render_text
from .retention import expired_ids, purge_expired, purge_ids
from .sources import local
from .storage import Store

QUOTE_MODES = {"excerpt": "фрагмент", "full": "полностью", "none": "только ссылка"}
WEIGHT_LABELS = {
    "applicability": "Практическая применимость",
    "specificity": "Конкретность",
    "evidence": "Обоснования и источники",
    "originality": "Оригинальность",
    "interest": "Соответствие интересам",
    "votes": "Голоса Reddit (вспомогательно)",
}
PAD = {"padx": 6, "pady": 3}


class App:
    def __init__(self, root: tk.Tk, config_path: Path):
        self.root = root
        self.config_path = config_path
        self.raw = load_raw(config_path)
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self.action_buttons: list[ttk.Button] = []

        root.title(f"reddit-scout {__version__}")
        root.geometry("1040x760")
        root.minsize(860, 600)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))

        status = ttk.Frame(root)
        status.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=6)
        self.status_var = tk.StringVar(value=f"Конфиг: {config_path}")
        ttk.Label(status, textvariable=self.status_var).pack(side=tk.LEFT)
        self.progress = ttk.Progressbar(status, mode="determinate", length=160)
        self.progress.pack(side=tk.RIGHT)

        paned = ttk.PanedWindow(root, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))
        self.notebook = ttk.Notebook(paned)
        paned.add(self.notebook, weight=4)
        log_frame = ttk.LabelFrame(paned, text="Журнал")
        paned.add(log_frame, weight=1)
        self.log_text = tk.Text(log_frame, height=6, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self._build_settings()
        self._build_import()
        self._build_process()
        self._build_report()
        self._build_search()
        self._build_purge()
        self.root.after(100, self._poll)
        if not config_path.exists():
            self.log("Конфиг ещё не создан: заполните «Настройки» и нажмите «Сохранить».")

    # ---- helpers ------------------------------------------------------------
    def log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text.rstrip() + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _button(self, parent, text, command, style=None, action=True):
        b = ttk.Button(parent, text=text, command=command, style=style or "TButton")
        if action:
            self.action_buttons.append(b)
        return b

    def _entry(self, parent, row, label, var, width=40, browse=None, hint=None):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **PAD)
        e = ttk.Entry(parent, textvariable=var, width=width)
        e.grid(row=row, column=1, sticky="we", **PAD)
        if browse:
            ttk.Button(parent, text="Обзор…", command=browse).grid(row=row, column=2, sticky="w", **PAD)
        if hint:
            ttk.Label(parent, text=hint, foreground="#666").grid(row=row, column=3, sticky="w", **PAD)
        return e

    def _set_busy(self, busy: bool, label: str = ""):
        self.busy = busy
        for b in self.action_buttons:
            b.configure(state="disabled" if busy else "normal")
        if busy:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
            self.status_var.set(f"Выполняется: {label}…")
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=0)
            self.status_var.set(f"Готово. Конфиг: {self.config_path}")

    def run_task(self, label: str, fn, on_done=None):
        """Run fn(cfg, log) in a worker thread with the saved config."""
        if self.busy:
            return
        cfg = self.save_settings(quiet=True)
        if cfg is None:
            return
        self._set_busy(True, label)
        self.log(f"▶ {label}")

        def worker():
            try:
                result = fn(cfg, lambda m: self.queue.put(("log", str(m))))
                self.queue.put(("done", label, result, on_done))
            except Exception as exc:  # report every failure in the UI
                self.queue.put(("error", label, exc, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                item = self.queue.get_nowait()
                if item[0] == "log":
                    self.log(item[1])
                elif item[0] == "done":
                    _, label, result, on_done = item
                    self._set_busy(False)
                    self.log(f"✔ {label}")
                    if on_done:
                        on_done(result)
                elif item[0] == "error":
                    _, label, exc, tb = item
                    self._set_busy(False)
                    self.log(f"✖ {label}: {exc}")
                    if not isinstance(exc, (ConfigError, ValueError, FileNotFoundError)):
                        self.log(tb)
                    messagebox.showerror("Ошибка", f"{label}:\n{exc}")
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ---- settings tab --------------------------------------------------------
    def _build_settings(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Настройки")
        r = self.raw
        self.v = {
            "subreddit": tk.StringVar(value=r["project"].get("subreddit", "")),
            "db_path": tk.StringVar(value=r["project"].get("db_path", "data/scout.sqlite3")),
            "start": tk.StringVar(value=r["period"].get("start", "")),
            "end": tk.StringVar(value=r["period"].get("end", "")),
            "c_start": tk.StringVar(value=r["comments"].get("start", "")),
            "c_end": tk.StringVar(value=r["comments"].get("end", "")),
            "vault": tk.StringVar(value=r["obsidian"].get("vault_path", "")),
            "folder": tk.StringVar(value=r["obsidian"].get("folder", "Reddit Scout")),
            "quote_mode": tk.StringVar(value=QUOTE_MODES.get(r["obsidian"].get("quote_mode", "excerpt"), "фрагмент")),
            "excerpt_chars": tk.IntVar(value=int(r["obsidian"].get("excerpt_chars", 400))),
            "include_authors": tk.BooleanVar(value=bool(r["obsidian"].get("include_authors", False))),
            "store_authors": tk.BooleanVar(value=bool(r["privacy"].get("store_author_names", False))),
            "retention_days": tk.IntVar(value=int(r["retention"].get("max_days_since_check", 0))),
            "threshold": tk.DoubleVar(value=float(r["classifier"].get("select_threshold", 0.45))),
            "min_chars": tk.IntVar(value=int(r["classifier"].get("min_chars", 40))),
            "max_per_thread": tk.IntVar(value=int(r["classifier"].get("max_per_thread", 8))),
        }
        canvas_holder = ttk.Frame(tab)
        canvas_holder.pack(fill=tk.BOTH, expand=True)
        left = ttk.Frame(canvas_holder)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        right = ttk.Frame(canvas_holder)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        f = ttk.LabelFrame(left, text="Subreddit и период")
        f.pack(fill=tk.X, **PAD)
        f.columnconfigure(1, weight=1)
        self._entry(f, 0, "Subreddit (без r/)", self.v["subreddit"], width=28)
        self._entry(f, 1, "Публикации с (ГГГГ-ММ-ДД)", self.v["start"], width=14)
        self._entry(f, 2, "по (включительно)", self.v["end"], width=14)
        ttk.Label(f, text="Пусто = предыдущие 12 месяцев", foreground="#666").grid(row=3, column=1, sticky="w", **PAD)
        self._entry(f, 4, "Комментарии с", self.v["c_start"], width=14)
        self._entry(f, 5, "по", self.v["c_end"], width=14)
        ttk.Label(f, text="Пусто = без фильтра дат комментариев", foreground="#666").grid(row=6, column=1, sticky="w", **PAD)

        f = ttk.LabelFrame(left, text="Obsidian")
        f.pack(fill=tk.X, **PAD)
        f.columnconfigure(1, weight=1)
        self._entry(f, 0, "Папка vault", self.v["vault"], browse=self._browse_vault)
        self._entry(f, 1, "Папка внутри vault", self.v["folder"], width=24)
        ttk.Label(f, text="Цитаты Reddit").grid(row=2, column=0, sticky="w", **PAD)
        ttk.Combobox(f, textvariable=self.v["quote_mode"], values=list(QUOTE_MODES.values()), state="readonly",
                     width=16).grid(row=2, column=1, sticky="w", **PAD)
        ttk.Label(f, text="Длина фрагмента").grid(row=3, column=0, sticky="w", **PAD)
        ttk.Spinbox(f, from_=100, to=5000, increment=100, textvariable=self.v["excerpt_chars"], width=8
                    ).grid(row=3, column=1, sticky="w", **PAD)
        ttk.Checkbutton(f, text="Показывать авторов в заметках", variable=self.v["include_authors"]
                        ).grid(row=4, column=1, sticky="w", **PAD)

        f = ttk.LabelFrame(left, text="Отбор и хранение")
        f.pack(fill=tk.X, **PAD)
        ttk.Label(f, text="Порог отбора (0–1)").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Spinbox(f, from_=0, to=1, increment=0.05, textvariable=self.v["threshold"], width=8
                    ).grid(row=0, column=1, sticky="w", **PAD)
        ttk.Label(f, text="Мин. длина комментария").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Spinbox(f, from_=0, to=2000, increment=10, textvariable=self.v["min_chars"], width=8
                    ).grid(row=1, column=1, sticky="w", **PAD)
        ttk.Label(f, text="Макс. отобранных в теме").grid(row=2, column=0, sticky="w", **PAD)
        ttk.Spinbox(f, from_=1, to=100, textvariable=self.v["max_per_thread"], width=8
                    ).grid(row=2, column=1, sticky="w", **PAD)
        ttk.Label(f, text="Автоудаление через, дней").grid(row=3, column=0, sticky="w", **PAD)
        ttk.Spinbox(f, from_=0, to=3650, textvariable=self.v["retention_days"], width=8
                    ).grid(row=3, column=1, sticky="w", **PAD)
        ttk.Label(f, text="0 = выкл.; считается от последнего импорта", foreground="#666"
                  ).grid(row=3, column=2, sticky="w", **PAD)
        ttk.Checkbutton(f, text="Хранить имена авторов", variable=self.v["store_authors"]
                        ).grid(row=4, column=1, columnspan=2, sticky="w", **PAD)

        f = ttk.LabelFrame(right, text="Веса критериев полезности")
        f.pack(fill=tk.X, **PAD)
        self.weight_vars = {}
        for i, (key, label) in enumerate(WEIGHT_LABELS.items()):
            var = tk.DoubleVar(value=float(self.raw["weights"].get(key, 0)))
            self.weight_vars[key] = var
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", **PAD)
            ttk.Scale(f, from_=0, to=1, variable=var, length=160,
                      command=lambda _v, k=key: self._weight_label(k)).grid(row=i, column=1, **PAD)
            lbl = ttk.Label(f, width=5)
            lbl.grid(row=i, column=2, **PAD)
            var._label = lbl  # type: ignore[attr-defined]
            self._weight_label(key)

        f = ttk.LabelFrame(right, text="Интересы (темы подборок)")
        f.pack(fill=tk.BOTH, expand=True, **PAD)
        self.interests = ttk.Treeview(f, columns=("name", "keywords", "weight"), show="headings", height=6)
        for col, text, w in (("name", "Тема", 120), ("keywords", "Ключевые слова", 220), ("weight", "Вес", 50)):
            self.interests.heading(col, text=text)
            self.interests.column(col, width=w, stretch=col == "keywords")
        self.interests.pack(fill=tk.BOTH, expand=True, **PAD)
        for it in self.raw.get("interests", []):
            self.interests.insert("", tk.END, values=(it["name"], ", ".join(it["keywords"]), it.get("weight", 1.0)))
        self.interests.bind("<<TreeviewSelect>>", self._interest_selected)
        form = ttk.Frame(f)
        form.pack(fill=tk.X)
        self.i_name, self.i_kw, self.i_w = tk.StringVar(), tk.StringVar(), tk.StringVar(value="1.0")
        ttk.Label(form, text="Тема").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Entry(form, textvariable=self.i_name, width=18).grid(row=0, column=1, sticky="we", **PAD)
        ttk.Label(form, text="Вес").grid(row=0, column=2, sticky="w", **PAD)
        ttk.Entry(form, textvariable=self.i_w, width=6).grid(row=0, column=3, sticky="w", **PAD)
        ttk.Label(form, text="Слова через запятую").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(form, textvariable=self.i_kw).grid(row=1, column=1, columnspan=3, sticky="we", **PAD)
        form.columnconfigure(1, weight=1)
        btns = ttk.Frame(f)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="Добавить", command=self._interest_add).pack(side=tk.LEFT, **PAD)
        ttk.Button(btns, text="Изменить выбранную", command=self._interest_update).pack(side=tk.LEFT, **PAD)
        ttk.Button(btns, text="Удалить выбранную", command=self._interest_delete).pack(side=tk.LEFT, **PAD)

        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, pady=4)
        self._button(bar, "Сохранить настройки", self.save_settings, style="Accent.TButton").pack(side=tk.RIGHT, **PAD)
        ttk.Label(bar, text=f"Файл: {self.config_path.name} (комментарии в файле при сохранении не сохраняются)",
                  foreground="#666").pack(side=tk.LEFT, **PAD)

    def _weight_label(self, key):
        var = self.weight_vars[key]
        var._label.configure(text=f"{var.get():.2f}")  # type: ignore[attr-defined]

    def _browse_vault(self):
        path = filedialog.askdirectory(title="Папка Obsidian vault", initialdir=self.v["vault"].get() or None)
        if path:
            self.v["vault"].set(path)

    def _interest_selected(self, _e=None):
        sel = self.interests.selection()
        if sel:
            name, kw, w = self.interests.item(sel[0], "values")
            self.i_name.set(name), self.i_kw.set(kw), self.i_w.set(w)

    def _interest_values(self):
        name = self.i_name.get().strip()
        kws = [k.strip().lower() for k in self.i_kw.get().split(",") if k.strip()]
        try:
            weight = float(self.i_w.get().replace(",", "."))
        except ValueError:
            weight = -1
        if not name or not kws or weight < 0:
            messagebox.showwarning("Интерес", "Нужны название, хотя бы одно слово и вес ≥ 0.")
            return None
        return name, ", ".join(kws), weight

    def _interest_add(self):
        vals = self._interest_values()
        if vals:
            self.interests.insert("", tk.END, values=vals)

    def _interest_update(self):
        sel = self.interests.selection()
        vals = self._interest_values()
        if sel and vals:
            self.interests.item(sel[0], values=vals)

    def _interest_delete(self):
        for s in self.interests.selection():
            self.interests.delete(s)

    def _collect(self) -> dict:
        r = self.raw
        v = self.v
        r["project"]["subreddit"] = v["subreddit"].get().strip()
        r["project"]["db_path"] = v["db_path"].get().strip() or "data/scout.sqlite3"
        r["period"] = {"start": v["start"].get().strip(), "end": v["end"].get().strip()}
        r["comments"] = {"start": v["c_start"].get().strip(), "end": v["c_end"].get().strip()}
        mode = {label: key for key, label in QUOTE_MODES.items()}.get(v["quote_mode"].get(), "excerpt")
        r["obsidian"].update(vault_path=v["vault"].get().strip(), folder=v["folder"].get().strip() or "Reddit Scout",
                             quote_mode=mode, excerpt_chars=int(v["excerpt_chars"].get()),
                             include_authors=bool(v["include_authors"].get()))
        r["privacy"]["store_author_names"] = bool(v["store_authors"].get())
        r["retention"]["max_days_since_check"] = int(v["retention_days"].get())
        r["classifier"].update(select_threshold=round(float(v["threshold"].get()), 3),
                               min_chars=int(v["min_chars"].get()), max_per_thread=int(v["max_per_thread"].get()))
        r["weights"] = {k: round(float(var.get()), 3) for k, var in self.weight_vars.items()}
        r["interests"] = []
        for iid in self.interests.get_children():
            name, kw, w = self.interests.item(iid, "values")
            r["interests"].append({"name": str(name), "keywords": [k.strip() for k in str(kw).split(",") if k.strip()],
                                   "weight": float(w)})
        gui = r.setdefault("gui", {})
        if hasattr(self, "basis_var"):
            gui["basis"] = self.basis_var.get()
            gui["source_id"] = self.source_var.get()
            gui["paths"] = list(self.paths.get(0, tk.END))
        return r

    def save_settings(self, quiet: bool = False):
        """Write config.toml and return the validated Config (None on error)."""
        try:
            raw = self._collect()
            save_raw(self.config_path, raw)
            cfg = load_config(self.config_path)
        except (ConfigError, ValueError, tk.TclError) as exc:
            messagebox.showerror("Настройки", f"Настройки не приняты:\n{exc}")
            return None
        if not quiet:
            self.log(f"Настройки сохранены: {self.config_path}")
        return cfg

    # ---- import tab -------------------------------------------------------------
    def _build_import(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Импорт")
        gui = self.raw.get("gui", {})
        info = ("Источники — только локальные файлы:\n"
                "• JSON в формате Reddit, на обработку которого у вас есть основание;\n"
                "• необязательно: страницы тредов, сохранённые вами из браузера (Ctrl+S → «Веб-страница полностью», "
                "«только HTML» или .mhtml). Перед сохранением раскройте нужные ветки.\n"
                "Повторный импорт тех же файлов пропускается; новая копия треда дополняет базу и вычищает удалённое.")
        ttk.Label(tab, text=info, justify=tk.LEFT, wraplength=960).pack(fill=tk.X, padx=10, pady=8)
        f = ttk.LabelFrame(tab, text="Файлы и папки")
        f.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)
        self.paths = tk.Listbox(f, height=8, selectmode=tk.EXTENDED)
        self.paths.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, **PAD)
        for p in gui.get("paths", []):
            self.paths.insert(tk.END, p)
        side = ttk.Frame(f)
        side.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(side, text="Добавить файлы…", command=self._add_files).pack(fill=tk.X, **PAD)
        ttk.Button(side, text="Добавить папку…", command=self._add_folder).pack(fill=tk.X, **PAD)
        ttk.Button(side, text="Убрать выбранные", command=self._remove_paths).pack(fill=tk.X, **PAD)
        ttk.Button(side, text="Очистить список", command=lambda: self.paths.delete(0, tk.END)).pack(fill=tk.X, **PAD)

        g = ttk.Frame(tab)
        g.pack(fill=tk.X, padx=8, pady=4)
        g.columnconfigure(1, weight=1)
        self.basis_var = tk.StringVar(value=gui.get("basis", ""))
        self.source_var = tk.StringVar(value=gui.get("source_id", "local"))
        self.force_var = tk.BooleanVar(value=False)
        self._entry(g, 0, "Основание на обработку *", self.basis_var, width=70)
        ttk.Label(g, text="Например: «страницы, сохранённые мной при чтении, для личных заметок». "
                          "Сохраняется как происхождение данных.", foreground="#666"
                  ).grid(row=1, column=1, columnspan=3, sticky="w", **PAD)
        self._entry(g, 2, "Имя источника", self.source_var, width=20)
        ttk.Checkbutton(g, text="Импортировать заново уже импортированные файлы", variable=self.force_var
                        ).grid(row=3, column=1, sticky="w", **PAD)
        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, padx=8, pady=6)
        self._button(bar, "Импортировать", self.do_import, style="Accent.TButton").pack(side=tk.RIGHT, **PAD)

    def _add_files(self):
        files = filedialog.askopenfilenames(
            title="Файлы для импорта",
            filetypes=[("Reddit JSON и сохранённые страницы", "*.json *.jsonl *.html *.htm *.mhtml *.mht"),
                       ("Все файлы", "*.*")])
        for f in files:
            if f not in self.paths.get(0, tk.END):
                self.paths.insert(tk.END, f)

    def _add_folder(self):
        d = filedialog.askdirectory(title="Папка с файлами для импорта")
        if d and d not in self.paths.get(0, tk.END):
            self.paths.insert(tk.END, d)

    def _remove_paths(self):
        for i in reversed(self.paths.curselection()):
            self.paths.delete(i)

    def _import_args(self):
        paths = [Path(p) for p in self.paths.get(0, tk.END)]
        if not paths:
            messagebox.showwarning("Импорт", "Добавьте файлы или папку.")
            return None
        basis = self.basis_var.get().strip()
        if not basis:
            messagebox.showwarning("Импорт", "Укажите основание на обработку данных.")
            return None
        return paths, basis, safe_filename(self.source_var.get().strip() or "local").replace(" ", "_")

    def do_import(self):
        args = self._import_args()
        if not args:
            return
        paths, basis, source_id = args
        force = self.force_var.get()

        def task(cfg, log):
            store = Store(cfg.db_path)
            try:
                days = cfg.retention.max_days_since_check
                return local.import_paths(store, cfg, paths, source_id=source_id,
                                          description=f"local files ({source_id})", basis=basis,
                                          max_hours_since_check=days * 24 if days > 0 else None,
                                          force=force, log=log)
            finally:
                store.close()

        self.run_task("Импорт", task, self._show_import_stats)

    def _show_import_stats(self, stats: dict):
        self.log(f"Файлов: {stats['files_done']} обработано, {stats['files_skipped']} пропущено (уже были); "
                 f"тем: {stats['threads']}, только постов: {stats['posts_only']}, страниц HTML: {stats['html_pages']}, "
                 f"не тредов: {stats['html_not_thread']}; вне периода: {stats['out_of_period']}, "
                 f"другой subreddit: {stats['other_subreddit']}; новых комментариев: {stats['comments_new']}, "
                 f"удалено: {stats['purged']}")
        for g in stats.get("gaps", []):
            self.log(f"  ! {g}")
        self.refresh_report()

    # ---- processing tab -------------------------------------------------------
    def _build_process(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Обработка и экспорт")
        steps = [
            ("Выполнить всё", "Импорт списка с вкладки «Импорт» (если он не пуст) → удаление просроченного → "
                              "классификация → экспорт в Obsidian.", self.do_run_all, "Accent.TButton"),
            ("Классифицировать", "Оценить комментарии по категориям и критериям полезности.", self.do_classify, None),
            ("Экспорт в Obsidian", "Записать заметки. Ваши записи под «Мои заметки» сохраняются.", self.do_export, None),
            ("Удалить просроченное", "Удалить элементы, которые не встречались в импорте дольше срока из настроек.",
             self.do_purge_expired, None),
        ]
        for i, (title, text, cmd, style) in enumerate(steps):
            self._button(tab, title, cmd, style=style).grid(row=i, column=0, sticky="we", padx=12, pady=8)
            ttk.Label(tab, text=text, wraplength=700).grid(row=i, column=1, sticky="w", padx=8)
        ttk.Separator(tab).grid(row=10, column=0, columnspan=2, sticky="we", pady=10)
        ttk.Button(tab, text="Открыть в Obsidian", command=self.open_obsidian).grid(row=11, column=0, sticky="we", padx=12, pady=6)
        ttk.Label(tab, text="Открывает vault в Obsidian (vault должен быть уже добавлен в Obsidian).").grid(
            row=11, column=1, sticky="w", padx=8)
        ttk.Button(tab, text="Открыть папку заметок", command=self.open_notes_folder).grid(row=12, column=0, sticky="we", padx=12, pady=6)
        ttk.Label(tab, text="Папка <vault>/<папка>/r_<subreddit> в Проводнике.").grid(row=12, column=1, sticky="w", padx=8)
        self._button(tab, "Собрать демо-vault", self.do_demo).grid(row=13, column=0, sticky="we", padx=12, pady=6)
        ttk.Label(tab, text="Синтетические данные → examples/demo-vault (ваши настройки и база не меняются).").grid(
            row=13, column=1, sticky="w", padx=8)
        tab.columnconfigure(1, weight=1)

    def _require_vault(self, cfg) -> Path | None:
        if not cfg.obsidian.vault_path:
            messagebox.showwarning("Obsidian", "Укажите папку vault на вкладке «Настройки».")
            return None
        return Path(cfg.obsidian.vault_path)

    def do_classify(self):
        def task(cfg, log):
            store = Store(cfg.db_path)
            try:
                return classify_all(store, cfg)
            finally:
                store.close()
        self.run_task("Классификация", task,
                      lambda s: self.log(f"Оценено: {s['assessed']}, отобрано: {s['selected']}, "
                                         f"вне периода комментариев: {s['out_of_comment_period']}"))

    def _export_task(self, cfg, log):
        store = Store(cfg.db_path)
        try:
            return export(store, cfg, Path(cfg.obsidian.vault_path) if cfg.obsidian.vault_path else None)
        finally:
            store.close()

    def _show_export(self, res):
        self.log(f"Записано: {len(res.written)}, без изменений: {len(res.unchanged)}, удалено: {len(res.removed)}, "
                 f"оставлено с пометкой об удалении: {len(res.kept_with_notice)}")
        for w in res.warnings:
            self.log(f"  ! {w}")

    def do_export(self):
        cfg = self.save_settings(quiet=True)
        if cfg is None or self._require_vault(cfg) is None:
            return
        self.run_task("Экспорт в Obsidian", self._export_task, self._show_export)

    def do_run_all(self):
        cfg = self.save_settings(quiet=True)
        if cfg is None or self._require_vault(cfg) is None:
            return
        import_args = None
        if self.paths.size():
            import_args = self._import_args()
            if import_args is None:
                return
        force = self.force_var.get()

        def task(cfg, log):
            store = Store(cfg.db_path)
            try:
                if import_args:
                    paths, basis, source_id = import_args
                    days = cfg.retention.max_days_since_check
                    st = local.import_paths(store, cfg, paths, source_id=source_id,
                                            description=f"local files ({source_id})", basis=basis,
                                            max_hours_since_check=days * 24 if days > 0 else None,
                                            force=force, log=log)
                    log(f"импорт: тем {st['threads']}, страниц HTML {st['html_pages']}, "
                        f"пропущено файлов {st['files_skipped']}")
                n = purge_expired(store, subreddit=cfg.subreddit)
                log(f"удалено просроченного: {n}")
                c = classify_all(store, cfg)
                log(f"классификация: оценено {c['assessed']}, отобрано {c['selected']}")
            finally:
                store.close()
            return self._export_task(cfg, log)

        self.run_task("Выполнить всё", task, lambda res: (self._show_export(res), self.refresh_report()))

    def do_purge_expired(self):
        cfg = self.save_settings(quiet=True)
        if cfg is None:
            return
        store = Store(cfg.db_path)
        n = len(expired_ids(store, subreddit=cfg.subreddit))
        store.close()
        if n == 0:
            messagebox.showinfo("Удаление просроченного", "Просроченных элементов нет.")
            return
        if not messagebox.askyesno("Удаление просроченного",
                                   f"Удалить текст {n} элемент(ов) из базы? Это нельзя отменить.\n"
                                   "Из заметок он исчезнет при следующем экспорте."):
            return

        def task(cfg, log):
            store = Store(cfg.db_path)
            try:
                return purge_expired(store, subreddit=cfg.subreddit)
            finally:
                store.close()
        self.run_task("Удаление просроченного", task, lambda n: self.log(f"Удалено: {n}. Запустите экспорт."))

    def _notes_root(self) -> Path | None:
        cfg = self.save_settings(quiet=True)
        if cfg is None or self._require_vault(cfg) is None:
            return None
        return Path(cfg.obsidian.vault_path) / safe_filename(cfg.obsidian.folder) / safe_filename(f"r_{cfg.subreddit}")

    def open_obsidian(self):
        cfg = self.save_settings(quiet=True)
        if cfg is None or self._require_vault(cfg) is None:
            return
        uri = "obsidian://open?path=" + urllib.parse.quote(str(Path(cfg.obsidian.vault_path).resolve()))
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("Obsidian", f"Не удалось открыть Obsidian: {exc}")

    def open_notes_folder(self):
        root = self._notes_root()
        if root is None:
            return
        target = root if root.exists() else root.parents[1]
        if not target.exists():
            messagebox.showinfo("Папка", "Папка ещё не создана: сначала выполните экспорт.")
            return
        os.startfile(str(target))  # type: ignore[attr-defined]

    def do_demo(self):
        out = PROJECT_ROOT / "examples" / "demo-vault"

        def task(cfg, log):
            from .cli import main
            if main(["demo", "--out", str(out)]) != 0:
                raise RuntimeError("demo failed")
            return out
        self.run_task("Демо", task, lambda p: self.log(f"Демо-vault: {p}. Откройте папку как vault в Obsidian."))

    # ---- report tab ----------------------------------------------------------------
    def _build_report(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Отчёт об охвате")
        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X)
        ttk.Button(bar, text="Обновить", command=self.refresh_report).pack(side=tk.LEFT, **PAD)
        self.report_text = tk.Text(tab, wrap="word", font=("Consolas", 10), state="disabled")
        self.report_text.pack(fill=tk.BOTH, expand=True, **PAD)
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)

    def _tab_changed(self, _e=None):
        if self.notebook.tab(self.notebook.select(), "text") == "Отчёт об охвате":
            self.refresh_report()

    def refresh_report(self):
        try:
            cfg = load_config(self.config_path)
        except (ConfigError, ValueError) as exc:
            text = f"Сначала сохраните настройки: {exc}"
        else:
            if not cfg.db_path.exists():
                text = "База ещё пуста: импортируйте файлы."
            else:
                store = Store(cfg.db_path)
                try:
                    text = render_text(coverage(store, cfg))
                finally:
                    store.close()
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", tk.END)
        self.report_text.insert("1.0", text)
        self.report_text.configure(state="disabled")

    # ---- search tab --------------------------------------------------------------------
    def _build_search(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Поиск")
        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, **PAD)
        self.query_var = tk.StringVar()
        e = ttk.Entry(bar, textvariable=self.query_var, width=50)
        e.pack(side=tk.LEFT, **PAD)
        e.bind("<Return>", lambda _e: self.do_search())
        ttk.Button(bar, text="Найти", command=self.do_search).pack(side=tk.LEFT, **PAD)
        ttk.Button(bar, text="Копировать id", command=self._copy_ids).pack(side=tk.LEFT, **PAD)
        ttk.Button(bar, text="Удалить выбранные…", command=self._purge_selected).pack(side=tk.LEFT, **PAD)
        self.results = ttk.Treeview(tab, columns=("id", "post", "snippet"), show="headings")
        for col, text, w in (("id", "id", 110), ("post", "тема", 110), ("snippet", "фрагмент", 700)):
            self.results.heading(col, text=text)
            self.results.column(col, width=w, stretch=col == "snippet")
        self.results.pack(fill=tk.BOTH, expand=True, **PAD)

    def do_search(self):
        try:
            cfg = load_config(self.config_path)
        except (ConfigError, ValueError) as exc:
            messagebox.showerror("Поиск", str(exc))
            return
        self.results.delete(*self.results.get_children())
        if not cfg.db_path.exists():
            return
        store = Store(cfg.db_path)
        try:
            for row in store.search(self.query_var.get(), 200):
                self.results.insert("", tk.END, values=(row["thing_id"], row["post_id"], row["snip"].replace("\n", " ")))
        finally:
            store.close()

    def _selected_ids(self) -> list[str]:
        return [self.results.item(i, "values")[0] for i in self.results.selection()]

    def _copy_ids(self):
        ids = self._selected_ids()
        if ids:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(ids))

    def _purge_selected(self):
        ids = self._selected_ids()
        if ids:
            self.purge_text.delete("1.0", tk.END)
            self.purge_text.insert("1.0", "\n".join(ids))
            self.notebook.select(self.purge_tab)

    # ---- purge tab ----------------------------------------------------------------------
    def _build_purge(self):
        tab = ttk.Frame(self.notebook)
        self.purge_tab = tab
        self.notebook.add(tab, text="Удаление")
        ttk.Label(tab, wraplength=960, justify=tk.LEFT, text=(
            "Удаляет текст публикаций (t3_…) и комментариев (t1_…) из базы, поискового индекса и оценок; при следующем "
            "экспорте — из заметок. Удаление публикации удаляет и её комментарии. Повторный импорт старой копии текст "
            "не вернёт. Ваши записи под «Мои заметки» сохраняются.")).pack(fill=tk.X, padx=10, pady=8)
        ttk.Label(tab, text="Идентификаторы (по одному в строке или через пробел):").pack(anchor="w", padx=10)
        self.purge_text = tk.Text(tab, height=10, font=("Consolas", 10))
        self.purge_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=4)
        bar = ttk.Frame(tab)
        bar.pack(fill=tk.X, padx=8, pady=6)
        self.reason_var = tk.StringVar(value="удалено в источнике")
        ttk.Label(bar, text="Причина").pack(side=tk.LEFT, **PAD)
        ttk.Entry(bar, textvariable=self.reason_var, width=40).pack(side=tk.LEFT, **PAD)
        self._button(bar, "Удалить…", self.do_purge_ids, style="Accent.TButton").pack(side=tk.RIGHT, **PAD)

    def do_purge_ids(self):
        ids = self.purge_text.get("1.0", tk.END).split()
        bad = [i for i in ids if not (i.startswith("t1_") or i.startswith("t3_"))]
        if not ids or bad:
            messagebox.showwarning("Удаление", "Нужны id вида t1_abc или t3_abc." + (f"\nНеверно: {bad[:5]}" if bad else ""))
            return
        if not messagebox.askyesno("Удаление", f"Удалить текст {len(ids)} элемент(ов)? Это нельзя отменить.\n"
                                               "Из заметок он исчезнет при следующем экспорте."):
            return
        reason = self.reason_var.get().strip() or "manual purge request"

        def task(cfg, log):
            store = Store(cfg.db_path)
            try:
                return purge_ids(store, ids, reason)
            finally:
                store.close()
        self.run_task("Удаление", task, lambda n: (self.log(f"Удалено: {n}. Запустите экспорт."),
                                                   self.purge_text.delete("1.0", tk.END)))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config_path = Path(argv[0]) if argv else PROJECT_ROOT / "config.toml"
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # sharp text on high-DPI screens
    except (ImportError, AttributeError, OSError):
        pass
    root = tk.Tk()
    App(root, config_path.resolve())
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
