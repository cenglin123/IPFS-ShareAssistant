# src/utils/filecoin_pin_uploader.py

import os
import sys
import subprocess
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from datetime import datetime
import time
import hashlib
import re
import math
import queue
import random
from concurrent.futures import ThreadPoolExecutor, as_completed

from utils.config_utils import load_config_file, save_config_file






class _SubprocessManager:
    """本模块子进程管理器"""

    def __init__(self, logger=None):
        self.logger = logger
        self._lock = threading.RLock()
        # pid -> (popen, tag, task_id)
        self._procs = {}

    def _log(self, level: str, msg: str):
        try:
            if not self.logger:
                return
            fn = getattr(self.logger, level, None)
            if fn:
                fn(msg)
        except Exception:
            pass

    def spawn(self, cmd, *, cwd=None, tag="default", task_id="global", **popen_kwargs):
        """
        统一 Popen 入口：记录 pid，便于 stop/cleanup 精确终止本次任务产生的所有进程
        """
        # Windows 下建议新进程组，便于 terminate/kill；不新增依赖，保持最小
        if os.name == "nt":
            popen_kwargs.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)

        proc = subprocess.Popen(cmd, cwd=cwd, **popen_kwargs)
        with self._lock:
            self._procs[proc.pid] = (proc, tag, task_id)

        self._log("info", f"[proc] spawn pid={proc.pid} tag={tag} task={task_id} cwd={cwd}")
        return proc

    def cleanup_finished(self):
        """清理已退出的进程记录，避免表无限增长"""
        with self._lock:
            dead = [pid for pid, (p, _, _) in self._procs.items() if p.poll() is not None]
            for pid in dead:
                self._procs.pop(pid, None)

    def terminate_all(self, *, tag=None, task_id=None, kill_tree=True):
        """
        终止本模块记录的子进程（按 tag/task_id 过滤）
        kill_tree=True 时 Windows 用 taskkill /T /F，能处理并发残留最稳
        """
        self.cleanup_finished()
        with self._lock:
            items = list(self._procs.items())

        for pid, (proc, ptag, ptask) in items:
            if tag is not None and ptag != tag:
                continue
            if task_id is not None and ptask != task_id:
                continue
            if proc.poll() is not None:
                continue

            try:
                if os.name == "nt" and kill_tree:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        text=True
                    )
                    self._log("warning", f"[proc] taskkill pid={pid} tag={ptag} task={ptask}")
                else:
                    proc.terminate()
                    self._log("warning", f"[proc] terminate pid={pid} tag={ptag} task={ptask}")
            except Exception as e:
                self._log("warning", f"[proc] terminate failed pid={pid}: {e}")

        self.cleanup_finished()

    def wait_all(self, *, tag=None, task_id=None, timeout=10.0):
        """等待指定 tag/task_id 的所有子进程退出；超时返回 False"""
        deadline = time.time() + max(0.1, float(timeout))
        while time.time() < deadline:
            self.cleanup_finished()
            with self._lock:
                live = [
                    (pid, p) for pid, (p, ptag, ptask) in self._procs.items()
                    if (tag is None or ptag == tag)
                    and (task_id is None or ptask == task_id)
                    and p.poll() is None
                ]
            if not live:
                return True
            time.sleep(0.2)
        return False


class FilecoinPinUploader:
    """基于 filecoin-pin.exe 的分块上传器"""

    def __init__(self, master, app):
        self.master = master
        self.app = app
        self.logger = app.logger
        self.stop_flag = False
        self.current_process = None
        # ------------- 状态与配置 -------------
        self.work_root = Path(app.app_path) / "output" / "filecoin-pin"
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.log_lock = threading.Lock()

        self.source_var = tk.StringVar()
        self.private_key_var = tk.StringVar()
        self.show_key_var = tk.BooleanVar(value=False)
        self.chunk_size_var = tk.IntVar(value=100)      # MB
        self.threshold_var = tk.IntVar(value=200)       # MB
        self.thread_count_var = tk.IntVar(value=8)
        self.auto_cleanup_var = tk.BooleanVar(value=True)
        self.key_button_text = tk.StringVar(value="写入config文件")
        self.cid_var = tk.StringVar()
        self.status_var = tk.StringVar(value="准备就绪")
        self.thread_status_var = tk.StringVar(value="")
        self.thread_status_var2 = tk.StringVar(value="")
        self.file_seq_status_var = tk.StringVar(value="")
        self.network_var = tk.StringVar(value="mainnet")
        # 占位符控制开关（当前已取消占位符，但保持标志避免调用错误）
        self.placeholder_active = False
        self.log_entries = []
        self.log_filter_var = tk.StringVar(value="全部")
        self.cid_lines = []

        self.carbites_path = self._resolve_tool("carbites.exe" if os.name == "nt" else "carbites")
        self.filecoin_pin_path = self._resolve_tool("filecoin-pin.exe" if os.name == "nt" else "filecoin-pin")
        self.config_path = Path(self.app.app_path) / "config.json"
        self.use_source_dir = tk.BooleanVar(value=False)
        self.work_root = Path(self.app.app_path) / "output" / "filecoin-pin"
        self.work_root_var = tk.StringVar(value=str(self.work_root))

        # ------------- 初始化 -------------
        self._build_ui()
        self._load_key_from_config()
        self._load_thread_count()
        self._load_network_settings()
        self._load_workdir_settings()
        self._load_thread_count()

        # 子进程管理器
        self.proc_mgr = _SubprocessManager(logger=self.logger)
        self.current_task_id = None

    def _resolve_tool(self, name):
        path = Path(self.app.app_path) / "tools" / "fil" / name
        return path if path.exists() else None

    def _build_ui(self):
        """搭建上传助手的 UI"""
        container = ttk.Frame(self.master)
        container.pack(fill="both", expand=True, padx=10, pady=10)

        # 操作按钮样式（加大字体与高度）
        action_style = ttk.Style(self.master)
        action_style.configure("BigAction.TButton", font=("", 11, "bold"), padding=(8, 6))

        header = ttk.LabelFrame(
            container,
            text="Filecoin Pin 上传模块",
            style="BigTitle.TLabelframe"
        )
        header.pack(fill="x", padx=5, pady=(0, 8))
        ttk.Label(header, text="完整流程链路说明：导出 CAR → carbites 按需分块 → filecoin-pin 上传 → 完成后清理（仅本次任务）").pack(anchor="w", padx=8, pady=4)

        paned = ttk.PanedWindow(container, orient=tk.HORIZONTAL)
        paned.pack(fill="both", expand=True, pady=(8, 6))

        left = ttk.Frame(paned)
        right = ttk.Frame(paned)
        paned.add(left, weight=1)
        paned.add(right, weight=1)

        # 源文件/文件夹
        src_frame = ttk.LabelFrame(left, text="选择上传文件/文件夹", style="BigTitle.TLabelframe")
        src_frame.pack(fill="x", padx=5, pady=5)
        ttk.Label(src_frame, text="拖入/选择要上传的文件或文件夹（多行批量）").pack(anchor="w", padx=5, pady=(2, 0))
        src_text_frame = ttk.Frame(src_frame)
        src_text_frame.pack(fill="both", expand=True, padx=5, pady=2)
        v_scroll = ttk.Scrollbar(src_text_frame, orient="vertical")
        v_scroll.pack(side="right", fill="y")
        self.src_text = tk.Text(src_text_frame, height=5, width=60, yscrollcommand=v_scroll.set, wrap="none")
        self.src_text.pack(side="left", fill="both", expand=True)
        v_scroll.config(command=self.src_text.yview)
        try:
            self.src_text.drop_target_register("DND_Files")
            self.src_text.dnd_bind("<<Drop>>", self._on_drop_source)
        except Exception:
            pass
        btn_row = ttk.Frame(src_frame)
        btn_row.pack(fill="x", pady=4, padx=5)
        ttk.Button(btn_row, text="选文件", width=10, command=self.pick_file).pack(side="left", padx=2, pady=2)
        ttk.Button(btn_row, text="选文件夹", width=12, command=self.pick_dir).pack(side="left", padx=2, pady=2)
        ttk.Button(btn_row, text="清空", width=8, command=self._clear_sources).pack(side="left", padx=2, pady=2)

        # 根 CID 显示    
        info_frame = ttk.LabelFrame(left, text="分享 CID", style="BigTitle.TLabelframe")
        info_frame.pack(fill="x", padx=5, pady=(0, 5))
        cid_text_frame = ttk.Frame(info_frame)
        cid_text_frame.pack(fill="both", expand=True, padx=5, pady=4)
        cid_v_scroll = ttk.Scrollbar(cid_text_frame, orient="vertical")
        cid_v_scroll.pack(side="right", fill="y")
        self.cid_text_box = tk.Text(cid_text_frame, height=5, wrap="none", yscrollcommand=cid_v_scroll.set)
        self.cid_text_box.pack(side="left", fill="both", expand=True)
        cid_v_scroll.config(command=self.cid_text_box.yview)
        btn_row_cid = ttk.Frame(info_frame)
        btn_row_cid.pack(fill="x", padx=5, pady=(2, 4))
        ttk.Button(btn_row_cid, text="清空", width=8, command=self._clear_cid_text).pack(side="right", padx=4)
        ttk.Button(btn_row_cid, text="填写至主界面", width=14, command=self._fill_to_main).pack(side="right", padx=4)
        ttk.Button(btn_row_cid, text="复制CID", width=10, command=self._copy_cid).pack(side="right", padx=4)

        # 执行操作
        action_frame = ttk.Frame(left)
        action_frame.pack(fill="x", padx=5, pady=(4, 4))
        self.start_button = ttk.Button(action_frame, text="【一键上传】", width=12, command=self.start_upload, style="BigAction.TButton")
        self.start_button.pack(side="left", padx=2)
        self.stop_button = ttk.Button(action_frame, text="停止", width=10, command=self.stop_upload, state=tk.DISABLED, style="BigAction.TButton")
        self.stop_button.pack(side="left", padx=2)
        self.cleanup_button = ttk.Button(action_frame, text="清理临时文件", width=12, command=self.cleanup_all_temp, style="BigAction.TButton")
        self.cleanup_button.pack(side="left", padx=2)

        # 分块/上传分块操作
        action_frame2 = ttk.Frame(left)
        action_frame2.pack(fill="x", padx=5, pady=(4, 4))
        self.split_only_button = ttk.Button(action_frame2, text="CAR分块", width=12, command=self.start_split_only, style="BigAction.TButton")
        self.split_only_button.pack(side="left", padx=2)
        self.upload_parts_button = ttk.Button(action_frame2, text="上传分块或<200MB的小文件", width=26, command=self.start_upload_existing_parts, style="BigAction.TButton")
        self.upload_parts_button.pack(side="left", padx=2)

        # ============ 右侧：参数设置 + 执行日志 ============
        # 参数设置（移到右侧上方）
        car_frame = ttk.LabelFrame(right, text="参数设置", style="BigTitle.TLabelframe")
        car_frame.pack(fill="x", padx=5, pady=5)

        # 分块参数：阈值、大小、线程数
        row = ttk.Frame(car_frame)
        row.pack(fill="x", padx=5, pady=2)
        ttk.Label(row, text="分块阈值(MB):").pack(side="left")
        ttk.Entry(row, textvariable=self.threshold_var, width=6).pack(side="left", padx=(2, 8))
        ttk.Label(row, text="分块大小(MB):").pack(side="left")
        ttk.Entry(row, textvariable=self.chunk_size_var, width=6).pack(side="left", padx=(2, 8))
        ttk.Label(row, text="线程数(1-16):").pack(side="left")
        thread_spin = tk.Spinbox(row, from_=1, to=16, width=4, textvariable=self.thread_count_var, command=self._save_thread_count)
        thread_spin.pack(side="left", padx=2)

        # 网络选择与账户状态
        net_row = ttk.Frame(car_frame)
        net_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(net_row, text="网络:").pack(side="left")
        self.network_combo = ttk.Combobox(net_row, state="readonly", width=10, values=["mainnet", "calibration"], textvariable=self.network_var)
        self.network_combo.pack(side="left", padx=4)
        self.network_combo.bind("<<ComboboxSelected>>", lambda e: self._save_network_settings())
        self.payments_status_button = ttk.Button(net_row, text="查看账户状态", width=12, command=self._check_payments_status)
        self.payments_status_button.pack(side="left", padx=4)

        # Filecoin 私钥（默认不记录，若要记住请点击写入config文件）
        key_frame = ttk.Frame(car_frame)
        key_frame.pack(fill="x", padx=5, pady=2)
        ttk.Label(key_frame, text="私钥:").pack(side="left")
        self.key_entry = ttk.Entry(key_frame, textvariable=self.private_key_var, show="*", width=32)
        self.key_entry.pack(side="left", padx=2)
        self.key_entry.bind("<KeyRelease>", lambda _: self._update_key_button_state())
        ttk.Checkbutton(key_frame, text="显示私钥", variable=self.show_key_var, command=self._toggle_key_visibility).pack(side="left", padx=2)
        self.key_save_button = ttk.Button(key_frame, textvariable=self.key_button_text, width=12, command=self.save_key_to_config)
        self.key_save_button.pack(side="left", padx=2)

        # 临时文件目录设置
        workdir_row = ttk.Frame(car_frame)
        workdir_row.pack(fill="x", padx=5, pady=2)
        ttk.Label(workdir_row, text="临时目录:").pack(side="left")
        self.workdir_entry = ttk.Entry(workdir_row, textvariable=self.work_root_var, width=36)
        self.workdir_entry.pack(side="left", padx=2, fill="x", expand=True)
        ttk.Button(workdir_row, text="浏览", width=6, command=self._choose_work_dir).pack(side="left", padx=2)
        ttk.Button(workdir_row, text="打开", width=6, command=self._open_work_dir).pack(side="left", padx=2)

        # 复选框选项
        options_row = ttk.Frame(car_frame)
        options_row.pack(fill="x", padx=5, pady=(2, 4))
        ttk.Checkbutton(options_row, text="上传后自动清理临时文件", variable=self.auto_cleanup_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(options_row, text="使用原目录", variable=self.use_source_dir, command=self._save_workdir_settings).pack(side="left")

        # 日志区域（高度减半）
        log_frame = ttk.LabelFrame(right, text="执行日志")
        log_frame.pack(fill="both", expand=True, padx=5, pady=5)
        filter_row = ttk.Frame(log_frame)
        filter_row.pack(fill="x", padx=5, pady=(4, 0))
        ttk.Label(filter_row, text="查看线程:").pack(side="left")
        self.log_filter = ttk.Combobox(filter_row, textvariable=self.log_filter_var, state="readonly", width=10)
        self.log_filter.pack(side="left", padx=4)
        self.log_filter.bind("<<ComboboxSelected>>", lambda e: self._refresh_log_display())
        self._update_log_filter_options()
        v_scrollbar = ttk.Scrollbar(log_frame, orient="vertical")
        v_scrollbar.pack(side="right", fill="y")
        h_scrollbar = ttk.Scrollbar(log_frame, orient="horizontal")
        h_scrollbar.pack(side="bottom", fill="x")
        self.log_text = tk.Text(
            log_frame,
            wrap="none",
            height=10,
            yscrollcommand=v_scrollbar.set,
            xscrollcommand=h_scrollbar.set,
            state=tk.DISABLED
        )
        self.log_text.pack(fill="both", expand=True, padx=5, pady=5)
        v_scrollbar.config(command=self.log_text.yview)
        h_scrollbar.config(command=self.log_text.xview)

        # 状态栏
        self.progress = ttk.Progressbar(container, mode="determinate")
        self.progress.pack(fill="x", padx=5, pady=(4, 2))
        ttk.Label(container, textvariable=self.status_var).pack(anchor="w", padx=5)
        ttk.Label(container, textvariable=self.file_seq_status_var, foreground="#333").pack(anchor="w", padx=5)
        ttk.Label(container, textvariable=self.thread_status_var, foreground="#444").pack(anchor="w", padx=5)
        ttk.Label(container, textvariable=self.thread_status_var2, foreground="#444").pack(anchor="w", padx=5, pady=(0, 4))
        self._set_thread_status_idle()

    def pick_file(self):
        path = filedialog.askopenfilename(title="选择文件")
        if path:
            self._append_source_path(path)

    def pick_dir(self):
        path = filedialog.askdirectory(title="选择文件夹")
        if path:
            self._append_source_path(path)

    # ------------- 配置读取/保存 -------------
    def load_key_from_file(self):
        path = filedialog.askopenfilename(title="选择保存私钥的文本文件")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                key = fh.read().strip()
            self.private_key_var.set(key)
            self._append_log("已从文件读取私钥")
            self._update_key_button_state()
        except Exception as exc:
            messagebox.showerror("错误", f"读取私钥失败：{exc}")

    def save_key_to_config(self):
        key = self.private_key_var.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先填写 Filecoin 私钥")
            return
        data = load_config_file(str(self.config_path)) if self.config_path.exists() else {}
        data["filecoin_private_key"] = key
        save_config_file(str(self.config_path), data, self.app.logger)
        self._append_log("私钥已写入 config.json（仅本地）")

    def _load_key_from_config(self):
        try:
            if self.config_path.exists():
                data = load_config_file(str(self.config_path))
                if isinstance(data, dict) and data.get("filecoin_private_key"):
                    self.private_key_var.set(data["filecoin_private_key"])
                    self._append_log("已从 config.json 读取 Filecoin 私钥")
        except Exception:
            pass
        self._update_key_button_state()

    def _load_thread_count(self):
        try:
            if self.config_path.exists():
                data = load_config_file(str(self.config_path))
                if isinstance(data, dict) and data.get("filecoin_thread_count"):
                    val = int(data["filecoin_thread_count"])
                    if 1 <= val <= 16:
                        self.thread_count_var.set(val)
        except Exception:
            pass
        self._update_log_filter_options()
        self._set_thread_status_idle()

    def _load_workdir_settings(self):
        try:
            if self.config_path.exists():
                data = load_config_file(str(self.config_path))
                if isinstance(data, dict):
                    if data.get("filecoin_work_root"):
                        self.work_root_var.set(data["filecoin_work_root"])
                    if "filecoin_use_source_dir" in data:
                        self.use_source_dir.set(bool(data["filecoin_use_source_dir"]))
                    if data.get("filecoin_network") in ("mainnet", "calibration"):
                        self.network_var.set(data["filecoin_network"])
        except Exception:
            pass
        # 同步 work_root
        self.work_root = Path(self.work_root_var.get())
        self._save_workdir_settings()

    def _save_thread_count(self):
        try:
            val = int(self.thread_count_var.get())
        except Exception:
            val = 8
        val = max(1, min(16, val))
        self.thread_count_var.set(val)
        data = load_config_file(str(self.config_path)) if self.config_path.exists() else {}
        data["filecoin_thread_count"] = val
        save_config_file(str(self.config_path), data, self.app.logger)
        self._append_log(f"线程数已设置为 {val}", log_to_file=False)
        self._update_log_filter_options()
        self._set_thread_status_idle()

    def _get_chunk_and_threshold(self):
        try:
            chunk_val = int(self.chunk_size_var.get())
            threshold_val = int(self.threshold_var.get())
        except Exception:
            raise ValueError("分块参数错误")
        # 分块大小上限 200
        if chunk_val > 200:
            chunk_val = 200
            self.chunk_size_var.set(chunk_val)
        if chunk_val < 1:
            chunk_val = 1
            self.chunk_size_var.set(chunk_val)
        # 只有接近 200MB 临界值时才减 1MB，避免触发边界问题；其它情况按用户设置值执行
        effective_chunk = chunk_val - 1 if chunk_val >= 199 else chunk_val
        effective_chunk = max(10, effective_chunk)
        if threshold_val < 1:
            threshold_val = 1
            self.threshold_var.set(threshold_val)
        return effective_chunk, threshold_val

    def _save_workdir_settings(self):
        data = load_config_file(str(self.config_path)) if self.config_path.exists() else {}
        data["filecoin_work_root"] = self.work_root_var.get()
        data["filecoin_use_source_dir"] = self.use_source_dir.get()
        data["filecoin_network"] = self.network_var.get()
        save_config_file(str(self.config_path), data, self.app.logger)
        self.work_root = Path(self.work_root_var.get())
        self._set_thread_status_idle()

    def _load_network_settings(self):
        try:
            if self.config_path.exists():
                data = load_config_file(str(self.config_path))
                if isinstance(data, dict) and data.get("filecoin_network") in ("mainnet", "calibration"):
                    self.network_var.set(data["filecoin_network"])
        except Exception:
            pass
        try:
            if getattr(self, "network_combo", None):
                self.network_combo.set(self.network_var.get())
        except Exception:
            pass

    def _save_network_settings(self):
        data = load_config_file(str(self.config_path)) if self.config_path.exists() else {}
        val = self.network_var.get()
        if val not in ("mainnet", "calibration"):
            val = "mainnet"
            self.network_var.set(val)
        data["filecoin_network"] = val
        save_config_file(str(self.config_path), data, self.app.logger)
        try:
            if getattr(self, "network_combo", None):
                self.network_combo.set(val)
        except Exception:
            pass

    def _update_key_button_state(self):
        has_key = bool(self.private_key_var.get().strip())
        state = tk.NORMAL if has_key else tk.DISABLED
        self.key_button_text.set("写入config文件")
        try:
            self.key_save_button.config(state=state)
        except Exception:
            pass
        try:
            self.cleanup_button.config(state=tk.NORMAL)
        except Exception:
            pass

    def _toggle_key_visibility(self):
        self.key_entry.config(show="" if self.show_key_var.get() else "*")

    def _open_work_dir(self):
        try:
            self.work_root = Path(self.work_root_var.get())
            self.work_root.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(self.work_root)
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', str(self.work_root)])
        except Exception as exc:
            self._append_log(f"打开工作目录失败: {exc}")

    def _choose_work_dir(self):
        selected = filedialog.askdirectory(title="选择临时文件目录", initialdir=self.work_root_var.get())
        if selected:
            self.work_root_var.set(selected)
            self.work_root = Path(selected)
            self._save_workdir_settings()

    # ------------- 操作入口 -------------
    def _check_payments_status(self):
        key = self.private_key_var.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先填写 Filecoin 私钥")
            return
        if not self.filecoin_pin_path:
            messagebox.showerror("错误", "未找到 tools/fil/filecoin-pin.exe")
            return
        cmd = [
            str(self.filecoin_pin_path),
            "payments",
            "status",
            "--private-key",
            key,
        ] + self._network_args()
        def set_controls(enabled):
            state = tk.NORMAL if enabled else tk.DISABLED
            try:
                self.payments_status_button.config(state=state)
            except Exception:
                pass
            try:
                self.network_combo.config(state="readonly" if enabled else tk.DISABLED)
            except Exception:
                pass

        set_controls(False)

        def run_status():
            try:
                self._run_command(cmd, label="查询账户状态", log_to_file=True)
            finally:
                self.app._call_ui(lambda: set_controls(True))
        threading.Thread(target=run_status, daemon=True).start()

    def start_upload(self):
        if self.current_process or self.stop_flag:
            return

        paths = self._get_sources()
        key = self.private_key_var.get().strip()

        if not paths:
            messagebox.showwarning("提示", "请先选择至少一个文件或文件夹")
            return
        if not key:
            messagebox.showwarning("提示", "请填写 Filecoin 私钥")
            return
        if not self.filecoin_pin_path:
            messagebox.showerror("错误", "未找到 tools/fil/filecoin-pin.exe")
            return

        try:
            chunk_size, threshold = self._get_chunk_and_threshold()
        except ValueError:
            messagebox.showerror("错误", "分块大小/阈值必须为数字")
            return

        threading.Thread(
            target=self._run_batch_upload,
            args=(paths, key, chunk_size, threshold, True, self.auto_cleanup_var.get()),
            daemon=True
        ).start()

    def start_split_only(self):
        sources = self._get_sources()
        if not sources:
            messagebox.showwarning("提示", "请先选择文件或文件夹")
            return
        source = sources[0]
        if not os.path.exists(source):
            messagebox.showerror("错误", "路径不存在，请重新选择")
            return
        try:
            chunk_size, threshold = self._get_chunk_and_threshold()
        except ValueError:
            messagebox.showerror("错误", "分块大小/阈值必须为数字")
            return
        self._start_pipeline(Path(source), None, chunk_size, threshold, do_upload=False, do_cleanup=False)

    def start_upload_existing_parts(self):
        folder = filedialog.askdirectory(title="选择已分块或<200MB文件所在文件夹")
        if not folder:
            return
        key = self.private_key_var.get().strip()
        if not key:
            messagebox.showwarning("提示", "请先填写 Filecoin 私钥")
            return
        if not self.filecoin_pin_path:
            messagebox.showerror("错误", "未找到 tools/filecoin-pin.exe")
            return

        files = [p for p in Path(folder).iterdir() if p.is_file()]
        if not files:
            messagebox.showwarning("提示", "该文件夹没有可上传的文件")
            return

        oversize = [p for p in files if p.stat().st_size > 200 * 1024 * 1024]
        if oversize:
            names = "\n".join(p.name for p in oversize)
            messagebox.showwarning("提示", f"以下文件超过200MB，请使用\"一键上传\"单独处理：\n{names}")
            return

        self.stop_flag = False
        self._set_controls_active(False)
        self._update_progress(0)
        self._set_status("上传已存在的分块/小文件中...")
        threading.Thread(target=self._run_upload_existing_parts, args=(files, key), daemon=True).start()

    def stop_upload(self):
        self.stop_flag = True
        if self.current_process and self.current_process.poll() is None:
            try:
                self.proc_mgr.terminate_all(tag="filecoin-pin", task_id=self.current_task_id, kill_tree=True)
            except Exception:
                pass
        self._set_status("正在停止当前任务...")
        self._append_log("已请求停止")

    def shutdown(self, timeout=5.0):
        """程序退出时调用，尽量优雅终止所有上传进程（不触发 UI 操作）"""
        self.stop_flag = True
        try:
            self.proc_mgr.terminate_all(tag="filecoin-pin", task_id=self.current_task_id, kill_tree=True)
        except Exception:
            pass
        try:
            self.proc_mgr.wait_all(tag="filecoin-pin", task_id=self.current_task_id, timeout=timeout)
        except Exception:
            pass

    def _run_upload_existing_parts(self, files, private_key):
        total = len(files)
        try:
            success = self._upload_parts_concurrent(sorted(files), private_key)
            if not success:
                raise RuntimeError("部分文件上传失败")
            self._set_status("上传完成")
        except Exception as exc:
            self._append_log(f"[错误] {exc}")
            self._set_status(f"发生错误：{exc}")
        finally:
            self._update_progress(0)
            self.app._call_ui(lambda: self._set_controls_active(True))

    def _start_pipeline(self, source_path, private_key, chunk_size, threshold, do_upload=True, do_cleanup=True):
        if do_upload:
            if not private_key:
                messagebox.showwarning("提示", "请填写 Filecoin 私钥")
                return
            if not self.filecoin_pin_path:
                messagebox.showerror("错误", "未找到 tools/filecoin-pin.exe")
                return

        self.stop_flag = False
        self._set_controls_active(False)
        self._update_progress(0)
        self.app._call_ui(lambda: self.cid_var.set(""))
        self._append_log("==== 开始新的 Filecoin 任务 ====")
        self._set_status("开始执行")

        threading.Thread(
            target=self._run_upload,
            args=(source_path, private_key, chunk_size, threshold, do_upload, do_cleanup, True),
            daemon=True
        ).start()

    def _run_batch_upload(self, paths, private_key, chunk_size, threshold, do_upload=True, do_cleanup=True):
        self.stop_flag = False
        self._set_controls_active(False)
        self._update_progress(0)
        failed_paths = []
        for idx, p in enumerate(paths, 1):
            if self.stop_flag:
                break
            if not Path(p).exists():
                self._append_log(f"跳过不存在的路径: {p}", log_to_file=False)
                continue
            self._set_file_seq_status(idx, len(paths))
            self.app._call_ui(lambda: self.cid_var.set(""))
            self._append_log(f"==== 开始新的 Filecoin 任务 ({idx}/{len(paths)}) ====")
            ok = self._run_upload(Path(p), private_key, chunk_size, threshold, do_upload, do_cleanup, manage_ui=False)
            if not ok:
                failed_paths.append(p)
        self._update_progress(0)
        self._set_controls_active(True)
        self._set_file_seq_status(0, 0)
        # 上传成功后清除成功的路径，仅保留失败项
        def update_sources():
            if hasattr(self, "src_text"):
                self.src_text.delete("1.0", tk.END)
                if failed_paths:
                    self.src_text.insert(tk.END, "\n".join(failed_paths))
            else:
                self.source_var.set("\n".join(failed_paths))
        self.app._call_ui(update_sources)

    def _run_upload(self, source_path, private_key, chunk_size, threshold, do_upload, do_cleanup, manage_ui=True):
        created_files = []
        cid = None
        if not source_path.exists():
            raise FileNotFoundError(f"路径不存在: {source_path}")
        work_dir, base_root = self._prepare_work_dir(source_path)
        upload_success = False

        try:
            cid = self._ipfs_add(source_path)
            self._set_status("导出 CAR 中...")
            car_path = self._export_car(cid, work_dir, source_path)
            car_path, car_parts = self._maybe_split(car_path, chunk_size, threshold)
            created_files.append(car_path)
            # 仅记录实际需要上传的分块（不重复计入原始 CAR）
            created_files.extend([p for p in car_parts if p not in created_files])

            if do_upload:
                self._set_status(f"开始上传到 Filecoin... (并行最多{max(1, min(16, self.thread_count_var.get()))}个)")
                success = self._upload_parts_concurrent(car_parts, private_key)
                if not success:
                    raise RuntimeError("部分分块上传失败")
                # 确保 filecoin-pin 进程完全结束后再清理
                time.sleep(0.5)
                self._set_status("上传完成，准备清理")
                self._update_progress(95)
                upload_success = True

                # 上传成功后调用插件进行广播和 PIN
                if cid:
                    self._invoke_pin_plugin(cid)
            else:
                self._set_status("分块完成")
                self._update_progress(90)
        except Exception as exc:
            self._append_log(f"[错误] {exc}")
            self._set_status(f"发生错误：{exc}")
        finally:
            if do_cleanup and upload_success:
                try:
                    self._cleanup(cid, created_files, work_dir, base_root, remove_workdir=True)
                except Exception as exc:
                    self._append_log(f"清理阶段出现问题: {exc}")
            elif do_cleanup and not upload_success:
                self._append_log("上传未完成，跳过清理以保留调试数据", log_to_file=False)
            if manage_ui:
                self._update_progress(0)
                self.app._call_ui(lambda: self._set_controls_active(True))
        return upload_success

    # ------------- 清理与状态 -------------
    def cleanup_all_temp(self):
        """手动清理 filecoin-pin 工作目录下的所有临时文件/空目录"""
        try:
            removed = 0
            if self.work_root.exists():
                for child in list(self.work_root.glob("*")):
                    if self._force_remove_path(child):
                        removed += 1
                # 最后尝试删除根目录本身
                if self._force_remove_path(self.work_root):
                    removed += 1
            self._append_log(f"已清理临时文件 {removed} 项", log_to_file=False)
            self._set_status("临时文件清理完成")
        except Exception as exc:
            self._append_log(f"清理临时文件失败: {exc}", log_to_file=False)

    def _ipfs_add(self, source_path):
        self._set_status("添加到 IPFS...")
        cmd = [
            self.app.kubo.kubo_path,
            "--repo-dir", self.app.repo_path,
            "add", "-Q",
            "--cid-version=1",
            "--pin=false",
        ]
        # 如未勾选"使用Filecoin参数"，则使用默认 chunker；否则使用 1MB chunker
        try:
            use_filecoin = bool(getattr(self.app, "use_filecoin", None) and self.app.use_filecoin.get())
        except Exception:
            use_filecoin = True
        if use_filecoin:
            cmd.extend(["--chunker", "size-1048576"])
        if source_path.is_dir():
            cmd.append("-r")
        cmd.append(str(source_path))

        rc, output = self._run_command(cmd, f"添加到 IPFS: {source_path}")
        if rc != 0:
            raise RuntimeError("添加到 IPFS 失败")

        cid_line = [line for line in output.splitlines() if line.strip()]
        cid = cid_line[-1].strip() if cid_line else ""
        if not cid:
            raise RuntimeError("未能解析 CID")
        self.app._call_ui(lambda: self.cid_var.set(cid))
        self._add_cid_entry(cid)
        self._append_log(f"获得 CID: {cid}")
        self._update_progress(20)
        return cid

    def _export_car(self, cid, work_dir, source_path):
        raw_name = source_path.stem if source_path.is_file() else source_path.name or "export"
        safe_name = self._sanitize_name(raw_name)
        if safe_name != raw_name:
            self._append_log(f"长文件名已缩短: {raw_name} -> {safe_name}")
        # CAR 文件名不再重复长名称，直接使用编号
        car_path = work_dir / "000.car"
        self._append_log(f"导出 CAR -> {car_path}")
        args = self.app._get_subprocess_args()
        with open(car_path, "wb") as fh:
            proc = subprocess.Popen(
                [
                    self.app.kubo.kubo_path, "--repo-dir", self.app.repo_path,
                    "dag", "export", cid
                ],
                stdout=fh,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                **args
            )
            self.current_process = proc
            stderr = proc.communicate()[1]
            self.current_process = None
        if proc.returncode != 0:
            raise RuntimeError(f"导出 CAR 失败: {stderr or '未知错误'}")
        self._append_log(f"导出完成，大小 {self._format_size(car_path.stat().st_size)}")
        self._update_progress(35)
        return car_path

    def _maybe_split(self, car_path, chunk_size, threshold):
        size = car_path.stat().st_size
        self._append_log(f"CAR 文件大小：{self._format_size(size)}")
        threshold_bytes = threshold * 1024 * 1024
        size_mb = size / (1024 * 1024)
        threads = max(1, min(16, self.thread_count_var.get()))
        max_chunk = max(2, chunk_size)  # 已在输入阶段减 1MB 的上限

        # 计算每块大小，按线程均分后限制在 max_chunk 内
        desired_chunk = max(2, math.ceil(size_mb / threads))
        chunk_to_use = min(max_chunk, desired_chunk)

        # 小于阈值时如无需分块则直接返回
        if size <= threshold_bytes and chunk_to_use >= size_mb:
            return car_path, [car_path]

        # 路径过长时，将 CAR 移动到更短的 split 目录
        car_path = self._shorten_car_path(car_path)

        if not self.carbites_path:
            raise RuntimeError("CAR 大于阈值且未找到 carbites.exe，无法分块")

        cmd = [
            str(self.carbites_path),
            "split",
            str(car_path),
            "--size",
            f"{chunk_to_use}MB",
            "--strategy",
            "simple"
        ]
        label = f"按 {threads} 线程切分（约 {chunk_to_use}MB/块，封顶 {max_chunk}MB）"
        rc, _ = self._run_command(cmd, label)
        if rc != 0:
            raise RuntimeError("分块失败")

        def _part_sort_key(p: Path) -> int:
            """
            从 carbites 输出文件名中提取数字序号
            兼容：
            xxx-0.car / xxx-12.car
            0.car / 12.car（fallback 情况）
            """
            m = re.search(r"-(\d+)\.car$", p.name)
            if not m:
                m = re.search(r"(\d+)\.car$", p.name)
            return int(m.group(1)) if m else 10**12

        parts = list(car_path.parent.glob(f"{car_path.stem}-*.car"))
        if not parts:
            parts = list(car_path.parent.glob("*.car"))

        # 防止把原始 car_path 本身误捞进来（按理说通常不会，以防万一）
        parts = [p for p in parts if p.resolve() != car_path.resolve()]

        parts.sort(key=_part_sort_key)

        # 给分块文件按序重命名，宽度按分块数量自适应，确保短路径且顺序明确
        normalized_parts = []
        width = max(3, len(str(len(parts))))
        for idx, part in enumerate(parts, 1):
            target = part.with_name(f"{idx:0{width}d}.car")
            if part != target:
                try:
                    part.rename(target)
                    part = target
                except Exception:
                    pass
            normalized_parts.append(part)
        parts = normalized_parts

        if not parts:
            raise RuntimeError("未找到分块结果")

        try:
            car_path.unlink()
            self._append_log(f"已删除原始 CAR: {car_path}")
        except Exception:
            pass

        self._append_log(f"生成 {len(parts)} 个分块")
        self._update_progress(45)
        return car_path, parts

    def _shorten_car_path(self, car_path: Path) -> Path:
        """必要时将 CAR 文件移动到更短路径的 split 目录"""
        path_str = str(car_path)
        if len(path_str) <= 200:
            return car_path
        split_dir = car_path.parent / "split"
        split_dir.mkdir(parents=True, exist_ok=True)
        target = split_dir / car_path.name
        try:
            shutil.move(str(car_path), str(target))
            self._append_log(f"为避免路径过长，将 CAR 移动至: {target}")
            return target
        except Exception as exc:
            self._append_log(f"移动 CAR 到短路径失败: {exc}")
            return car_path

    def _ensure_runway(self, private_key):
        """
        上传前确保 Filecoin Pay 有足够的 runway（≥30天）
        
        此方法用于解决多线程并发上传时的 nonce 冲突问题：
        - 原因：--auto-fund 会在每个线程中独立检查并触发 deposit 交易
        - 解决：上传前单独执行一次检查/充值，然后并行上传时不带 --auto-fund
        
        优化：添加缓存机制，避免频繁检查阻塞上传流程
        """
        # 验证网络设置，确保使用正确的网络
        current_network = self.network_var.get()
        if current_network not in ("mainnet", "calibration"):
            self._append_log(f"警告：网络设置 '{current_network}' 无效，使用默认 mainnet")
            current_network = "mainnet"
            self.network_var.set(current_network)
            self._save_network_settings()

        # 测试网禁用所有 payments 相关逻辑（status/setup/auto-fund）
        if current_network == "calibration":
            if not hasattr(self, '_runway_cache'):
                self._runway_cache = {}
            cache_key = f"runway_check_{private_key[-8:]}_{current_network}"
            self._runway_cache[cache_key] = (time.time(), True)
            self._append_log(f"测试网（{current_network}）已禁用 payments/runway 检查与自动充值，跳过")
            return True
        
        # 缓存检查结果（5分钟有效）
        cache_key = f"runway_check_{private_key[-8:]}_{current_network}"
        current_time = time.time()
        
        # 检查缓存
        if hasattr(self, '_runway_cache'):
            cache_time, cache_result = self._runway_cache.get(cache_key, (0, None))
            if current_time - cache_time < 300:  # 5分钟缓存
                if cache_result is True:
                    self._append_log(f"使用缓存结果：runway 充足（{current_network}，缓存有效期内）")
                    return True
                elif cache_result is False:
                    self._append_log(f"使用缓存结果：runway 不足（{current_network}，缓存有效期内）")
                    return False
        
        self._append_log(f"检查 Filecoin Pay 余额与 runway（网络：{current_network}）...")
        
        try:
            # 1. 先获取当前状态（设置超时，避免长时间阻塞）
            cmd = [
                str(self.filecoin_pin_path),
                "payments", "status",
                "--private-key", private_key,
            ] + self._network_args()
            
            # 使用带超时的命令执行
            rc, output = self._run_command_with_timeout(cmd, timeout=30, log_to_file=False)
            if rc != 0:
                self._append_log(f"无法获取余额状态（网络：{current_network}），将使用缓存或继续尝试上传")
                # 如果有缓存，使用缓存结果
                if hasattr(self, '_runway_cache') and cache_key in self._runway_cache:
                    _, cache_result = self._runway_cache[cache_key]
                    return cache_result if cache_result is not None else True
                return True
            
            # 2. 快速解析 runway 天数（简化逻辑）
            runway_days = None
            import re
            # 快速查找 runway 信息
            for line in output.splitlines():
                if "Runway:" in line:
                    match = re.search(r"~?(\d+)\s*day", line, re.IGNORECASE)
                    if match:
                        runway_days = int(match.group(1))
                        break
            
            if runway_days is None:
                # 尝试其他格式
                for line in output.splitlines():
                    if "runway:" in line.lower():
                        match = re.search(r"~?(\d+)\s*day", line, re.IGNORECASE)
                        if match:
                            runway_days = int(match.group(1))
                            break
            
            if runway_days is None:
                self._append_log(f"无法解析 runway（网络：{current_network}），将使用缓存或继续尝试上传")
                # 缓存未知结果
                if not hasattr(self, '_runway_cache'):
                    self._runway_cache = {}
                self._runway_cache[cache_key] = (current_time, None)
                return True
            
            self._append_log(f"当前 runway（网络：{current_network}）: ~{runway_days} 天")
            
            # 3. 如果 runway < 30 天，执行充值
            if runway_days < 30:
                self._append_log(f"Runway 不足 30 天，执行自动充值（网络：{current_network}）...")
                
                fund_cmd = [
                    str(self.filecoin_pin_path),
                    "payments", "setup", "--auto",
                    "--private-key", private_key,
                ] + self._network_args()
                
                rc, _ = self._run_command_with_timeout(fund_cmd, timeout=60, label=f"自动充值（{current_network}）")
                
                if rc == 0:
                    self._append_log(f"充值成功（网络：{current_network}），等待链上交易确认（约 30 秒）...")
                    time.sleep(30)  # 等待 Filecoin 链上交易确认
                    # 充值成功后更新缓存
                    if not hasattr(self, '_runway_cache'):
                        self._runway_cache = {}
                    self._runway_cache[cache_key] = (current_time, True)
                    return True
                else:
                    self._append_log(f"充值失败（网络：{current_network}），将继续尝试上传（可能会遇到余额不足问题）")
                    # 充值失败也更新缓存（5分钟后重试）
                    if not hasattr(self, '_runway_cache'):
                        self._runway_cache = {}
                    self._runway_cache[cache_key] = (current_time, False)
                    return False
            else:
                self._append_log(f"Runway 充足（{runway_days} 天 ≥ 30 天，网络：{current_network}），无需充值")
                # 更新缓存
                if not hasattr(self, '_runway_cache'):
                    self._runway_cache = {}
                self._runway_cache[cache_key] = (current_time, True)
                return True
                
        except Exception as e:
            self._append_log(f"检查 runway 时发生错误（网络：{current_network}）: {e}")
            # 出错时使用缓存或返回 True 继续尝试
            if hasattr(self, '_runway_cache') and cache_key in self._runway_cache:
                _, cache_result = self._runway_cache[cache_key]
                return cache_result if cache_result is not None else True
            return True

    def _upload_car(self, car_path, private_key, idx, total, slot=None, provider_id=None):
        label = f"[{idx}/{total}] 上传 {car_path.name}"
        self._set_status(label)
        self._log_part(idx, label, slot)

        # 注意：这里去掉了 --auto-fund 参数
        # 资金检查已在 _upload_parts_concurrent 开始时通过 _ensure_runway 统一处理
        # 这样可以避免多线程并发时的 nonce 冲突问题
        cmd = [
            str(self.filecoin_pin_path),
            "import",
            str(car_path),
            # "--auto-fund",  # 已移除，改为上传前统一检查
            "--private-key",
            private_key,
        ]
        if provider_id is not None:
            cmd += ["--provider-id", str(provider_id)]
        cmd += self._network_args()

        self._append_log("$ " + " ".join(self._mask_private_key(cmd)))

        args = self.app._get_subprocess_args()

        # cwd 指向 tools/fil（或至少不要指向临时目录），避免 Windows 锁住临时目录导致无法清理
        safe_cwd = str(Path(self.filecoin_pin_path).parent)

        # 用 proc_mgr 统一登记并发子进程（避免 self.current_process 被覆盖）
        proc = self.proc_mgr.spawn(
            cmd,
            cwd=safe_cwd,
            tag="filecoin-pin",
            task_id=self.current_task_id or "global",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **args
        )

        success = False
        output_lines = []
        # 防止子进程在网络异常后不再输出但一直不退出，导致读取 stdout 阻塞而“卡住不动”
        # 仅在已出现错误输出且长时间无输出时才判定为卡住，避免误杀正常耗时上传
        silence_timeout_s = 180
        saw_error_output = False

        try:
            q = queue.Queue()
            sentinel = object()

            def _reader():
                try:
                    # 在后台线程里阻塞读 stdout，避免主线程卡住
                    for line in proc.stdout:
                        q.put(line)
                except Exception as e:
                    q.put(f"[stdout] {e}")
                finally:
                    q.put(sentinel)

            t = threading.Thread(target=_reader, daemon=True)
            t.start()

            last_output_ts = time.time()
            while True:
                if self.stop_flag:
                    # 这里仍然尽快终止当前进程；并发的其它进程请在 stop_upload() 里统一 terminate_all
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    self._log_part(idx, "上传被手动停止", slot)
                    break

                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    item = None

                if item is sentinel:
                    break

                if item is not None:
                    last_output_ts = time.time()
                    text = str(item).rstrip()
                    if text:
                        output_lines.append(text)
                        self._log_part(idx, text, slot)

                        if (
                            "Unhandled 'error' event" in text
                            or "Error:" in text
                            or "ECONNRESET" in text
                            or "Failed to find piece" in text
                            or "Timeout waiting for piece" in text
                            or "Import failed" in text
                        ):
                            saw_error_output = True

                        if "Import completed successfully" in text or "Import Complete" in text:
                            success = True
                    continue

                # 没有新输出：检查是否已经退出或疑似卡死
                if proc.poll() is not None:
                    # 等待 reader 把剩余输出消费完（短暂）
                    if q.empty():
                        break

                if saw_error_output and time.time() - last_output_ts > silence_timeout_s:
                    # 已出现错误输出后长时间无输出且未退出：认为卡住，杀掉以进入重试
                    self._log_part(idx, f"错误后长时间无输出（>{silence_timeout_s}s），判定为卡住，终止并进入重试", slot)
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                    if proc.poll() is None:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    break
        finally:
            # 确保关闭 stdout，帮助 Windows 更快释放资源
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
            # 清理已结束的进程记录
            try:
                self.proc_mgr.cleanup_finished()
            except Exception:
                pass

        rc = proc.returncode
        success = success or (rc == 0)

        if success and output_lines:
            self._log_final_details(output_lines, idx)

        if success:
            self._log_part(idx, f"{car_path.name} 上传完成", slot)
        else:
            self._log_part(idx, f"{car_path.name} 上传失败，返回码 {rc}", slot)

        return success, output_lines

    def _upload_with_retry(self, car_path, private_key, idx, total, max_retries=5, show_status=True, slot=None):
        """带重试的上传逻辑，包含网络自动切换功能"""
        original_network = self.network_var.get()
        network_switched = False
        testnet_error_count = 0
        forced_provider_id = None
        last_provider_id = None
        
        # 如果是测试网，先检查是否有历史失败记录
        if original_network == "calibration":
            # 检查是否有测试网失败的历史记录
            if hasattr(self, '_testnet_failures'):
                if self._testnet_failures >= 2:  # 如果测试网失败次数超过2次
                    self._log_part(idx, f"测试网历史失败次数过多（{self._testnet_failures}次），自动切换到主网", slot)
                    self.network_var.set("mainnet")
                    self._save_network_settings()
                    network_switched = True
                    self._append_log(f"已自动切换网络：calibration → mainnet（基于历史失败记录）")
        
        for attempt in range(1, max_retries + 1):
            if show_status:
                self._set_status(f"[{idx}/{total}] 上传 {car_path.name} - 尝试 {attempt}/{max_retries}")

            current_network = self.network_var.get()
            provider_ids = self._get_provider_ids(current_network)
            provider_id = None
            if forced_provider_id is not None:
                provider_id = forced_provider_id
                self._log_part(idx, f"切换存储提供商 provider-id={provider_id}", slot)
                forced_provider_id = None
            elif attempt > 1 and provider_ids:
                provider_id = provider_ids[(attempt - 2) % len(provider_ids)]
                self._log_part(idx, f"切换存储提供商 provider-id={provider_id}", slot)

            if provider_id is not None:
                last_provider_id = provider_id

            success, output = self._upload_car(car_path, private_key, idx, total, slot, provider_id=provider_id)
            
            if success:
                # 如果切换了网络且成功，记录这个成功经验
                if network_switched:
                    if not hasattr(self, '_mainnet_successes'):
                        self._mainnet_successes = 0
                    self._mainnet_successes += 1
                return True
            
            if self.stop_flag:
                break
            
            # 分析失败原因
            is_testnet_error = False
            is_nonce_error = False
            is_connection_error = False
            
            if output:
                # _upload_car 返回 list；早期版本返回 tuple，兼容两种类型
                if isinstance(output, tuple) and len(output) > 1:
                    output_lines = output[1]
                else:
                    output_lines = output

                if isinstance(output_lines, list):
                    combined_output = " ".join(str(line) for line in output_lines)

                    # 检查是否是测试网服务提供商错误
                    if ("Failed to find piece" in combined_output and "Service Provider PDP" in combined_output) or \
                       ("Timeout waiting for piece to be found" in combined_output):
                        is_testnet_error = True
                        # 记录测试网失败
                        if not hasattr(self, '_testnet_failures'):
                            self._testnet_failures = 0
                        self._testnet_failures += 1

                    # 检查是否是连接错误（如ETIMEDOUT）
                    if (
                        "ETIMEDOUT" in combined_output
                        or "connect ETIMEDOUT" in combined_output
                        or "ECONNRESET" in combined_output
                        or "read ECONNRESET" in combined_output
                        or "socket hang up" in combined_output
                        or "fetch failed" in combined_output
                        or "Failed to upload piece to service provider" in combined_output
                    ):
                        is_connection_error = True

                    # 检查是否是 nonce 冲突错误
                    if "nonce has already been used" in combined_output or "nonce 15 already in mpool" in combined_output:
                        is_nonce_error = True
            
            # 测试网错误：先随机切换测试网提供商，最多 2 次，再切主网
            if (is_testnet_error or is_connection_error) and original_network == "calibration" and not network_switched:
                testnet_error_count += 1
                if testnet_error_count <= 2:
                    testnet_providers = self._get_provider_ids("calibration")
                    if testnet_providers:
                        candidates = [pid for pid in testnet_providers if pid != last_provider_id]
                        if not candidates:
                            candidates = testnet_providers
                        forced_provider_id = random.choice(candidates)
                        self._log_part(idx, f"测试网错误，随机切换提供商 provider-id={forced_provider_id}（{testnet_error_count}/2）", slot)
                        # 进入下一次重试
                        continue
                self._log_part(idx, f"测试网连续错误 {testnet_error_count} 次，切换到主网（mainnet）", slot)
                self.network_var.set("mainnet")
                self._save_network_settings()
                network_switched = True
                self._append_log("已自动切换网络：calibration → mainnet")
                # 切换网络后立即重试
                continue
            
            # 如果是主网错误且之前有主网成功记录，考虑是否网络问题
            if (is_testnet_error or is_connection_error) and original_network == "mainnet" and hasattr(self, '_mainnet_successes') and self._mainnet_successes > 0:
                self._log_part(idx, f"主网服务异常，但历史上有{self._mainnet_successes}次成功记录，可能是临时网络问题", slot)
            
            if attempt < max_retries:
                # 根据错误类型调整重试间隔
                if is_nonce_error:
                    # nonce 冲突需要等待更长时间（30秒）
                    wait_time = 30
                    self._log_part(idx, f"{car_path.name} nonce 冲突，等待 {wait_time} 秒后重试 ({attempt}/{max_retries})", slot)
                elif is_testnet_error and network_switched:
                    # 切换到主网后等待较短时间（3秒）
                    wait_time = 3
                    self._log_part(idx, f"{car_path.name} 切换到主网后重试，等待 {wait_time} 秒 ({attempt}/{max_retries})", slot)
                elif is_connection_error:
                    # 连接错误等待中等时间（10秒）
                    wait_time = 10
                    self._log_part(idx, f"{car_path.name} 网络连接错误，等待 {wait_time} 秒后重试 ({attempt}/{max_retries})", slot)
                else:
                    # 其他错误等待较短时间（5秒）
                    wait_time = 5
                    self._log_part(idx, f"{car_path.name} 上传失败，{wait_time} 秒后重试 ({attempt}/{max_retries})", slot)
                
                time.sleep(wait_time)
        
        # 如果重试失败且切换了网络，恢复原始网络设置
        if network_switched:
            self.network_var.set(original_network)
            self._save_network_settings()
            self._append_log(f"已恢复原始网络设置：{original_network}")
        
        return False

    def _get_provider_ids(self, network):
        if network == "mainnet":
            return [1, 5]
        if network == "calibration":
            return [2, 5, 4, 11, 9]
        return []

    def _upload_parts_concurrent(self, car_parts, private_key):
        """并行上传分块（最多16线程，取决于设置），提供聚合进度"""

        # 上传前先确保 runway 充足
        # 避免多线程并发时每个线程都触发 deposit 交易导致的 nonce 冲突
        if self.network_var.get() == "mainnet":
            self._ensure_runway(private_key)
        else:
            self._append_log(f"测试网（{self.network_var.get()}）跳过 payments/runway 检查")
        
        total = len(car_parts)
        success_all = True
        failed_parts = []
        max_workers = min(max(1, self.thread_count_var.get()), 16, total)

        status = {"done": 0, "fail": 0, "inflight": 0}
        slots = ["空闲"] * max_workers
        part_labels = {part: part.name for part in car_parts}

        def update_status():
            head = f"完成{status['done']}/{total} 失败{status['fail']} 上传中{status['inflight']}"
            slot_list = [f"{i+1:02d}:{s}" for i, s in enumerate(slots)]
            self._set_thread_status(head, slot_list)

        parts_iter = iter(list(enumerate(car_parts, 1)))  # (idx, part)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_slot = {}

            def submit_one(slot_idx):
                try:
                    idx, part = next(parts_iter)
                except StopIteration:
                    return False
                status["inflight"] += 1
                slots[slot_idx] = f"{part_labels[part]} 上传中"
                update_status()
                future = executor.submit(self._upload_with_retry, part, private_key, idx, total, 5, False, slot_idx + 1)
                future_to_slot[future] = (slot_idx, part, idx)
                return True

            # 先填满线程
            for slot_idx in range(max_workers):
                if not submit_one(slot_idx):
                    break

            completed = 0
            update_status()
            while future_to_slot and not self.stop_flag:
                for future in as_completed(list(future_to_slot.keys()), timeout=None):
                    slot_idx, part, idx = future_to_slot.pop(future)
                    try:
                        ok = future.result()
                        if ok:
                            status["done"] += 1
                            slots[slot_idx] = f"{part_labels[part]} 完成"
                        else:
                            status["fail"] += 1
                            slots[slot_idx] = f"{part_labels[part]} 失败"
                            success_all = False
                            failed_parts.append(part_labels[part])
                    except Exception as exc:
                        status["fail"] += 1
                        slots[slot_idx] = f"{part_labels[part]} 异常"
                        success_all = False
                        failed_parts.append(part_labels[part])
                        self._log_part(idx, f"{part.name} 上传异常: {exc}", slot_idx + 1)
                    status["inflight"] = max(0, status["inflight"] - 1)
                    completed += 1
                    self._update_progress(50 + int(40 * completed / total))
                    # 同一槽位尝试提交下一个任务
                    submit_one(slot_idx)
                    update_status()
                    if self.stop_flag:
                        break
                if self.stop_flag:
                    break
            if completed == total:
                slots = ["空闲"] * max_workers
                update_status()

        if failed_parts:
            failed_list = ", ".join(failed_parts)
            self._append_log(f"[Summary] 上传失败分块（{len(failed_parts)}）: {failed_list}")

        return success_all

    def _cleanup(self, cid, created_files, work_dir, base_root, remove_workdir=True):
        if self.stop_flag or (not cid and not created_files):
            return
        if cid and self._cid_in_mfs(cid):
            self._append_log(f"MFS 中存在 {cid}，跳过清理")
            self._set_status("MFS 中存在，跳过清理")
            return
        self._set_status("清理临时数据...")
        if cid:
            try:
                self._run_command(
                    [self.app.kubo.kubo_path, "--repo-dir", self.app.repo_path, "pin", "rm", cid],
                    f"尝试解除 CID 固定: {cid}",
                    log_to_file=False
                )
            except Exception:
                pass
        try:
            self._run_command(
                [self.app.kubo.kubo_path, "--repo-dir", self.app.repo_path, "repo", "gc"],
                "运行 repo gc",
                log_to_file=False,
                skip_removed=True
            )
        except Exception as exc:
            self._append_log(f"GC 执行失败: {exc}")

        for path in created_files:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
        if remove_workdir:
            self._force_remove_path(work_dir)
            try:
                # 使用原文件目录时，base_root 位于源文件夹下，需要一并删除
                if base_root and (self.use_source_dir.get() or Path(base_root) != Path(self.work_root_var.get())):
                    self._force_remove_path(base_root)
            except Exception:
                pass
        self._set_status("完成")

    def _cid_in_mfs(self, cid: str) -> bool:
        """检查 CID 是否出现在 MFS 根目录（命令轻量，不递归）"""
        cmd = [
            self.app.kubo.kubo_path,
            "--repo-dir", self.app.repo_path,
            "files", "ls", "--cid", "/"
        ]
        try:
            args = self.app._get_subprocess_args()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                **args
            )
            if result.returncode != 0:
                return False
            for line in result.stdout.splitlines():
                if cid in line:
                    return True
        except Exception:
            return False
        return False

    def _force_remove_path(self, path: Path) -> bool:
        """强制删除文件/目录，必要时重命名后删除"""
        path = Path(path)
        for attempt in range(3):
            try:
                if path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=False, onerror=self._on_remove_error)
                else:
                    return True
                if not path.exists():
                    return True
            except Exception:
                time.sleep(0.5)
        # 如果仍未删除，尝试重命名后删除
        try:
            if path.exists():
                temp_name = path.with_name(path.name + f".to_delete_{int(time.time())}")
                path.rename(temp_name)
                shutil.rmtree(temp_name, ignore_errors=False, onerror=self._on_remove_error)
                return not temp_name.exists()
        except Exception:
            pass
        self._append_log(f"临时目录未完全删除：{path}", log_to_file=False)
        return False

    def _on_remove_error(self, func, path, exc_info):
        try:
            os.chmod(path, 0o666)
            func(path)
        except Exception:
            pass

    def _log_final_details(self, lines, idx=None):
        """将 filecoin-pin 成功输出中的关键信息写入日志面板"""
        keywords = (
            "Import Complete",
            "Network:",
            "Import Details",
            "File:",
            "Size:",
            "Root CID:",
            "Filecoin Storage",
            "Piece CID:",
            "Piece ID:",
            "Data Set ID:",
            "Storage Provider",
            "Provider ID:",
            "Direct Download URL:"
        )
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if any(stripped.startswith(k) for k in keywords):
                if idx:
                    self._append_log(stripped, thread_id=idx)
                else:
                    self._append_log(stripped)
        if self.cid_var.get():
            if idx:
                self._append_log(f"根CID: {self.cid_var.get()}", thread_id=idx)
            else:
                self._append_log(f"根CID: {self.cid_var.get()}")

    def _add_cid_entry(self, cid):
        self.cid_lines.append(cid)
        def update():
            try:
                self.cid_text_box.insert(tk.END, cid + "\n")
                self.cid_text_box.see(tk.END)
            except Exception:
                pass
        self.app._call_ui(update)

    def _get_all_cids_text(self):
        try:
            return self.cid_text_box.get("1.0", tk.END)
        except Exception:
            return self.cid_var.get()

    def _clear_cid_text(self):
        """清空分享 CID 文本"""
        self.cid_lines = []
        self.cid_var.set("")
        try:
            self.cid_text_box.delete("1.0", tk.END)
        except Exception:
            pass

    # ========== 拖放处理 ==========
    def _on_drop_source(self, event):
        try:
            paths = self.master.tk.splitlist(event.data)
            if not paths:
                return
            path = paths[0]
            # 去掉包裹的引号
            if (path.startswith("{") and path.endswith("}")) or (path.startswith('"') and path.endswith('"')):
                path = path[1:-1]
            self._append_source_path(path)
        except Exception:
            pass

    def _copy_cid(self):
        """复制根 CID 到剪贴板"""
        cid = self.cid_var.get().strip() or self._get_all_cids_text().strip()
        if not cid:
            messagebox.showwarning("提示", "没有可复制的 CID")
            return
        try:
            self.master.clipboard_clear()
            self.master.clipboard_append(cid)
            self._append_log("已复制根 CID")
        except Exception:
            pass
    # 填写到主界面
    def _fill_to_main(self):
        cid = self.cid_var.get().strip()
        if not cid:
            # 回退多行框
            cid = self._get_all_cids_text().strip()
            if not cid:
                messagebox.showwarning("提示", "没有可填写的 CID")
                return
        try:
            if hasattr(self.app, "cid_text_advanced"):
                self.app.cid_text_advanced.delete("1.0", tk.END)
                self.app.cid_text_advanced.insert(tk.END, cid + ("\n" if not cid.endswith("\n") else ""))
        except Exception:
            pass
        try:
            if hasattr(self.app, "cid_output_text_simple"):
                self.app.cid_output_text_simple.delete("1.0", tk.END)
                self.app.cid_output_text_simple.insert(tk.END, cid + ("\n" if not cid.endswith("\n") else ""))
        except Exception:
            pass
        # 将主窗口从托盘弹出并置于前台
        try:
            if hasattr(self.app, "root"):
                self.app.root.deiconify()  # 从托盘/最小化恢复窗口
                self.app.root.lift()       # 置于最上层
                self.app.root.focus_force() # 获取焦点
        except Exception:
            pass

    def _log_part(self, idx, text, slot=None):
        """在日志前添加分块序号/线程槽位，便于区分并发输出"""
        if not text or not str(text).strip():
            return
        prefix = f"[{idx}] "
        thread_id = slot or idx
        self._append_log(prefix + text, thread_id=thread_id)

    def _append_source_path(self, path):
        try:
            self.src_text.insert(tk.END, ("" if self.src_text.get("1.0", tk.END).strip() == "" else "\n") + path)
            self.src_text.see(tk.END)
        except Exception:
            self.source_var.set(path)

    def _clear_sources(self):
        try:
            self.src_text.delete("1.0", tk.END)
        except Exception:
            self.source_var.set("")

    def _get_sources(self):
        try:
            text = self.src_text.get("1.0", tk.END)
            paths = [line.strip().strip('"') for line in text.splitlines() if line.strip()]
        except Exception:
            paths = [self.source_var.get().strip('" ')] if self.source_var.get().strip() else []
        paths = [p for p in paths if p]
        return paths

    def _log_visible(self, thread_id):
        target = self.log_filter_var.get()
        if target == "全部" or not target:
            return True
        try:
            tid = int(target)
        except Exception:
            return True
        return thread_id == tid

    def _refresh_log_display(self):
        def refresh():
            if not getattr(self, "log_text", None) or not self.log_text.winfo_exists():
                return
            with self.log_lock:
                self.log_text.config(state=tk.NORMAL)
                self.log_text.delete("1.0", tk.END)
                for tid, text in self.log_entries:
                    if self._log_visible(tid):
                        self.log_text.insert(tk.END, text + "\n")
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
        self.app._call_ui(refresh)

    def _update_log_filter_options(self):
        options = ["全部"] + [str(i) for i in range(1, max(1, self.thread_count_var.get()) + 1)]
        self.log_filter["values"] = options
        if self.log_filter_var.get() not in options:
            self.log_filter_var.set("全部")

    def _prepare_work_dir(self, source_path):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        original_name = source_path.stem if source_path.is_file() else source_path.name or "export"
        safe_name = self._sanitize_name(original_name)
        if safe_name != original_name:
            self._append_log(f"长文件名已缩短以避免路径过长: {original_name} -> {safe_name}")

        if self.use_source_dir.get():
            base_root = source_path.parent / f"filecoin-pin_temp_{ts}"
        else:
            base_root = Path(self.work_root_var.get())
        base_root.mkdir(parents=True, exist_ok=True)

        path = base_root / f"{safe_name}-{ts}"
        path.mkdir(parents=True, exist_ok=True)
        return path, base_root

    def _sanitize_name(self, name: str, limit: int = 64) -> str:
        """为路径生成安全、较短的名称，保留可读性并追加哈希避免冲突"""
        if not name:
            return "export"
        cleaned = re.sub(r'[\\/:*?"<>|]+', "_", name)
        if len(cleaned) <= limit:
            return cleaned
        prefix_len = max(8, limit - 12)
        hash_part = hashlib.sha1(cleaned.encode("utf-8", errors="ignore")).hexdigest()[:8]
        return f"{cleaned[:prefix_len]}_{hash_part}"

    def _run_command(self, cmd, label=None, log_to_file=True, skip_removed=False):
        if label:
            self._append_log(label, log_to_file=log_to_file)
        masked = " ".join(self._mask_private_key(cmd))
        self._append_log(f"$ {masked}", log_to_file=log_to_file)
        args = self.app._get_subprocess_args()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **args
        )
        self.current_process = proc
        output_lines = []
        try:
            for line in proc.stdout:
                if self.stop_flag:
                    proc.terminate()
                    self._append_log("已请求停止，终止当前命令")
                    break
                cleaned = line.rstrip()
                if skip_removed and cleaned.strip().startswith("removed "):
                    continue
                output_lines.append(cleaned)
                self._append_log(cleaned, log_to_file=log_to_file)
            rc = proc.wait()
        finally:
            self.current_process = None
        return rc, "\n".join(output_lines)

    def _run_command_with_timeout(self, cmd, timeout=30, label=None, log_to_file=True):
        """带超时的命令执行，避免长时间阻塞"""
        if label:
            self._append_log(label, log_to_file=log_to_file)
        masked = " ".join(self._mask_private_key(cmd))
        self._append_log(f"$ {masked}", log_to_file=log_to_file)
        args = self.app._get_subprocess_args()
        
        try:
            # 使用 subprocess.run 带超时
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                **args
            )
            
            output = result.stdout
            if output:
                for line in output.splitlines():
                    self._append_log(line, log_to_file=log_to_file)
            
            return result.returncode, output
            
        except subprocess.TimeoutExpired:
            self._append_log(f"命令执行超时（{timeout}秒）", log_to_file=log_to_file)
            return 1, f"命令执行超时（{timeout}秒）"
        except Exception as e:
            self._append_log(f"命令执行错误: {e}", log_to_file=log_to_file)
            return 1, f"命令执行错误: {e}"

    def _mask_private_key(self, cmd_list):
        masked = []
        skip_next = False
        for item in cmd_list:
            text = str(item)
            if skip_next:
                masked.append("******")
                skip_next = False
                continue
            masked.append(text)
            if text.lower() == "--private-key":
                skip_next = True
        return masked

    def _network_args(self):
        net = self.network_var.get() or "mainnet"
        if net == "calibration":
            return ["--network", "calibration"]
        return ["--mainnet"]

    def _set_controls_active(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        try:
            if self.start_button.winfo_exists():
                self.start_button.config(state=state)
            if self.stop_button.winfo_exists():
                self.stop_button.config(state=tk.DISABLED if enabled else tk.NORMAL)
            if hasattr(self, "split_only_button") and self.split_only_button.winfo_exists():
                self.split_only_button.config(state=state)
            if hasattr(self, "upload_parts_button") and self.upload_parts_button.winfo_exists():
                self.upload_parts_button.config(state=state)
            if hasattr(self, "cleanup_button") and self.cleanup_button.winfo_exists():
                self.cleanup_button.config(state=state)
        except Exception:
            pass

    def _append_log(self, text, log_to_file=True, thread_id=None):
        def write():
            try:
                if not getattr(self, "log_text", None) or not self.log_text.winfo_exists():
                    return
                with self.log_lock:
                    self.log_entries.append((thread_id, text))
                    if self._log_visible(thread_id):
                        self.log_text.config(state=tk.NORMAL)
                        self.log_text.insert(tk.END, text + "\n")
                        self.log_text.see(tk.END)
                        self.log_text.config(state=tk.DISABLED)
            except Exception:
                pass
        self.app._call_ui(write)
        if log_to_file:
            try:
                self.logger.info(text)
            except Exception:
                pass

    def _set_status(self, text):
        self.app._call_ui(lambda: self.status_var.set(text))

    def _set_thread_status(self, head, slots_list):
        # slots_list 拆为两排，每排最多 8 个；第二排前置空格保持对齐
        first_row = slots_list[:8]
        second_row = slots_list[8:]
        line1 = head
        if first_row:
            line1 = f"{head} | " + " | ".join(first_row)
        indent = 2 * " " * (len(head) + 3)
        line2 = ""
        if second_row:
            line2 = indent + " | ".join(second_row)
        def update():
            self.thread_status_var.set(line1)
            self.thread_status_var2.set(line2)
        self.app._call_ui(update)

    def _set_file_seq_status(self, idx, total):
        self.app._call_ui(lambda: self.file_seq_status_var.set(f"文件进度: {idx}/{total}"))

    def _set_thread_status_idle(self):
        count = max(1, self.thread_count_var.get())
        head = "完成0/0 失败0 上传中0"
        self._set_thread_status(head, [f"{i+1:02d}:空闲" for i in range(count)])

    def _update_progress(self, value):
        def update():
            try:
                if self.progress.winfo_exists():
                    self.progress.config(value=value)
            except Exception:
                pass
        self.app._call_ui(update)

    def _format_size(self, size_in_bytes):
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_in_bytes)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.2f} {unit}"
            size /= 1024

    def _invoke_pin_plugin(self, cid):
        """调用插件对 CID 进行一次广播和 PIN（Filecoin 上传完成后）"""
        def run_plugin():
            try:
                # 获取项目根目录和 Python 解释器
                base_dir = Path(self.app.app_path)
                plugin_path = base_dir / "plugins" / "sample_pin_loop.py"

                if not plugin_path.exists():
                    self.logger.warning(f"Plugin not found: {plugin_path}")
                    return

                # 优先使用 runtime/python.exe，回退到 sys.executable
                python_exec = self.app.runtime_python or sys.executable
                if not python_exec:
                    self.logger.warning("Python executable not found")
                    return

                # 先记录开始日志
                self.logger.info(f"Filecoin upload: invoking broadcast and pin for {cid}")

                # 调用插件：广播一次，PIN 一次，然后退出（静默模式）
                # 使用多线程并发广播，即使 100 个网关也能快速完成
                # duration 设置为 60 秒以确保广播完成（考虑网络延迟和 aleph_client 执行时间）
                cmd = [
                    python_exec, str(plugin_path), cid,
                    "--interval", "0",      # 立即执行
                    "--duration", "60",     # 确保有足够时间完成广播和 pin 操作
                    "--broadcast-interval", "0",  # 立即广播
                    "--max-gateways", "100",  # 广播 100 个网关（多线程并发）
                    "--quiet"               # 静默模式，不输出详细日志
                ]

                # 记录执行的指令
                cmd_str = " ".join(cmd)
                self.logger.info(f"Filecoin upload: executing plugin command: {cmd_str}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,  # 增加超时到 180 秒，给插件足够时间完成
                    cwd=str(base_dir),
                    **self.app._get_subprocess_args()
                )

                # 记录完成日志
                if result.returncode == 0:
                    # 从输出中提取广播摘要
                    broadcast_summary = ""
                    if result.stdout:
                        for line in result.stdout.splitlines():
                            if "[broadcast] completed:" in line:
                                broadcast_summary = line.strip()
                                break

                    if broadcast_summary:
                        self.logger.info(f"Filecoin upload: {broadcast_summary}")
                        self.logger.info(f"Aleph pin completed for {cid}")
                    else:
                        self.logger.info(f"Filecoin upload: broadcast and pin completed for {cid}")
                else:
                    self.logger.warning(f"Filecoin upload: plugin exited with code {result.returncode}")
                    if result.stderr:
                        self.logger.debug(f"Plugin stderr: {result.stderr}")

            except subprocess.TimeoutExpired:
                self.logger.warning(f"Filecoin upload: plugin timeout for {cid}")
            except Exception as e:
                self.logger.error(f"Filecoin upload: plugin error for {cid}: {e}")

        # 在后台线程中运行，不阻塞主流程
        threading.Thread(target=run_plugin, daemon=True).start()
