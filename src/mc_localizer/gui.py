from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .pipeline import PatchGenerator
from .cfpa import ensure_cfpa_pack
from .custom_json import translate_dictionary_file, translate_text_file
from .resource_memory import CompositeMemory, ResourcePackMemory
from .runtime import data_dir
from .translator import CoalescingTranslator, IdentityTranslator, OpenAITranslator, TranslationCache
from . import __version__


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"Minecraft 汉化补丁生成器 v{__version__}")
        self.geometry("860x715")
        self.config_path = data_dir() / "api_config.json"
        self.debug_dir = data_dir() / "debug"
        self.debug_status_path = self.debug_dir / "current_status.json"
        self.debug_log_path = self.debug_dir / "live.log"
        self.last_event = "等待开始"
        saved = self.read_api_config()
        self.vars = {name: tk.StringVar(value=value) for name, value in {
            "source": "", "output": "", "api_key": os.getenv("MC_LOCALIZER_API_KEY", ""),
            "base_url": os.getenv("MC_LOCALIZER_BASE_URL", "https://api.openai.com/v1"),
            "model": os.getenv("MC_LOCALIZER_MODEL", "gpt-4.1-mini"),
            "reasoning_effort": "disabled",
            "scan_workers": str(max(2, (os.cpu_count() or 4) * 2)),
            "process_workers": str(min(32, os.cpu_count() or 4)),
            "api_workers": "8", "memory": "",
            "glossary": "",
        }.items()}
        if "scan_workers" not in saved and "workers" in saved:
            saved["scan_workers"] = saved["workers"]
        for key in ("api_key", "base_url", "model", "reasoning_effort", "scan_workers", "process_workers", "api_workers"):
            if key in saved and not os.getenv(f"MC_LOCALIZER_{'API_KEY' if key == 'api_key' else key.upper()}"):
                self.vars[key].set(saved[key])
        for key in ("source", "output", "glossary"):
            saved_path = saved.get(key, "")
            if saved_path and Path(saved_path).exists():
                self.vars[key].set(saved_path)
        self.cfpa_status_var = tk.StringVar(value="开始生成时自动匹配并下载")
        form = ttk.Frame(self, padding=12); form.pack(fill="x")
        rows = [("整合包/版本目录", "source", self.pick_source), ("输出目录", "output", self.pick_output),
                ("API Key（留空仅复用现有翻译）", "api_key", None), ("API 地址", "base_url", self.save_api_config),
                ("模型", "model", self.load_models), ("思考强度", "reasoning_effort", None),
                ("已有中文资源包 ZIP", "memory", self.pick_memory),
                ("自动 CFPA 资源包", "cfpa_status", None),
                ("术语表", "glossary", self.show_glossary_editor),
                ("扫描线程数", "scan_workers", None), ("CPU 进程数", "process_workers", None),
                ("API 并发数", "api_workers", None)]
        for row, (label, key, action) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=4)
            if key == "model":
                self.model_box = ttk.Combobox(form, textvariable=self.vars[key])
                self.model_box.grid(row=row, column=1, sticky="ew", padx=8)
            elif key == "reasoning_effort":
                ttk.Combobox(form, textvariable=self.vars[key], state="readonly",
                             values=("disabled", "low", "high", "max")).grid(row=row, column=1, sticky="ew", padx=8)
            elif key in {"scan_workers", "process_workers", "api_workers"}:
                ttk.Spinbox(form, from_=1, to=128, textvariable=self.vars[key]).grid(row=row, column=1, sticky="ew", padx=8)
            elif key == "cfpa_status":
                ttk.Entry(form, textvariable=self.cfpa_status_var, state="readonly").grid(row=row, column=1, sticky="ew", padx=8)
            else:
                ttk.Entry(form, textvariable=self.vars[key], show="*" if key == "api_key" else "").grid(row=row, column=1, sticky="ew", padx=8)
            if action:
                text = "读取模型" if key == "model" else ("保存配置" if key == "base_url" else ("编辑" if key == "glossary" else "选择"))
                button = ttk.Button(form, text=text, command=action)
                button.grid(row=row, column=2)
                if key == "model": self.models_button = button
        form.columnconfigure(1, weight=1)
        controls = ttk.Frame(self, padding=(12, 0)); controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="开始生成", command=self.start); self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止", command=self.stop, state="disabled"); self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="查看本地缓存", command=self.show_cache).pack(side="left", padx=(8, 0))
        self.estimate_button = ttk.Button(controls, text="扫描预估", command=self.estimate); self.estimate_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="翻译范围", command=self.show_scopes).pack(side="left", padx=(8, 0))
        self.custom_button = ttk.Button(controls, text="自定义翻译", command=self.start_custom_translation)
        self.custom_button.pack(side="left", padx=(8, 0))
        self.quality_button = ttk.Button(controls, text="质量报告", command=self.show_quality, state="disabled"); self.quality_button.pack(side="left", padx=(8, 0))
        self.install_button = ttk.Button(controls, text="一键安装", command=self.install_release, state="disabled"); self.install_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="恢复安装", command=self.restore_install).pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(controls, mode="determinate", maximum=100); self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(controls, textvariable=self.status_var, width=18).pack(side="right")
        self.debug_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="调试监控", variable=self.debug_enabled).pack(side="right", padx=8)
        self.log = tk.Text(self, bg="#101418", fg="#e8eef2", font=("Microsoft YaHei UI", 10)); self.log.pack(fill="both", expand=True, padx=12, pady=12)
        self.messages = queue.Queue()
        self.cancel_event = threading.Event()
        self.last_result = None
        self.scope_vars = {name: tk.BooleanVar(value=bool(saved.get(f"scope_{name}", True)))
                           for name in ("locale", "patchouli", "quests", "scripts", "config", "serverconfig", "shaders")}
        self.autodetect_resources(match_source=bool(self.vars["source"].get()))
        self.log.insert("end", f"调试状态：{self.debug_status_path}\n调试日志：{self.debug_log_path}\n\n")
        self.load_release_from_output()
        self.write_debug_status("idle")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.flush)

    def read_api_config(self) -> dict:
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save_api_config(self, notify: bool = True):
        data = {key: self.vars[key].get().strip()
                for key in ("source", "output", "api_key", "base_url", "model", "reasoning_effort",
                            "scan_workers", "process_workers", "api_workers", "glossary")}
        if hasattr(self, "scope_vars"):
            data.update({f"scope_{key}": value.get() for key, value in self.scope_vars.items()})
        try:
            self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if notify: messagebox.showinfo("配置已保存", f"API 配置已保存到：\n{self.config_path}")
        except OSError as exc:
            if notify: messagebox.showerror("保存失败", str(exc))

    def on_close(self):
        self.write_debug_status("closed")
        self.save_api_config(notify=False)
        self.destroy()

    def append_debug_log(self, text: str) -> None:
        if not self.debug_enabled.get(): return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            with self.debug_log_path.open("a", encoding="utf-8") as stream:
                stream.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
        except OSError:
            pass

    def write_debug_status(self, state: str, result=None) -> None:
        if not self.debug_enabled.get(): return
        elapsed = int(time.monotonic() - self.started_at) if hasattr(self, "started_at") else 0
        data = {
            "state": state, "pid": os.getpid(), "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": elapsed, "last_event": self.last_event,
            "source": self.vars["source"].get(), "output": self.vars["output"].get(),
            "resource_pack": self.vars["memory"].get(),
            "api_base_url": self.vars["base_url"].get(), "model": self.vars["model"].get(),
            "reasoning_effort": self.vars["reasoning_effort"].get(),
            "scan_workers": self.vars["scan_workers"].get(), "api_workers": self.vars["api_workers"].get(),
            "process_workers": self.vars["process_workers"].get(),
            "active_python_threads": threading.active_count(), "pending_ui_messages": self.messages.qsize() if hasattr(self, "messages") else 0,
        }
        if result is not None: data["result"] = result
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.debug_status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.debug_status_path)
        except OSError:
            pass

    def pick_source(self):
        selected = filedialog.askdirectory()
        if selected:
            self.vars["source"].set(selected)
            self.autodetect_resources(match_source=True)
    def pick_output(self):
        selected = filedialog.askdirectory()
        if selected:
            self.vars["output"].set(selected)
            self.load_release_from_output()
    def pick_memory(self): self.vars["memory"].set(filedialog.askopenfilename(filetypes=[("Resource pack", "*.zip")]) or self.vars["memory"].get())
    def pick_glossary(self): self.vars["glossary"].set(filedialog.askopenfilename(filetypes=[("JSON", "*.json")]) or self.vars["glossary"].get())

    @staticmethod
    def load_glossary(path: str | Path) -> dict[str, str]:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict): raise ValueError("术语表必须是 JSON 对象")
        return {str(key).strip(): str(value).strip() for key, value in data.items()
                if str(key).strip() and str(value).strip()}

    def show_glossary_editor(self):
        window = tk.Toplevel(self); window.title("术语表编辑器"); window.geometry("720x520")
        frame = ttk.Frame(window, padding=10); frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(frame, columns=("source", "target"), show="headings", selectmode="extended")
        tree.heading("source", text="英文术语"); tree.heading("target", text="固定中文译法")
        tree.column("source", width=290); tree.column("target", width=290)
        bar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview); tree.configure(yscrollcommand=bar.set)
        tree.grid(row=0, column=0, sticky="nsew"); bar.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1)
        terms = {}
        current = self.vars["glossary"].get()
        if current and Path(current).exists():
            try: terms.update(self.load_glossary(current))
            except Exception as exc: messagebox.showerror("术语表读取失败", str(exc), parent=window)
        def refresh():
            tree.delete(*tree.get_children())
            for index, (source, target) in enumerate(sorted(terms.items(), key=lambda item:item[0].lower())):
                tree.insert("", "end", iid=str(index), values=(source, target))
        def add_or_edit(edit=False):
            source0=target0=""
            if edit:
                selected=tree.selection()
                if len(selected)!=1: messagebox.showinfo("编辑术语", "请选择一条术语", parent=window); return
                source0,target0=tree.item(selected[0],"values")
            source=simpledialog.askstring("英文术语", "输入英文术语：", initialvalue=source0, parent=window)
            if source is None or not source.strip(): return
            target=simpledialog.askstring("中文译法", f"{source.strip()} 的固定中文译法：", initialvalue=target0, parent=window)
            if target is None or not target.strip(): return
            if edit and source0 != source.strip(): terms.pop(source0, None)
            terms[source.strip()]=target.strip(); refresh()
        def remove():
            for item in tree.selection(): terms.pop(tree.item(item,"values")[0],None)
            refresh()
        def import_file():
            path=filedialog.askopenfilename(parent=window,filetypes=[("JSON","*.json")])
            if path:
                try: terms.update(self.load_glossary(path)); refresh()
                except Exception as exc: messagebox.showerror("导入失败",str(exc),parent=window)
        def add_template():
            template={"Mana":"魔力","Source":"源力","Chunk":"区块","Quest":"任务","Spell":"法术",
                      "Storage":"存储","Energy":"能量","Fluid":"流体","Item":"物品","Block":"方块",
                      "Entity":"实体","Damage":"伤害","Cooldown":"冷却时间","Recipe":"配方","Upgrade":"升级"}
            for key,value in template.items(): terms.setdefault(key,value)
            refresh()
        def save_as(close=True):
            default = Path(current) if current else self.config_path.parent / "glossary.json"
            path=filedialog.asksaveasfilename(parent=window,defaultextension=".json",initialdir=str(default.parent),
                                               initialfile=default.name,filetypes=[("JSON","*.json")])
            if not path:return
            Path(path).write_text(json.dumps(terms,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            self.vars["glossary"].set(path); self.save_api_config(False)
            if close: window.destroy()
        buttons=ttk.Frame(window,padding=(10,0,10,10)); buttons.pack(fill="x")
        for label,command in (("新增",lambda:add_or_edit(False)),("编辑",lambda:add_or_edit(True)),("删除",remove),
                              ("常用模板",add_template),("导入 JSON",import_file),("导出并使用",save_as)):
            ttk.Button(buttons,text=label,command=command).pack(side="left",padx=4)
        tree.bind("<Double-1>",lambda _event:add_or_edit(True)); refresh()

    def show_scopes(self):
        window = tk.Toplevel(self); window.title("翻译范围"); window.resizable(False, False)
        labels = {"locale":"模组语言文件", "patchouli":"Patchouli 指南书", "quests":"任务书/SNBT",
                  "scripts":"KubeJS/FancyMenu", "config":"普通配置说明", "serverconfig":"存档 serverconfig",
                  "shaders":"光影包语言"}
        for row, (key, label) in enumerate(labels.items()):
            ttk.Checkbutton(window, text=label, variable=self.scope_vars[key]).grid(row=row, column=0, sticky="w", padx=16, pady=6)
        ttk.Button(window, text="保存", command=lambda:(self.save_api_config(False), window.destroy())).grid(row=len(labels), column=0, pady=12)

    @staticmethod
    def _version_key(path: Path) -> tuple[int, ...]:
        match = re.search(r"(?<!\d)(\d+(?:\.\d+){1,2})(?!\d)", path.stem)
        return tuple(map(int, match.group(1).split("."))) if match else ()

    @classmethod
    def _matching_pack(cls, packs: list[Path], source: str) -> Path | None:
        versions = {tuple(map(int, version.split(".")))
                    for version in re.findall(r"(?<!\d)(\d+(?:\.\d+){1,2})(?!\d)", source)}
        return next((path for path in reversed(packs) if cls._version_key(path) in versions), None)

    def autodetect_resources(self, match_source: bool = False):
        package_root = Path(__file__).resolve().parents[2]
        roots = []
        for candidate in (Path.cwd(), Path.cwd().parent, package_root, package_root.parent):
            candidate = candidate.resolve()
            if candidate not in roots: roots.append(candidate)

        detected = []
        packs = []
        for root in roots:
            folder = root / "chinese_i18n"
            if folder.is_dir(): packs.extend(folder.glob("*.zip"))
        packs = sorted(set(path.resolve() for path in packs), key=self._version_key)
        if packs and (not self.vars["memory"].get() or match_source):
            selected = None
            selected = self._matching_pack(packs, self.vars["source"].get())
            if selected is None and not match_source: selected = packs[-1]
            if selected:
                self.vars["memory"].set(str(selected)); detected.append(f"中文资源包：{selected}")

        if detected:
            self.log.insert("end", "已自动识别\n" + "\n".join(detected) + "\n\n")

    def load_models(self):
        if not self.vars["api_key"].get().strip():
            messagebox.showerror("缺少 API Key", "请先填写 API Key")
            return
        self.save_api_config(notify=False)
        self.models_button.config(state="disabled", text="读取中…")
        config = {key: self.vars[key].get().strip() for key in ("api_key", "base_url", "model")}
        threading.Thread(target=self.models_worker, args=(config,), daemon=True).start()

    def models_worker(self, config):
        try:
            client = OpenAITranslator(config["api_key"], config["base_url"], config["model"])
            self.messages.put(("models", client.list_models()))
        except Exception as exc:
            self.messages.put(("models_error", f"{type(exc).__name__}: {exc}"))

    def show_cache(self):
        window = tk.Toplevel(self)
        window.title("本地翻译缓存")
        window.geometry("1050x620")
        toolbar = ttk.Frame(window, padding=10); toolbar.pack(fill="x")
        search_var = tk.StringVar()
        ttk.Label(toolbar, text="搜索").pack(side="left")
        search_entry = ttk.Entry(toolbar, textvariable=search_var, width=45)
        search_entry.pack(side="left", fill="x", expand=True, padx=8)
        count_var = tk.StringVar(value="正在读取…")
        ttk.Label(toolbar, textvariable=count_var).pack(side="right", padx=(10, 0))

        table_frame = ttk.Frame(window, padding=(10, 0, 10, 10)); table_frame.pack(fill="both", expand=True)
        tree = ttk.Treeview(table_frame, columns=("locked", "model", "source", "target"), show="headings")
        tree.heading("locked", text="锁定")
        tree.heading("model", text="模型")
        tree.heading("source", text="原文")
        tree.heading("target", text="译文")
        tree.column("locked", width=55, minwidth=45, anchor="center")
        tree.column("model", width=150, minwidth=90)
        tree.column("source", width=390, minwidth=150)
        tree.column("target", width=390, minwidth=150)
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.grid(row=0, column=0, sticky="nsew"); ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1); table_frame.columnconfigure(0, weight=1)

        cache = TranslationCache(self.config_path.parent / "translation_cache.db")
        page = {"index": 0, "size": 500, "total": 0}
        def refresh(*_):
            tree.delete(*tree.get_children())
            search = search_var.get().strip(); page["total"] = cache.count(search)
            max_page = max(0, (page["total"] - 1) // page["size"])
            page["index"] = min(page["index"], max_page)
            rows = cache.detailed_entries(search, page["size"], page["index"] * page["size"])
            for cache_key, model, source, target, locked in rows:
                tree.insert("", "end", iid=cache_key, values=("是" if locked else "", model or "（旧缓存，模型未知）",
                                                               source or "（旧缓存未保存原文）", target))
            count_var.set(f"共 {page['total']} 条 · 第 {page['index']+1}/{max_page+1} 页 · 本页 {len(rows)} 条")
            previous_button.config(state="normal" if page["index"] > 0 else "disabled")
            next_button.config(state="normal" if page["index"] < max_page else "disabled")
        ttk.Button(toolbar, text="刷新", command=refresh).pack(side="left")
        def change_page(delta): page["index"] += delta; refresh()
        previous_button=ttk.Button(toolbar,text="上一页",command=lambda:change_page(-1)); previous_button.pack(side="left",padx=(8,0))
        next_button=ttk.Button(toolbar,text="下一页",command=lambda:change_page(1)); next_button.pack(side="left",padx=(8,0))
        def edit_selected():
            selected = tree.selection()
            if len(selected) != 1:
                messagebox.showinfo("编辑缓存", "请先选择一条缓存记录", parent=window); return
            current = tree.item(selected[0], "values")[3]
            value = simpledialog.askstring("编辑译文", "修改缓存译文：", initialvalue=current, parent=window)
            if value is not None:
                try: cache.update(selected[0], value); refresh()
                except ValueError as exc: messagebox.showerror("保存失败", str(exc), parent=window)
        def delete_selected():
            selected = list(tree.selection())
            if not selected: messagebox.showinfo("删除缓存", "请先选择记录", parent=window); return
            if messagebox.askyesno("删除缓存", f"确定删除选中的 {len(selected)} 条缓存？", parent=window):
                deleted = cache.delete(selected); refresh()
                if deleted < len(selected): messagebox.showinfo("删除结果", f"已删除 {deleted} 条；锁定记录已保留。", parent=window)
        def backup_cache():
            target = filedialog.asksaveasfilename(parent=window, defaultextension=".db",
                                                  initialfile="translation_cache_backup.db",
                                                  filetypes=[("SQLite", "*.db")])
            if target:
                shutil.copy2(cache.path, target)
                messagebox.showinfo("备份完成", target, parent=window)
        def restore_cache():
            source = filedialog.askopenfilename(parent=window, filetypes=[("SQLite", "*.db")])
            if source and messagebox.askyesno("恢复缓存", "将用备份替换当前本地缓存，是否继续？", parent=window):
                shutil.copy2(source, cache.path); refresh()
        def lock_selected(locked):
            selected = list(tree.selection())
            if not selected:
                messagebox.showinfo("缓存锁定", "请先选择一条或多条记录", parent=window); return
            changed = cache.set_locked(selected, locked); refresh()
            count_var.set(f"已{'锁定' if locked else '解锁'} {changed} 条")
        def toggle_lock(event):
            if tree.identify_column(event.x) != "#1": return
            item = tree.identify_row(event.y)
            if item:
                locked = tree.item(item, "values")[0] == "是"
                cache.set_locked([item], not locked); refresh()
        ttk.Button(toolbar, text="编辑译文", command=edit_selected).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="删除选中", command=delete_selected).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="备份", command=backup_cache).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="恢复", command=restore_cache).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="锁定", command=lambda:lock_selected(True)).pack(side="left", padx=(8, 0))
        ttk.Button(toolbar, text="解锁", command=lambda:lock_selected(False)).pack(side="left", padx=(8, 0))
        search_entry.bind("<Return>", lambda _event:(page.update(index=0),refresh()))
        tree.bind("<Double-1>", toggle_lock)
        refresh()
        search_entry.focus_set()

    def start(self):
        if not self.vars["source"].get() or not self.vars["output"].get():
            messagebox.showerror("缺少路径", "请选择整合包和输出目录")
            return
        source_path = Path(self.vars["source"].get()).resolve()
        output_path = Path(self.vars["output"].get()).resolve()
        if source_path == output_path or (source_path.is_dir() and
                                          (output_path.is_relative_to(source_path) or source_path.is_relative_to(output_path))):
            messagebox.showerror("目录冲突", "输出目录必须是独立的平级目录，不能等于、包含或位于游戏实例目录内。")
            return
        try:
            scan_workers = int(self.vars["scan_workers"].get())
            process_workers = int(self.vars["process_workers"].get())
            api_workers = int(self.vars["api_workers"].get())
            if not 1 <= scan_workers <= 128 or not 1 <= process_workers <= 32 or not 1 <= api_workers <= 128: raise ValueError
        except ValueError:
            messagebox.showerror("并发设置错误", "扫描线程/API 并发须为 1–128，CPU 进程数须为 1–32")
            return
        self.save_api_config(notify=False)
        self.cancel_event.clear()
        self.last_result = None; self.quality_button.config(state="disabled"); self.install_button.config(state="disabled")
        self.cfpa_status_var.set("正在匹配当前游戏版本…")
        self.started_at = time.monotonic()
        self.last_event = "初始化生成任务"
        if self.debug_enabled.get():
            try:
                self.debug_dir.mkdir(parents=True, exist_ok=True)
                self.debug_log_path.write_text("", encoding="utf-8")
            except OSError: pass
        self.start_button.config(state="disabled"); self.stop_button.config(state="normal"); self.progress["value"] = 0
        self.status_var.set("运行中 · 0 秒")
        self.log.insert("end", f"\n[{time.strftime('%H:%M:%S')}] 开始生成（扫描线程 {scan_workers}，CPU 进程 {process_workers}，API 并发 {api_workers}）\n"); self.log.see("end")
        self.append_debug_log(f"开始生成（扫描线程 {scan_workers}，CPU 进程 {process_workers}，API 并发 {api_workers}）")
        self.write_debug_status("running")
        job = {key: self.vars[key].get() for key in self.vars}
        job["scopes"] = {key for key, value in self.scope_vars.items() if value.get()}
        threading.Thread(target=self.worker, args=(job,), daemon=True).start()
        self.update_elapsed()

    def start_custom_translation(self):
        if not self.vars["api_key"].get().strip():
            messagebox.showerror("缺少 API Key", "自定义翻译必须配置 API Key，且不会读取或写入本地缓存。")
            return
        source = filedialog.askopenfilename(title="选择自定义翻译文件",
                                            filetypes=[("支持的文件", "*.json *.txt"), ("JSON", "*.json"), ("文本", "*.txt")])
        if not source:
            return
        source_path = Path(source)
        output = filedialog.asksaveasfilename(
            title="保存翻译结果", defaultextension=source_path.suffix, initialdir=str(source_path.parent),
            initialfile=source_path.stem + "-已翻译" + source_path.suffix,
            filetypes=[("原格式", "*" + source_path.suffix), ("所有文件", "*.*")])
        if not output:
            return
        try:
            api_workers = int(self.vars["api_workers"].get())
            if not 1 <= api_workers <= 128:
                raise ValueError
        except ValueError:
            messagebox.showerror("并发设置错误", "API 并发须为 1–128")
            return
        self.save_api_config(False)
        self.started_at = time.monotonic(); self.progress["value"] = 0
        self.start_button.config(state="disabled"); self.custom_button.config(state="disabled")
        self.status_var.set("自定义翻译 · 0 秒")
        self.log.insert("end", f"\n[{time.strftime('%H:%M:%S')}] 自定义翻译开始：{source}\n")
        job = {key: self.vars[key].get() for key in ("api_key", "base_url", "model", "reasoning_effort", "glossary")}
        job.update({"source": source, "output": output, "api_workers": api_workers})
        threading.Thread(target=self.custom_translation_worker, args=(job,), daemon=True).start()
        self.update_elapsed()

    def custom_translation_worker(self, job):
        try:
            glossary = self.load_glossary(job["glossary"]) if job["glossary"] else {}
            prompt = ("Translate Japanese or English RPG Maker game text into natural Simplified Chinese. "
                      "Preserve control codes, placeholders, escapes, punctuation intent, and character names consistently. "
                      "Return only one JSON object whose keys are the exact numeric IDs from the input and whose values "
                      "are translated strings. Do not omit, merge, split, or reorder IDs.")
            # cache=None is intentional: custom files must never read or pollute Minecraft translation cache.
            translator = OpenAITranslator(job["api_key"], job["base_url"], job["model"], cache=None,
                                          api_workers=job["api_workers"],
                                          reasoning_effort=job["reasoning_effort"], glossary=glossary,
                                          system_prompt=prompt)
            report = lambda text: self.messages.put(("custom_progress", text))
            handler = translate_dictionary_file if Path(job["source"]).suffix.lower() == ".json" else translate_text_file
            result = handler(job["source"], job["output"], translator,
                             workers=job["api_workers"], progress=report)
            self.messages.put(("custom_done", json.dumps(result, ensure_ascii=False, indent=2)))
        except Exception as exc:
            self.messages.put(("custom_error", f"{type(exc).__name__}: {exc}"))

    def estimate(self):
        source = self.vars["source"].get().strip()
        if not source or not Path(source).exists():
            messagebox.showerror("缺少路径", "请先选择有效的整合包/版本目录"); return
        self.estimate_button.config(state="disabled", text="预估中…")
        scopes = {key for key, value in self.scope_vars.items() if value.get()}
        output = self.vars["output"].get() or str(self.config_path.parent / "estimate-output")
        threading.Thread(target=self.estimate_worker, args=(source, output, scopes), daemon=True).start()

    def estimate_worker(self, source: str, output: str, scopes: set[str]):
        try:
            generator = PatchGenerator(source, output)
            files = list(generator._walk_files(Path(source))) if Path(source).is_dir() else [Path(source)]
            archives = sum(path.suffix.lower() in {".jar", ".zip"} and not generator._is_runtime_archive(path)
                           for path in files)
            config_suffixes = {".toml", ".cfg", ".properties", ".json5", ".yaml", ".yml", ".snbt", ".json"}
            counts = {key: 0 for key in ("locale", "patchouli", "quests", "scripts", "config", "serverconfig", "shaders")}
            root = Path(source)
            for path in files:
                try: parts = [part.lower() for part in path.relative_to(root).parts]
                except ValueError: parts = [part.lower() for part in path.parts]
                suffix = path.suffix.lower()
                if path.name.lower() in {"en_us.json", "en_us.lang"}: counts["locale"] += 1
                if "patchouli_books" in parts and "en_us" in parts and suffix == ".json": counts["patchouli"] += 1
                if suffix in {".snbt", ".nbt"} and any(name in parts for name in ("ftbquests", "quests")): counts["quests"] += 1
                if suffix in {".js", ".txt"} and any(name in parts for name in ("kubejs", "fancymenu")): counts["scripts"] += 1
                if parts and parts[0] in {"config", "defaultconfigs"} and suffix in config_suffixes: counts["config"] += 1
                if parts and parts[0] == "saves" and "serverconfig" in parts and suffix in config_suffixes: counts["serverconfig"] += 1
            shader_root = root / "shaderpacks"
            if shader_root.is_dir():
                for pack in shader_root.iterdir():
                    if pack.is_dir():
                        counts["shaders"] += sum(path.name.lower() in {"en_us.lang", "en_us.json"}
                                                  for path in pack.rglob("*") if path.is_file())
                    elif pack.suffix.lower() == ".zip":
                        try:
                            with zipfile.ZipFile(pack) as archive:
                                counts["shaders"] += sum(Path(name).name.lower() in {"en_us.lang", "en_us.json"}
                                                          for name in archive.namelist())
                        except (OSError, zipfile.BadZipFile): pass
            enabled = {key: value for key, value in counts.items() if key in scopes}
            self.messages.put(("estimate", {"files": len(files), "archives": archives,
                                             "counts": counts, "enabled": enabled, "scopes": sorted(scopes)}))
        except Exception as exc:
            self.messages.put(("estimate_error", f"{type(exc).__name__}: {exc}"))

    def stop(self):
        if str(self.start_button["state"]) != "disabled": return
        self.cancel_event.set()
        self.stop_button.config(state="disabled")
        self.status_var.set("正在安全停止…")
        self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] 已请求停止；正在运行的请求结束后保留旧成品\n")
        self.log.see("end")

    def show_quality(self):
        if not self.last_result: return
        report = self.last_result.get("quality_report", {})
        text = json.dumps(report, ensure_ascii=False, indent=2)
        window = tk.Toplevel(self); window.title("质量报告"); window.geometry("620x430")
        box = tk.Text(window, font=("Microsoft YaHei UI", 10)); box.pack(fill="both", expand=True, padx=10, pady=10)
        box.insert("1.0", text); box.config(state="disabled")

    def load_release_from_output(self) -> bool:
        output_text = self.vars["output"].get().strip()
        if not output_text: return False
        output = Path(output_text)
        manifest_path = output / "发布清单.json"
        quality_path = output / "质量报告.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            quality = json.loads(quality_path.read_text(encoding="utf-8"))
            pack_names = manifest.get("资源包", [])
            install_files = manifest.get("安装文件", [])
            if not isinstance(pack_names, list) or not isinstance(install_files, list): raise ValueError("发布清单格式错误")
            packs = [output / Path(name) for name in pack_names]
            if not all(path.is_dir() for path in packs): raise FileNotFoundError("资源包目录不完整")
            if not install_files: raise FileNotFoundError("发布清单中没有可安装文件")
            for name in install_files:
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or not (output / relative).is_file():
                    raise ValueError(f"发布文件无效或缺失：{name}")
            self.last_result = {"resource_packs": [str(path) for path in packs],
                                "instance_overlay": str(output), "install_files": install_files,
                                "quality_report": quality}
            self.quality_button.config(state="normal")
            self.install_button.config(state="normal" if quality.get("是否可用") else "disabled")
            self.log.insert("end", f"已识别可安装成品：{output}\n")
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.last_result = None
            self.quality_button.config(state="disabled")
            self.install_button.config(state="disabled")
            return False

    def install_release(self):
        if not self.load_release_from_output():
            messagebox.showerror("无法安装", "输出目录中没有完整的可安装成品，请先完成生成"); return
        instance = Path(self.vars["source"].get())
        if not instance.is_dir(): messagebox.showerror("无法安装", "源路径不是游戏实例目录"); return
        if not messagebox.askyesno("一键安装", "将备份被覆盖文件、安装并启用资源包，同时应用实例覆盖文件。是否继续？"): return
        backup = None
        try:
            output = Path(self.vars["output"].get())
            backup_dir = self.config_path.parent / "安装备份"
            backup_dir.mkdir(parents=True, exist_ok=True)
            safe_instance = re.sub(r'[^\w.-]+', '_', instance.name) or "Minecraft实例"
            backup = backup_dir / f"{safe_instance}-汉化安装备份-{time.strftime('%Y%m%d-%H%M%S')}.zip"
            packs = [Path(path) for path in self.last_result.get("resource_packs", [])]
            install_files = [Path(name) for name in self.last_result.get("install_files", [])]
            new_files = []
            with zipfile.ZipFile(backup, "w", zipfile.ZIP_DEFLATED) as archive:
                for relative in install_files:
                    existing = instance / relative
                    if existing.is_file(): archive.write(existing, relative.as_posix())
                    elif not existing.exists(): new_files.append(relative.as_posix())
                options = instance / "options.txt"
                if options.exists(): archive.write(options, "options.txt")
                archive.writestr("__new_files__.txt", "\n".join(new_files))
            output = Path(self.vars["output"].get())
            for relative in install_files:
                target = instance / relative; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(output / relative, target)
            options = instance / "options.txt"
            if options.exists() and packs:
                lines = []
                for line in options.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("resourcePacks:"):
                        for pack in packs:
                            token = f'"file/{pack.name}"'
                            if token not in line: line = line.rstrip().rstrip("]") + ("," if not line.rstrip().endswith("[") else "") + token + "]"
                    lines.append(line)
                options.write_text("\n".join(lines) + "\n", encoding="utf-8")
            messagebox.showinfo("安装完成", f"已安装到：\n{instance}\n\n备份：\n{backup}")
        except Exception as exc:
            rollback = ""
            if backup and backup.exists():
                try: self._restore_install_backup(instance, backup); rollback = "\n已自动恢复安装前状态。"
                except Exception as rollback_error: rollback = f"\n自动恢复失败：{rollback_error}，请手动使用备份。"
            messagebox.showerror("安装失败", f"{type(exc).__name__}: {exc}{rollback}")

    @staticmethod
    def _restore_install_backup(instance: Path, backup: str | Path):
        with zipfile.ZipFile(backup) as archive:
            names = set(archive.namelist())
            new_files = archive.read("__new_files__.txt").decode("utf-8").splitlines() if "__new_files__.txt" in names else []
            for name in new_files:
                relative = Path(name)
                if not relative.is_absolute() and ".." not in relative.parts:
                    target = instance / relative
                    if target.is_file(): target.unlink()
            for name in names - {"__new_files__.txt"}:
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts: continue
                target = instance / relative; target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(name) as src, target.open("wb") as dst: shutil.copyfileobj(src, dst)

    def restore_install(self):
        instance = Path(self.vars["source"].get())
        if not instance.is_dir(): messagebox.showerror("无法恢复", "源路径不是游戏实例目录"); return
        backup_dir = self.config_path.parent / "安装备份"
        backup = filedialog.askopenfilename(title="选择汉化安装备份", initialdir=str(backup_dir),
                                            filetypes=[("ZIP", "*.zip")])
        if not backup or not messagebox.askyesno("恢复安装", "将恢复备份并删除安装时新增的文件，是否继续？"): return
        try:
            self._restore_install_backup(instance, backup)
            messagebox.showinfo("恢复完成", f"已从备份恢复：\n{backup}")
        except Exception as exc:
            messagebox.showerror("恢复失败", f"{type(exc).__name__}: {exc}")

    def update_elapsed(self):
        if str(self.start_button["state"]) == "disabled":
            elapsed = int(time.monotonic() - self.started_at)
            percent = float(self.progress["value"])
            if percent >= 1:
                remaining = max(0, int(elapsed * (100 - percent) / percent))
                self.status_var.set(f"{percent:.0f}% · 剩余约 {remaining} 秒")
            else:
                self.status_var.set(f"运行中 · {elapsed} 秒")
            self.write_debug_status("running")
            self.after(1000, self.update_elapsed)

    def update_progress_value(self, message: str):
        rules = (("自定义翻译进度", 5, 98), ("文本收集进度", 0, 25), ("全局翻译进度", 25, 55),
                 ("结构化收集", 55, 65), ("结构化翻译进度", 65, 90),
                 ("写回结构化文件", 90, 97))
        for marker, start, end in rules:
            if marker in message:
                match = re.search(r"(\d+)\s*/\s*(\d+)", message)
                if match and int(match.group(2)):
                    self.progress["value"] = start + (end - start) * int(match.group(1)) / int(match.group(2))
                return
        if "正在写入资源包" in message: self.progress["value"] = 98
        elif "生成完成" in message: self.progress["value"] = 100

    def worker(self, job):
        translator = IdentityTranslator()
        batching = None
        try:
            report = lambda text: self.messages.put(("progress", text))
            if job["api_key"]:
                glossary = {}
                if job["glossary"]:
                    glossary = self.load_glossary(job["glossary"])
                client = OpenAITranslator(
                    job["api_key"], job["base_url"], job["model"],
                    TranslationCache(self.config_path.parent / "translation_cache.db"),
                    api_workers=int(job["api_workers"]),
                    reasoning_effort=job["reasoning_effort"], glossary=glossary)
                batching = CoalescingTranslator(client, int(job["api_workers"]), progress=report)
                translator = batching
            memories = []
            if job["memory"]: memories.append(ResourcePackMemory([job["memory"]]))
            probe = PatchGenerator(job["source"], job["output"], IdentityTranslator(),
                                   finalize_output=False, scopes=job["scopes"])
            try:
                cfpa_pack = ensure_cfpa_pack(probe._minecraft_version(), job["source"],
                                             self.config_path.parent / "cfpa_cache", report=report)
                if cfpa_pack:
                    memories.append(ResourcePackMemory([cfpa_pack]))
                    self.messages.put(("cfpa_pack", str(cfpa_pack)))
                else:
                    self.messages.put(("cfpa_none", "未找到匹配当前版本的 CFPA 中文资源包"))
            except Exception as exc:
                self.messages.put(("cfpa_error", f"获取失败：{type(exc).__name__}: {exc}"))
                report(f"CFPA 中文资源包获取失败，将继续使用本地资源和 API：{type(exc).__name__}: {exc}")
            memory = CompositeMemory(*memories) if memories else None
            result = PatchGenerator(job["source"], job["output"], translator,
                                    memory, report, int(job["scan_workers"]),
                                    int(job["process_workers"]), finalize_output=True,
                                    cancel_event=self.cancel_event,
                                    scopes=job["scopes"]).generate()
            self.messages.put(("done", json.dumps(result.__dict__, ensure_ascii=False, indent=2)))
        except Exception as exc:
            self.messages.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            if batching: batching.close()

    def flush(self):
        try:
            while True:
                kind, text = self.messages.get_nowait()
                if kind == "custom_progress":
                    self.last_event = text; self.append_debug_log(text)
                    self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n"); self.log.see("end")
                    self.update_progress_value(text)
                    continue
                if kind in {"custom_done", "custom_error"}:
                    success = kind == "custom_done"
                    self.progress["value"] = 100 if success else self.progress["value"]
                    self.start_button.config(state="normal"); self.custom_button.config(state="normal")
                    self.status_var.set("自定义翻译完成" if success else "自定义翻译失败")
                    self.log.insert("end", text + "\n"); self.log.see("end")
                    messagebox.showinfo("自定义翻译完成" if success else "自定义翻译失败", text)
                    continue
                if kind == "cfpa_pack":
                    self.cfpa_status_var.set(text)
                    message = f"自动 CFPA 资源包已参与本次翻译：{text}"
                    self.log.insert("end", message + "\n"); self.log.see("end")
                    self.append_debug_log(message)
                    continue
                if kind in {"cfpa_none", "cfpa_error"}:
                    self.cfpa_status_var.set(text)
                    self.log.insert("end", f"自动 CFPA：{text}\n"); self.log.see("end")
                    continue
                if kind == "models":
                    self.model_box["values"] = text
                    if self.vars["model"].get() not in text: self.vars["model"].set(text[0])
                    self.models_button.config(state="normal", text="读取模型")
                    self.log.insert("end", f"已读取 {len(text)} 个可用模型\n"); self.log.see("end")
                    continue
                if kind == "models_error":
                    self.models_button.config(state="normal", text="读取模型")
                    self.log.insert("end", text + "\n"); self.log.see("end")
                    messagebox.showerror("读取模型失败", text)
                    continue
                if kind == "estimate":
                    self.estimate_button.config(state="normal", text="扫描预估")
                    labels = {"locale":"语言文件", "patchouli":"指南书", "quests":"任务书",
                              "scripts":"脚本菜单", "config":"普通配置", "serverconfig":"存档配置",
                              "shaders":"光影语言"}
                    all_items = "，".join(f"{labels[key]} {value}" for key, value in text["counts"].items())
                    enabled_items = "，".join(f"{labels[key]} {value}" for key, value in text["enabled"].items()) or "无"
                    summary = (f"扫描预估（不调用 API）\n\n完整发现：文件 {text['files']}，压缩包 {text['archives']}\n"
                               f"{all_items}\n\n本次启用：{enabled_items}")
                    self.log.insert("end", summary + "\n"); self.log.see("end")
                    messagebox.showinfo("扫描预估", summary)
                    continue
                if kind == "estimate_error":
                    self.estimate_button.config(state="normal", text="扫描预估")
                    messagebox.showerror("扫描预估失败", text)
                    continue
                if kind == "progress":
                    self.last_event = text
                    self.append_debug_log(text)
                    self.write_debug_status("running")
                    self.log.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n"); self.log.see("end")
                    self.update_progress_value(text)
                    continue
                self.log.insert("end", text + "\n"); self.log.see("end")
                if kind in {"done", "error"}:
                    self.progress["value"] = 100 if kind == "done" else self.progress["value"]
                    self.start_button.config(state="normal"); self.stop_button.config(state="disabled")
                    elapsed = int(time.monotonic() - self.started_at)
                    self.status_var.set(("已完成" if kind == "done" else "失败") + f" · {elapsed} 秒")
                    self.last_event = "生成完成" if kind == "done" else text
                    self.append_debug_log(self.last_event)
                    try: result_data = json.loads(text) if kind == "done" else {"error": text}
                    except json.JSONDecodeError: result_data = {"message": text}
                    self.write_debug_status("completed" if kind == "done" else "error", result_data)
                    if kind == "done":
                        self.last_result = result_data; self.quality_button.config(state="normal")
                        if result_data.get("quality_report", {}).get("是否可用"): self.install_button.config(state="normal")
                    messagebox.showinfo("完成" if kind == "done" else "错误", text)
        except queue.Empty: pass
        self.after(100, self.flush)


def main():
    App().mainloop()


if __name__ == "__main__": main()
