# src\utils\aleph_integrated_app.py

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
from tkinterdnd2 import DND_FILES, TkinterDnD
import subprocess
import threading
import os
import sys
import time
import re
import json
from pathlib import Path
from datetime import datetime
import base64
import webbrowser
import logging
import runpy
import io
import traceback
import asyncio
import requests

# ==================== 提前设置 ALEPH_HOME ====================
def _ensure_aleph_home():
    # 若用户已显式设置环境变量，尊重现有配置
    aleph_home = os.environ.get("ALEPH_HOME") or os.environ.get("ALEPH_CONFIG_HOME")
    if not aleph_home:
        if getattr(sys, 'frozen', False):
            base_path = Path(sys.executable).resolve().parent
        else:
            # 本文件位于 项目根/src/utils/，向上两级到项目根目录
            base_path = Path(__file__).resolve().parents[2]
        # 若实际落在二级 src 目录内（仅应当存放代码），则将配置放到上一层
        if base_path.name == "src" and (base_path / "utils").exists() and (base_path / "ipfs_gui.py").exists():
            base_path = base_path.parent
        aleph_home = str(base_path / ".aleph-im")

    os.environ["ALEPH_CONFIG_HOME"] = aleph_home
    os.environ["ALEPH_HOME"] = aleph_home

_ensure_aleph_home()

try:
    import aleph_client
except ImportError:
    pass

def _guess_runtime_python():
    """
    返回随应用打包的 runtime/python 可执行文件路径，若不存在返回 None
    """
    try:
        if getattr(sys, 'frozen', False):
            base_path = Path(sys.executable).resolve().parent
        else:
            base_path = Path(__file__).resolve().parents[2]
            if base_path.name == "src" and (base_path / "utils").exists():
                base_path = base_path.parent
        # Windows 打包路径
        candidate = base_path / "runtime" / ("python.exe" if os.name == "nt" else "python")
        if candidate.exists():
            return candidate
        # 兼容类 Unix 打包路径
        candidate = base_path / "runtime" / "bin" / "python"
        if candidate.exists():
            return candidate
    except Exception:
        return None
    return None

def _shorten_error(err):
    """
    压缩冗长的错误输出（尤其是带 Traceback 的网络错误），只保留关键信息。
    """
    if not err:
        return err
    # 去掉边框类字符行
    lines = [l for l in err.splitlines() if l.strip() and not l.strip().startswith(("┌", "│", "└"))]
    # 优先保留包含核心关键词的行
    key_lines = [l for l in lines if ("BroadcastError" in l or "Unexpected HTTP response" in l or "Service Unavailable" in l)]
    if key_lines:
        return key_lines[0]
    tb_lines = [l for l in lines if "Traceback" in l]
    if tb_lines:
        return tb_lines[0]
    # 回退保留首行
    return lines[0] if lines else err

def _looks_network_error(err):
    if not err:
        return False
    keys = ["Cannot connect", "ClientConnectorError", "TimeoutError", "ConnectionResetError", "Service Unavailable", "BroadcastError", "Unexpected HTTP response"]
    if any(k in err for k in keys):
        return True
    return bool(re.search(r"\b5\d{2}\b", err))

from utils import EmbeddedKubo

# ==================== 全局环境配置 ====================
class NullWriter:
    def write(self, data): pass
    def flush(self): pass
    def isatty(self): return False

if sys.stdout is None: sys.stdout = NullWriter()
if sys.stderr is None: sys.stderr = NullWriter()

if sys.platform == 'win32':
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass

# ==================== 常量定义 ====================
class Constants:
    """应用常量"""
    CID_V0_PREFIX = 'Qm'
    CID_V0_LENGTH = 46
    CID_V1_PREFIX = 'b'
    CHUNKER_DEFAULT = "size-262144"
    CHUNKER_FILECOIN = "size-1048576"
    WINDOW_WIDTH_RATIO = 0.6
    WINDOW_HEIGHT_RATIO = 0.80
    MIN_WINDOW_WIDTH = 1200
    MIN_WINDOW_HEIGHT = 800
    MIN_LEFT_FRAME_WIDTH = 500
    FONT_SIZE_NORMAL = 10
    FONT_SIZE_TITLE = 12
    ALEPH_CONFIG_DIR = ".aleph-im"
    # ALEPH_EXE_NAME = "aleph.exe"
    
    # API 节点列表：官方负载均衡器优先
    ALEPH_API_ENDPOINTS = [
        "https://official.aleph.cloud",
        "https://api2.aleph.im",
        "https://api1.aleph.im",
        "https://public-api.aleph.sh"
    ]
    DEFAULT_TIMEOUT = 300
    PIN_INTERVAL = 3  # seconds, 控制所有 PIN 操作的间隔

# ==================== 智能学习模块 (Machine Learning) ====================
class NodeIntelligence:
    """
    节点智能学习模块
    利用指数移动平均 (EMA) 算法累积CCN节点性能数据，实现从历史中学习。
    """
    def __init__(self, config_dir, logger):
        self.history_file = os.path.join(config_dir, "ccn_node_learning_data.json")
        self.logger = logger
        self.stats = self._load_stats()
        
    def _load_stats(self):
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r') as f:
                    return json.load(f)
        except Exception as e:
            self.logger.warning(f"加载节点历史数据失败: {e}")
        return {}
        
    def save_stats(self):
        try:
            with open(self.history_file, 'w') as f:
                # 使用 indent=2 让 JSON 文件更易读
                json.dump(self.stats, f, indent=2) 
        except Exception:
            pass
            
    def record_observation(self, url, latency_ms, is_success=True, name=None):
        """
        【学习核心】记录一次观测结果，并更新预测模型
        :param url: 节点地址
        :param latency_ms: 本次实测延迟 (ms)
        :param is_success: 是否连接成功
        :param name: 节点名称 (例如 'Rick_Sanchez_is_back!*')
        """
        if url not in self.stats:
            # 初始化新节点记忆
            self.stats[url] = {
                "name": name or "Unknown", # 记录节点名称
                "ema_latency": latency_ms if is_success else 2000, # 初始猜测
                "success_count": 0,
                "fail_count": 0,
                "total_samples": 0,
                "last_seen": 0,
                "score": 0 # 动态计算
            }
            
        node = self.stats[url]
        
        # 如果有新的名称传入，更新名称（以防名称变更或补全）
        if name:
            node["name"] = name
            
        node["last_seen"] = time.time()
        node["total_samples"] += 1
        
        if is_success:
            node["success_count"] += 1
            # --- 学习算法: 指数移动平均 (EMA) ---
            # Alpha = 0.3 表示新数据占 30% 权重，历史记忆占 70% 权重
            # 这样既能平滑偶尔的网络抖动，又能在大趋势变慢时及时反应
            alpha = 0.3
            current_ema = node.get("ema_latency", latency_ms)
            new_ema = (current_ema * (1 - alpha)) + (latency_ms * alpha)
            node["ema_latency"] = new_ema
        else:
            node["fail_count"] += 1
            # 惩罚机制: 连接失败一次，历史评分大幅下降 (延迟预估值增加 1.5 倍)
            node["ema_latency"] = node.get("ema_latency", 1000) * 1.5
            
        self.save_stats()
        
    def get_predicted_performance(self, url, is_official=False):
        """
        获取节点表现的【预测值】(越低越好)
        预测值 = 历史EMA延迟 * 稳定性惩罚 * 官方偏好权重
        """
        if url not in self.stats:
            # 对于未知节点，给予一个默认分 (官方节点默认分更优，优先尝试)
            return 500 if is_official else 800
            
        node = self.stats[url]
        base_latency = node.get("ema_latency", 1000)
        
        # 1. 稳定性惩罚 (Stability Penalty)
        total = node.get("total_samples", 1)
        fails = node.get("fail_count", 0)
        fail_rate = fails / total if total > 0 else 0
        # 失败率越高，惩罚系数越大 (e.g., 10% 失败率 -> 1.2倍惩罚)
        stability_penalty = 1.0 + (fail_rate * 2.0) 
        
        # 2. 官方偏好权重 (Official Bias)
        # 官方节点给予 0.9 的优惠系数，社区节点 1.0
        # 略微降低对官方的偏好，便于在官方节点抖动时切换
        official_bias = 0.9 if is_official else 1.0
        
        predicted_score = base_latency * stability_penalty * official_bias
        return predicted_score

# ==================== 工具类 ====================
def run_aleph_cli(args, input_text=None):
    # 优先调用随应用打包的 runtime/python -m aleph_client，确保与插件一致
    runtime_python = _guess_runtime_python()
    if runtime_python and runtime_python.exists():
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = SubprocessHelper.run_command(
            [str(runtime_python), "-m", "aleph_client"] + list(args),
            input=input_text or "",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # 避免控制台编码导致崩溃
            env=env
        )
        final_stderr = proc.stderr or ""
        ignore_keywords = ["Could not import library 'magic'", "Consider installing rusty-rlp", "No account type specified", "Detected ETH account"]
        filtered_lines = [line for line in final_stderr.splitlines() if not any(keyword in line for keyword in ignore_keywords)]
        final_stderr_cleaned = "\n".join(filtered_lines)
        final_stderr_cleaned = _shorten_error(final_stderr_cleaned) if _looks_network_error(final_stderr_cleaned) else final_stderr_cleaned
        return proc.stdout or "", final_stderr_cleaned, proc.returncode

    # 回退方案：在当前解释器内用 runpy 运行 aleph_client
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    stdin_buf = io.StringIO(input_text) if input_text is not None else io.StringIO("")

    old_argv = sys.argv[:]
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    old_stdin = sys.stdin

    root_logger = logging.getLogger()
    old_handlers = root_logger.handlers[:] 

    try:
        os.environ["PYTHONIOENCODING"] = "utf-8"
        os.environ["PYTHONUTF8"] = "1"
        
        sys.argv = ["aleph"] + list(args)
        sys.stdout = stdout_buf
        sys.stderr = stderr_buf
        sys.stdin = stdin_buf

        for h in old_handlers: root_logger.removeHandler(h)
        
        temp_handler = logging.StreamHandler(stderr_buf)
        temp_handler.setFormatter(logging.Formatter('%(message)s'))
        root_logger.addHandler(temp_handler)
        
        logging.getLogger("magic").setLevel(logging.ERROR)
        logging.getLogger("aleph_client").setLevel(logging.WARNING) 
        logging.getLogger("aleph").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.ERROR)
        logging.getLogger("aiodns").setLevel(logging.ERROR)
        logging.getLogger("urllib3").setLevel(logging.ERROR)

        try:
            runpy.run_module("aleph_client.__main__", run_name="__main__")
            returncode = 0
        except SystemExit as e:
            returncode = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except ImportError:
            traceback.print_exc(file=stderr_buf)
            returncode = 1
        except Exception:
            traceback.print_exc(file=stderr_buf)
            returncode = 1

    finally:
        root_logger.removeHandler(temp_handler)
        for h in old_handlers: root_logger.addHandler(h)
        sys.argv = old_argv
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        sys.stdin = old_stdin
    
    final_stderr = stderr_buf.getvalue()
    ignore_keywords = ["Could not import library 'magic'", "Consider installing rusty-rlp", "No account type specified", "Detected ETH account"]
    filtered_lines = [line for line in final_stderr.splitlines() if not any(keyword in line for keyword in ignore_keywords)]
    final_stderr_cleaned = "\n".join(filtered_lines)

    return stdout_buf.getvalue(), final_stderr_cleaned, returncode

class SubprocessHelper:
    @staticmethod
    def popen_command(command, **kwargs):
        flags = {}
        if sys.platform.startswith('win'):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            flags = {'startupinfo': si, 'creationflags': subprocess.CREATE_NO_WINDOW}
        return subprocess.Popen(command, **flags, **kwargs)
    
    @staticmethod
    def run_command(command, timeout=None, **kwargs):
        flags = {}
        if sys.platform.startswith('win'):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
            flags = {'startupinfo': si, 'creationflags': subprocess.CREATE_NO_WINDOW}
        return subprocess.run(command, timeout=timeout, **flags, **kwargs)

class CIDValidator:
    @staticmethod
    def is_cid_v0(cid): return cid.startswith(Constants.CID_V0_PREFIX) and len(cid) == Constants.CID_V0_LENGTH
    @staticmethod
    def is_cid_v1(cid): return cid.startswith(Constants.CID_V1_PREFIX) and all(c.isalnum() or c == '-' for c in cid)
    @staticmethod
    def is_valid_cid(cid): return CIDValidator.is_cid_v0(cid) or CIDValidator.is_cid_v1(cid)

class UIHelper:
    @staticmethod
    def create_scrolled_text(parent, height=10, width=50):
        frame = ttk.Frame(parent)
        v_bar = ttk.Scrollbar(frame, orient='vertical')
        v_bar.pack(side='right', fill='y')
        h_bar = ttk.Scrollbar(frame, orient='horizontal')
        h_bar.pack(side='bottom', fill='x')
        text = tk.Text(frame, height=height, width=width, wrap='none', yscrollcommand=v_bar.set, xscrollcommand=h_bar.set)
        text.pack(side='left', fill='both', expand=True)
        v_bar.config(command=text.yview)
        h_bar.config(command=text.xview)
        return frame, text

class IPFSRepoFinder:
    def __init__(self, app_path, logger):
        self.app_path = app_path
        self.logger = logger
    def find_repo(self):
        default_path = os.path.join(self.app_path, ".ipfs")
        return default_path

class AlephConfigManager:
    def __init__(self, app_path, logger):
        self.app_path = app_path
        self.logger = logger
        self.config_dir = os.path.join(self.app_path, Constants.ALEPH_CONFIG_DIR)
    def ensure_config_directory(self):
        try:
            os.makedirs(os.path.join(self.config_dir, "private-keys"), exist_ok=True)
            return self.config_dir
        except Exception as e:
            self.logger.error(f"Config dir error: {e}")
            return None

# ==================== 左侧CID计算器 ====================
class CIDCalculator:
    def __init__(self, master, app_path, logger, integrated_app, ipfs_path=None, repo_dir=None, allow_repo_init=True):
        self.master = master
        self.app_path = app_path
        self.logger = logger
        self.integrated_app = integrated_app
        self.kubo = None
        self.ipfs_path = ipfs_path
        self.repo_dir = repo_dir or IPFSRepoFinder(app_path, logger).find_repo()
        self._allow_repo_init = allow_repo_init

        # 若调用方未提供 ipfs 路径，则按旧逻辑自行管理（会触发初始化）
        if not self.ipfs_path:
            self.kubo = EmbeddedKubo(os.path.join(app_path, "kubo"))
            self.ipfs_path = self.kubo.kubo_path
            # 当组件自行管理 Kubo 时允许初始化仓库
            self._allow_repo_init = True

        self.create_widgets()
        try:
            self._ensure_repo_initialized()
        except Exception as exc:
            # 若禁用自动初始化，则仅记录日志等待主程序处理
            self.logger.warning(f"IPFS 初始化检查跳过/失败: {exc}")
    
    def create_widgets(self):
        main_frame = ttk.Frame(self.master)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        input_frame = ttk.LabelFrame(main_frame, text="INPUT 输入", style='BigTitle.TLabelframe')
        input_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        input_text_frame, self.input_text = UIHelper.create_scrolled_text(input_frame, height=12)
        input_text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.input_text.drop_target_register(DND_FILES)
        self.input_text.dnd_bind('<<Drop>>', self._on_drop)
        
        opts_frame = ttk.Frame(main_frame)
        opts_frame.pack(fill=tk.X, pady=10)
        ttk.Label(opts_frame, text="计算模式:").pack(side=tk.LEFT)

        self.cid_version_var = tk.StringVar(value="CID v1") # 默认 CID v1
        values = ["CID v0", "CID v1", "File -> v1 -> v0", "File -> v0 -> v1"]
        ttk.Combobox(opts_frame, values=values, state="readonly", width=20, textvariable=self.cid_version_var).pack(side=tk.LEFT)
        self.use_filecoin = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts_frame, text=f"使用Filecoin参数", variable=self.use_filecoin).pack(side=tk.LEFT, padx=20)
        
        btn_frame = ttk.Frame(main_frame)
        btn_frame.pack(pady=10)
        ttk.Button(btn_frame, text="计算CID", width=12, command=self.start_calculate_cid).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="复制CID", width=12, command=self.copy_to_clipboard).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_frame, text="填充到右侧→", width=15, command=self.fill_to_aleph).pack(side=tk.LEFT, padx=10)
        ttk.Button(btn_frame, text="清空", width=8, command=self.clear_all).pack(side=tk.LEFT, padx=2)
        
        out_frame = ttk.LabelFrame(main_frame, text="OUTPUT 输出", style='BigTitle.TLabelframe')
        out_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        output_text_frame, self.output_text = UIHelper.create_scrolled_text(out_frame, height=12)
        output_text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(main_frame, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, pady=5)

    def _on_drop(self, event):
        files = self.master.tk.splitlist(event.data)
        curr = self.input_text.get("1.0", tk.END).strip()
        for f in files:
            if curr: self.input_text.insert(tk.END, "\n")
            self.input_text.insert(tk.END, f)
            curr += "\n" + f

    def start_calculate_cid(self):
        items = [i.strip() for i in self.input_text.get("1.0", tk.END).split("\n") if i.strip()]
        if not items: return
        self.status_var.set("计算中...")
        threading.Thread(target=self.calculate_cid, args=(items,), daemon=True).start()

    def calculate_cid(self, items):
        res = []
        mode = self.cid_version_var.get()
        for idx, item in enumerate(items, 1):
            self.master.after(0, lambda m=f"({idx}/{len(items)}) {item}": self.status_var.set(m))
            try:
                # 1. 如果输入是现有 CID (进行格式转换)
                if CIDValidator.is_valid_cid(item):
                    target_v0 = (mode == "CID v0" or mode == "File -> v1 -> v0")
                    target_v1 = (mode == "CID v1" or mode == "File -> v0 -> v1")
                    
                    if target_v0:
                        res.append(self._to_v0(item))
                    elif target_v1:
                        res.append(self._to_v1(item))
                    else:
                        res.append(item)

                # 2. 如果输入是文件路径 (进行计算)
                elif os.path.exists(item):
                    cid = ""
                    if mode == "CID v0":
                        cid = self._calc_file(item, 0)
                    elif mode == "CID v1":
                        cid = self._calc_file(item, 1)
                    elif mode == "File -> v1 -> v0":
                        # 先算 v1 再转 v0
                        temp = self._calc_file(item, 1)
                        cid = self._to_v0(temp)
                    elif mode == "File -> v0 -> v1":
                        # 先算 v0 再转 v1
                        temp = self._calc_file(item, 0)
                        cid = self._to_v1(temp)
                    else:
                        # 默认情况
                        cid = self._calc_file(item, 1)
                    
                    res.append(cid)
                else: 
                    res.append(f"Error: {item}")
            except Exception as e: 
                res.append(f"Error: {str(e)}")
        self.master.after(0, lambda: self._finish(res))

    def _calc_file(self, path, ver):
        self._ensure_repo_initialized()
        cmd = [self.ipfs_path, "add", "--only-hash", "-Q", "--repo-dir", self.repo_dir, f"--cid-version={ver}"]
        if os.path.isdir(path): cmd.append("-r")
        cmd.extend(["--chunker", Constants.CHUNKER_FILECOIN if self.use_filecoin.get() else Constants.CHUNKER_DEFAULT, path])
        p = SubprocessHelper.popen_command(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        out, err = p.communicate(timeout=300)
        if p.returncode != 0: raise Exception(err.decode('utf-8', errors='replace'))
        return out.decode('utf-8', errors='replace').strip()

    def _to_v0(self, cid):
        self._ensure_repo_initialized()
        cmd = [self.ipfs_path, "cid", "format", "-v", "0", cid]
        p = SubprocessHelper.popen_command(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        out, err = p.communicate(timeout=300)
        if p.returncode != 0: raise Exception(err.decode('utf-8', errors='replace'))
        return out.decode('utf-8', errors='replace').strip()

    def _to_v1(self, cid):
        self._ensure_repo_initialized()
        cmd = [self.ipfs_path, "cid", "format", "-v", "1", cid]
        p = SubprocessHelper.popen_command(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False)
        out, err = p.communicate(timeout=300)
        if p.returncode != 0: raise Exception(err.decode('utf-8', errors='replace'))
        return out.decode('utf-8', errors='replace').strip()

    def _ensure_repo_initialized(self):
        if not self.ipfs_path:
            raise RuntimeError("未提供 IPFS 可执行路径")

        config_path = os.path.join(self.repo_dir, "config")
        if os.path.exists(config_path):
            return

        if not self._allow_repo_init:
            raise RuntimeError("IPFS 节点尚未初始化，请在主程序中先完成节点初始化后再使用 Aleph 工具。")

        os.makedirs(self.repo_dir, exist_ok=True)
        env = os.environ.copy()
        env["IPFS_PATH"] = self.repo_dir
        SubprocessHelper.run_command([self.ipfs_path, "init"], env=env, capture_output=True, timeout=60)

    def _finish(self, results):
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", "\n".join(results))
        self.status_var.set(f"完成: {len(results)} 个")

    def copy_to_clipboard(self):
        c = self.output_text.get("1.0", tk.END).strip()
        if c: 
            self.master.clipboard_clear()
            self.master.clipboard_append(c)
            self.status_var.set("已复制")

    def fill_to_aleph(self):
        cids = [c for c in self.output_text.get("1.0", tk.END).split("\n") if CIDValidator.is_valid_cid(c.strip())]
        if cids:
            self.integrated_app.aleph_manager.fill_cid_list(cids)
            self.status_var.set(f"已填充 {len(cids)} 个")

    def clear_all(self):
        self.input_text.delete("1.0", tk.END)
        self.output_text.delete("1.0", tk.END)

# ==================== 右侧Aleph管理器 ====================
class AlephManager:
    def __init__(self, master, app_path, logger, ipfs_path, integrated_app):
        self.master = master
        self.app_path = app_path
        self.logger = logger
        self.ipfs_path = ipfs_path
        self.integrated_app = integrated_app
        
        self.api_endpoints = list(Constants.ALEPH_API_ENDPOINTS)
        self.active_api_endpoint = None
        self.last_success_endpoint = None
                
        self.config_manager = AlephConfigManager(app_path, logger)
        self.config_manager.ensure_config_directory()
        
        # 初始化学习模块
        self.intelligence = NodeIntelligence(self.config_manager.config_dir, logger)
        # 读取路由状态（记忆上次成功节点）
        self._load_router_state()
        
        self.account_indices = {}
        self.unlinked_indices = {}
        self.active_account_name = None
        self._no_account_warned = False
        
        self.create_widgets()
        self.start_node_optimization()
    
    def start_node_optimization(self):
        self.logger.info("启动节点智能巡检服务...")
        thread = threading.Thread(target=self._node_optimization_loop, daemon=True)
        thread.start()

    def _node_optimization_loop(self):
        first_run = True
        while True:
            try:
                self._optimize_nodes_task(is_first_run=first_run)
                first_run = False
            except Exception as e:
                self.logger.error(f"节点巡检异常: {e}")
            time.sleep(300)

    def _optimize_nodes_task(self, is_first_run=False):
        """
        [智能优化]
        1. 获取节点：【改进】优先从当前活跃节点获取，失败则回退到官方节点
        2. 质量初筛：Score < 0.8 (80%) 的节点直接淘汰
        3. 智能排序：结合 (历史预测延迟 / Score) 选出最佳候选人
        4. 决赛圈实测：对 Top 选手进行 Ping 测试
        5. 决策：综合评分最高的胜出
        """
        try:
            if not is_first_run: self.logger.info("执行节点健康检查与学习...")

            # 1. 获取活跃节点列表 (动态 Discovery)
            candidates = []
            raw_nodes = []
            
            # 构建 Discovery 来源列表：优先用当前最快的节点，其次用官方列表
            discovery_sources = []
            if self.active_api_endpoint:
                discovery_sources.append(self.active_api_endpoint)
            
            for ep in Constants.ALEPH_API_ENDPOINTS:
                if ep not in discovery_sources:
                    discovery_sources.append(ep)
            
            discovery_path = "/api/v0/aggregates/0xa1B3bb7d2332383D96b7796B908fB7f7F3c2Be10.json?keys=corechannel&limit=50" # 这是官方的某个钱包路径
            
            # 轮询尝试获取列表
            discovery_success = False
            for base_url in discovery_sources:
                try:
                    full_url = f"{base_url}{discovery_path}"
                    resp = requests.get(full_url, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        raw_nodes = data.get('data', {}).get('corechannel', {}).get('nodes', [])
                        discovery_success = True
                        if not is_first_run: 
                            self.logger.info(f"从 {base_url} 成功获取到 {len(raw_nodes)} 个节点")
                        break
                except Exception:
                    continue
            
            if not discovery_success:
                if not is_first_run: self.logger.warning("所有节点无法连接，无法获取网络拓扑")
                # 即使获取列表失败，仍然继续测试官方节点
            
            # 解析节点数据
            for n in raw_nodes:
                score = n.get('score', 0)
                # 初筛: 活跃且评分 >= 0.8
                if n.get('status') == 'active' and score >= 0.80 and n.get('multiaddress'):
                    match = re.search(r'/ip4/([\d\.]+)', n['multiaddress'])
                    if match:
                        url = f"http://{match.group(1)}:4024"
                        candidates.append({
                            "name": n.get('name', 'Unk'), 
                            "url": url, 
                            "is_official": False,
                            "score": score
                        })

            # 2. 加入官方节点
            for url in Constants.ALEPH_API_ENDPOINTS:
                candidates.append({
                    "name": "Official", 
                    "url": url, 
                    "is_official": True, 
                    "score": 1.0
                })

            # 3. [智能选拔] 利用 历史性能 和 实时Score 对候选人进行预排序
            # 排序依据：预测延迟 / (Score^2) -> Score越高，分母越大，值越小，排名越前
            candidates.sort(key=lambda x: self.intelligence.get_predicted_performance(x['url'], x['is_official']) / (x['score'] ** 2))
            
            # 选取 Top 8 个节点进行实测
            test_targets = candidates[:8]
            
            # 确保当前节点也在测试列表中
            current_endpoint = self.active_api_endpoint or self.api_endpoints[0]
            if not any(t['url'] == current_endpoint for t in test_targets):
                test_targets.append({
                    "name": "Current", 
                    "url": current_endpoint, 
                    "is_official": "aleph" in current_endpoint,
                    "score": 1.0 # 假设当前节点也是好的
                })

            # 4. [实测与学习]
            scored_results = []
            
            for node in test_targets:
                url = node['url']
                latency = float('inf')
                success = False
                try:
                    start = time.time()
                    resp = requests.get(f"{url}/api/v0/info/public.json", timeout=2)
                    latency = (time.time() - start) * 1000
                    success = resp.status_code == 200
                except:
                    pass
                
                # 【改进】更新记忆 (传入节点名称)
                self.intelligence.record_observation(url, latency, success, name=node.get('name'))
                
                if success:
                    # 获取最新的综合评分 (越低越好)
                    predicted_perf = self.intelligence.get_predicted_performance(url, node['is_official'])
                    # 最终排名分 = 预测性能分 / Score
                    final_rank_score = predicted_perf / node['score']
                    
                    scored_results.append((final_rank_score, latency, node))

            if not scored_results: return

            # 按综合排名分排序
            scored_results.sort(key=lambda x: x[0])
            
            best_rank_score, best_latency, best_node = scored_results[0]
            best_url = best_node['url']
            
            # 5. 切换决策
            should_switch = False
            if is_first_run:
                should_switch = True
            elif best_url != current_endpoint:
                curr_stats = next((x for x in scored_results if x[2]['url'] == current_endpoint), None)
                if curr_stats:
                    curr_rank_score = curr_stats[0]
                    # 只有当新节点综合分 优于 当前节点 15% 以上才切换
                    if best_rank_score < curr_rank_score * 0.85:
                        should_switch = True
                else:
                    should_switch = True # 当前节点未响应

            if should_switch:
                new_list = [best_url]
                # 备选节点 (前3名)
                for _, _, n in scored_results[1:4]:
                    if n['url'] not in new_list: new_list.append(n['url'])
                # 官方兜底
                for off in Constants.ALEPH_API_ENDPOINTS:
                    if off not in new_list: new_list.append(off)
                
                self.api_endpoints = new_list
                self.active_api_endpoint = best_url
                
                # 日志中 Score 和 IP 显示
                score_display = f"{best_node['score']*100:.1f}%" if best_node['score'] < 1.0 else "Official"
                # 显式打印出 IP 地址 [best_url]
                msg = f"智能路由: 已连接最优节点 '{best_node['name']}' [{best_url}] (延迟:{best_latency:.0f}ms, 信誉:{score_display})"
                self.log(msg)
                self.master.after(0, lambda: self.status_var.set(msg))

        except Exception as e:
            self.logger.warning(f"智能优化任务出错: {e}")

    def create_widgets(self):
        main = ttk.Frame(self.master)
        main.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # 账户区域
        acc_frame = ttk.LabelFrame(main, text="账户管理", style='BigTitle.TLabelframe')
        acc_frame.pack(fill=tk.X, pady=5)
        
        fr1 = ttk.Frame(acc_frame)
        fr1.pack(fill=tk.X, pady=5)
        ttk.Label(fr1, text="创建新账户:").grid(row=0, column=0, padx=5)
        self.account_name_var = tk.StringVar()
        ttk.Entry(fr1, textvariable=self.account_name_var, width=30).grid(row=0, column=1, padx=5)
        ttk.Button(fr1, text="【创建】", width=10, command=self.create_account).grid(row=0, column=2, padx=5)
        
        fr2 = ttk.Frame(acc_frame)
        fr2.pack(fill=tk.X, pady=5)
        ttk.Label(fr2, text="当前账户:").grid(row=0, column=0, padx=5)
        self.account_list_var = tk.StringVar()
        self.account_combo = ttk.Combobox(fr2, textvariable=self.account_list_var, width=30, state="readonly")
        self.account_combo.grid(row=0, column=1, padx=5)
        ttk.Button(fr2, text="设为默认", width=10, command=self.set_as_default_account).grid(row=0, column=2, padx=2)
        ttk.Button(fr2, text="删除账户", width=10, command=self.delete_account).grid(row=0, column=3, padx=2)
        ttk.Button(fr2, text="打开路径", width=10, command=self.open_account_folder).grid(row=0, column=4, padx=2)
        ttk.Button(fr2, text="刷新", width=10, command=self.refresh_accounts).grid(row=1, column=2, padx=2)
        ttk.Button(fr2, text="文件列表", width=10, command=self.show_file_list).grid(row=1, column=3, padx=2)
        
        # CID操作区域
        cid_frame = ttk.LabelFrame(main, text="Aleph CID Pin操作", style='BigTitle.TLabelframe')
        cid_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        ttk.Label(cid_frame, text="CID列表 (支持 v0 和 v1):").pack(anchor=tk.W, padx=5)
        
        cid_input_frame, self.cid_text = UIHelper.create_scrolled_text(cid_frame, height=10)
        cid_input_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        btns = ttk.Frame(cid_frame)
        btns.pack(fill=tk.X, padx=5)
        ttk.Button(btns, text="←从左侧填充", width=12, command=self.integrated_app.cid_calculator.fill_to_aleph).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="粘贴", width=8, command=self.paste_cid).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="清空", width=8, command=self.clear_cid).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="删除CID", width=8, command=self.delete_cid).pack(side=tk.LEFT, padx=2)
        ttk.Button(btns, text="【PIN到Aleph】", width=12, command=self.pin_cid, style='Accent.TButton').pack(side=tk.LEFT, padx=10)
        ttk.Button(btns, text="【PIN-遍历所有账户】", width=17, command=self.pin_cid_all_accounts).pack(side=tk.LEFT, padx=2)
        
        # 日志
        log_frame = ttk.LabelFrame(main, text="操作日志", style='BigTitle.TLabelframe')
        log_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        log_container, self.log_text = UIHelper.create_scrolled_text(log_frame, height=12)
        log_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.status_var = tk.StringVar(value="准备就绪")
        ttk.Label(main, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W).pack(fill=tk.X, pady=5)
        
        self.master.after(10, self.refresh_accounts)

    def fill_cid_list(self, cids):
        if not cids: return
        curr = self.cid_text.get("1.0", tk.END).strip()
        if curr and not curr.endswith("\n"): self.cid_text.insert(tk.END, "\n")
        for c in cids: self.cid_text.insert(tk.END, c + "\n")
        self.status_var.set(f"已填充 {len(cids)} 个CID")

    def log(self, msg):
        def _ui():
            if not getattr(self, "log_text", None):
                return
            if not self.log_text.winfo_exists():
                return
            self.log_text.config(state=tk.NORMAL)
            self.log_text.insert(tk.END, msg + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state=tk.DISABLED)
        self.logger.info(msg)
        try: self.master.after(0, _ui)
        except: pass

    def _router_state_path(self):
        return os.path.join(self.config_manager.config_dir, "router_state.json")

    def _load_router_state(self):
        try:
            path = self._router_state_path()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.last_success_endpoint = data.get("last_success_endpoint")
        except Exception as exc:
            self.logger.warning(f"读取路由状态失败: {exc}")

    def _save_router_state(self):
        try:
            path = self._router_state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"last_success_endpoint": self.last_success_endpoint}, f)
        except Exception as exc:
            self.logger.warning(f"保存路由状态失败: {exc}")

    def run_aleph_command(self, cmd, input_text=None):
        self.log(f"执行: {cmd}")
        args = cmd.split() if isinstance(cmd, str) else list(cmd)
        # 基于历史成功的节点做轮替，优先尝试最近成功节点
        ordered = list(self.api_endpoints or [])
        if self.last_success_endpoint:
            le = None if self.last_success_endpoint == "default" else self.last_success_endpoint
            if le in ordered:
                ordered = [le] + [e for e in ordered if e != le]
            elif le:
                ordered = [le] + ordered

        # 在已有列表末尾追加一个 None，让 aleph_client 使用其内置默认节点（官方负载均衡）作为兜底
        endpoints = ordered + [None]
        last_err = ""
        # 外层尝试两轮，防止偶发 503 导致全军覆没
        for attempt in range(2):
            for idx, ep in enumerate(endpoints, 1):
                start_ts = time.time()
                if ep:
                    # 设置所有可能的环境变量
                    os.environ["ALEPH_API_SERVER"] = ep
                    os.environ["ALEPH_API_HOST"] = ep
                    os.environ["ALEPH_API_URL"] = ep
                    self.active_api_endpoint = ep
                else: 
                    # 清理之
                    os.environ.pop("ALEPH_API_SERVER", None)
                    os.environ.pop("ALEPH_API_HOST", None)
                    os.environ.pop("ALEPH_API_URL", None)
                    self.active_api_endpoint = None
                
                out, err, rc = run_aleph_cli(args, input_text)
                last_err = err

                # 记录一次真实调用的成功/失败，用于学习评分（用户体验直接驱动）
                try:
                    elapsed_ms = max((time.time() - start_ts) * 1000, 1)
                    is_success = (rc == 0 and not self._is_network_error(err))
                    node_name = "Official" if ep in Constants.ALEPH_API_ENDPOINTS else ("Default" if ep is None else "Custom")
                    self.intelligence.record_observation(ep or "default", elapsed_ms, is_success, name=node_name)
                except Exception:
                    pass

                if rc == 0 or not self._is_network_error(err):
                    if rc == 0:
                        self.last_success_endpoint = ep or "default"
                        self._save_router_state()
                        self.log(f"成功节点: {ep or '默认'}")
                    if out and "--json" not in args: self.log(f"输出: {out}")
                    if err: self.log(f"信息: {_shorten_error(err)}")
                    return out, err, rc
                
                # 网络错误则尝试下一个节点，同时记录失败原因
                fail_msg = f"[{idx}/{len(endpoints)}] 节点 {ep or '默认'} 失败: {_shorten_error(err) or '无错误输出'}"
                self.log(fail_msg)
                
                if idx < len(endpoints): time.sleep(1)
            # 一轮失败后再等一秒重试一轮
            time.sleep(1)
        
        self.log(f"所有节点连接失败，最后错误: {_shorten_error(last_err) or '无'}")
        return "", "", 1

    def _is_network_error(self, err):
        if not err:
            # 没有错误输出但返回码非0，也按网络类处理以触发回退
            return True
        keys = ["Cannot connect", "ClientConnectorError", "TimeoutError", "ConnectionResetError", "Service Unavailable", "BroadcastError", "Unexpected HTTP response"]
        if any(k in err for k in keys):
            return True
        # 捕获 5xx HTTP 响应
        return bool(re.search(r"\b5\d{2}\b", err))

    # 业务方法封装
    def create_account(self):
        name = self.account_name_var.get().strip()
        if not name: return messagebox.showerror("错误", "请输入账户名")
        def _t():
            self.status_var.set("创建中...")
            out, err, rc = self.run_aleph_command(["account", "create", "--key-format", "hexadecimal"], f"{name}\n\n")
            if rc != 0: out, err, rc = self.run_aleph_command(["account", "create"], f"{name}\n\n")
            if rc == 0:
                self.log("账户创建成功")
                self.master.after(1000, self.refresh_accounts)
                self.master.after(0, lambda: self.account_name_var.set(""))
            else: self.log(f"创建失败: {err}")
        threading.Thread(target=_t, daemon=True).start()

    def refresh_accounts(self):
        def _t():
            self.status_var.set("刷新账户...")
            folder = os.path.join(self.config_manager.config_dir, "private-keys")
            key_files = {}
            if os.path.isdir(folder):
                for f in os.listdir(folder):
                    p = Path(f)
                    if p.suffix == ".key" and p.stem.lower() != "default":
                        key_files[p.stem] = str(Path(folder) / p.name)
                # 清理空的 default.key
                default_key = Path(folder) / "default.key"
                if default_key.exists() and default_key.stat().st_size == 0:
                    try:
                        default_key.unlink()
                        self.log("已移除空的 default.key")
                    except Exception as exc:
                        self.log(f"移除 default.key 失败: {exc}")

            accs = []
            active = None
            a_idxs, u_idxs = {}, {}

            # 先检查/修复 config，避免空 config 导致 account list 报错
            cfg = os.path.join(self.config_manager.config_dir, "config.json")
            cfg_valid = False
            cfg_cleaned = False
            if os.path.exists(cfg):
                try:
                    with open(cfg, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    cfg_key = data.get("path")
                    cfg_chain = data.get("chain")
                    if cfg_key and cfg_chain and os.path.exists(cfg_key):
                        cfg_valid = True
                    else:
                        # 无效则清空
                        with open(cfg, "w", encoding="utf-8") as f:
                            json.dump({}, f)
                        cfg_cleaned = True
                except Exception as exc:
                    self.log(f"读取 config.json 失败: {exc}")

            # 如果无效且有现成 key，可自动写入首个 key，默认链 ETH
            if not cfg_valid and key_files:
                first_key = sorted(key_files.values())[0]
                try:
                    with open(cfg, "w", encoding="utf-8") as f:
                        json.dump({"path": first_key, "chain": "ETH"}, f)
                    cfg_valid = True
                    self.log(f"已自动设置 Aleph config.json 使用 {Path(first_key).name}")
                except Exception as exc:
                    self.log(f"写入 Aleph config.json 失败: {exc}")
            elif cfg_cleaned:
                self.log("已清理无效的 Aleph config.json")

            out, err, rc = (None, None, 1)
            if cfg_valid:
                out, err, rc = self.run_aleph_command(["account", "list"])

            if rc == 0 and out:
                curr, unlinked = 0, 1
                for line in out.splitlines():
                    m = re.search(r'[│|]\s+([\w-]+)\s+[│|].*[│|]\s+([*-])\s*[│|]', line)
                    if m:
                        n, act = m.group(1).strip(), m.group(2) == '*'
                        if n.lower() == "default":
                            continue
                        if n not in key_files:
                            continue  # 仅保留有实际密钥文件的账户
                        accs.append(n)
                        a_idxs[n] = curr + 1
                        if act:
                            active = n
                        else:
                            u_idxs[n] = unlinked
                            unlinked += 1
                        curr += 1

            # 若命令失败或未找到匹配，回退到本地文件列表
            if not accs:
                accs = sorted(key_files.keys())
                active = active or self.account_list_var.get()

            if active not in accs and accs:
                active = accs[0]

            self.master.after(0, lambda: self.update_list(accs, active, a_idxs, u_idxs))
            self.master.after(0, lambda: self.status_var.set("账户刷新成功"))

        # 如果 config.json 指向的 key 已不存在，清理之
        def _clean_config():
            cfg = os.path.join(self.config_manager.config_dir, "config.json")
            if os.path.exists(cfg):
                try:
                    import json as _json
                    with open(cfg, "r", encoding="utf-8") as f:
                        data = _json.load(f)
                    key_path = data.get("path")
                    if key_path and not os.path.exists(key_path):
                        with open(cfg, "w", encoding="utf-8") as f:
                            _json.dump({}, f)
                        self.log("已清理失效的 Aleph config.json")
                except Exception as exc:
                    self.log(f"清理 config.json 失败: {exc}")

        threading.Thread(target=_clean_config, daemon=True).start()
        threading.Thread(target=_t, daemon=True).start()

    def update_list(self, accs, active, a_idx, u_idx):
        sorted_accs = sorted(accs)
        self.account_combo['values'] = sorted_accs
        if active in sorted_accs:
            self.account_combo.set(active)
        elif sorted_accs:
            self.account_combo.set(sorted_accs[0])
        self.account_indices = a_idx
        self.unlinked_indices = u_idx
        self.active_account_name = active
        if not sorted_accs and not self._no_account_warned:
            self._no_account_warned = True
            messagebox.showinfo("提示", "你还没有账户，请先创建一个账户吧")

    def open_account_folder(self):
        """打开当前选中账户的密钥目录"""
        name = self.account_list_var.get()
        if not name:
            return self.log("请先选择账户")
        folder = os.path.join(self.config_manager.config_dir, "private-keys")
        if not os.path.isdir(folder):
            return self.log("未找到账户目录")
        try:
            if os.name == "nt":
                os.startfile(folder)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", folder])
            self.log(f"已打开账户目录: {folder}")
        except Exception as exc:
            self.log(f"打开账户目录失败: {exc}")

    def delete_account(self):
        name = self.account_list_var.get().strip()
        if not name:
            return
        if not messagebox.askyesno("确认", f"删除 {name}?"): return
        def _t():
            kp = os.path.join(self.config_manager.config_dir, "private-keys", f"{name}.key")
            if os.path.exists(kp):
                try:
                    os.remove(kp)
                    self.log(f"已删除 {name}")
                except Exception as exc:
                    self.log(f"删除失败 {kp}: {exc}")
            else:
                self.log(f"未找到要删除的密钥文件: {kp}")
            # 如果配置指向该账户，重置 config.json
            cfg = os.path.join(self.config_manager.config_dir, "config.json")
            if os.path.exists(cfg):
                try:
                    with open(cfg, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    key_path = data.get("path")
                    if key_path and name + ".key" in key_path:
                        with open(cfg, "w", encoding="utf-8") as f:
                            json.dump({}, f)
                        self.log("已重置 Aleph config.json")
                except Exception as exc:
                    self.log(f"重置 config.json 失败: {exc}")
            # 额外清理 default.key（若存在且不是当前选择的账户）
            default_key = os.path.join(self.config_manager.config_dir, "private-keys", "default.key")
            if os.path.exists(default_key):
                try:
                    os.remove(default_key)
                    self.log("已移除 default.key")
                except Exception as exc:
                    self.log(f"移除 default.key 失败: {exc}")
            self.master.after(0, self.refresh_accounts)
        threading.Thread(target=_t, daemon=True).start()

    def set_as_default_account(self):
        """将选中的账户写入 config.json 作为默认"""
        name = self.account_list_var.get().strip()
        if not name:
            return messagebox.showwarning("提示", "请先选择账户")
        kp = os.path.join(self.config_manager.config_dir, "private-keys", f"{name}.key")
        if not os.path.exists(kp):
            return messagebox.showerror("错误", f"未找到账户密钥文件: {kp}")
        cfg = os.path.join(self.config_manager.config_dir, "config.json")
        chain = "ETH"
        try:
            if os.path.exists(cfg):
                with open(cfg, "r", encoding="utf-8") as f:
                    data = json.load(f)
                chain = data.get("chain") or chain
        except Exception:
            pass
        try:
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({"path": kp, "chain": chain}, f)
            self.log(f"已将 {name} 设为默认账户")
            messagebox.showinfo("完成", f"已将 {name} 设置为默认账户")
        except Exception as exc:
            self.log(f"写入默认账户失败: {exc}")
            messagebox.showerror("错误", f"写入默认账户失败：{exc}")

    def _get_selected_key_file(self):
        """根据当前选中的账户返回私钥文件路径"""
        name = self.account_list_var.get()
        if not name:
            self.log("未选择账户")
            return None
        kp = os.path.join(self.config_manager.config_dir, "private-keys", f"{name}.key")
        if not os.path.exists(kp):
            self.log(f"未找到账户密钥文件: {kp}")
            return None
        return kp

    # CID 粘贴
    def paste_cid(self):
        try:
            c = self.master.clipboard_get().strip()
            if c: 
                curr = self.cid_text.get("1.0", tk.END).strip()
                if curr: self.cid_text.insert(tk.END, "\n")
                self.cid_text.insert(tk.END, c)
        except: pass

    def clear_cid(self): self.cid_text.delete("1.0", tk.END)

    def pin_cid(self):
        raw = self.cid_text.get("1.0", tk.END).split("\n")
        cids = [c.strip() for c in raw if c.strip()]
        if not cids: return messagebox.showinfo("提示", "无CID")
        key_file = self._get_selected_key_file()
        if not key_file:
            return messagebox.showerror("错误", "未找到选中账户的密钥文件，无法执行 pin")
        
        def _t():
            for i, c in enumerate(cids):
                self.status_var.set(f"PIN ({i+1}/{len(cids)}): {c}")
                # 注：相比于单独版本的aleph助手，内置版移除了强制转换 v1 -> v0 的逻辑
                # 并且同时支持 v0 (Qm...) 和 v1 (b...)
                
                if CIDValidator.is_valid_cid(c):
                    out, err, rc = self.run_aleph_command(["file", "pin", c, "--private-key-file", key_file])
                    if rc == 0: self.log(f"PIN成功: {c}")
                    else: self.log(f"PIN失败: {c} - {err}")
                else: self.log(f"无效CID: {c}")
                time.sleep(Constants.PIN_INTERVAL)
            self.status_var.set("PIN任务结束")
            # 任务完成后清空输入框
            self.master.after(0, self.clear_cid)
            
        threading.Thread(target=_t, daemon=True).start()

    def _get_key_file_by_name(self, name):
        folder = os.path.join(self.config_manager.config_dir, "private-keys")
        kp = os.path.join(folder, f"{name}.key")
        return kp if os.path.exists(kp) else None

    def pin_cid_all_accounts(self):
        raw = self.cid_text.get("1.0", tk.END).split("\n")
        cids = [c.strip() for c in raw if c.strip()]
        if not cids:
            return messagebox.showinfo("提示", "无CID")

        accounts = list(self.account_combo['values'])
        if not accounts:
            return messagebox.showerror("错误", "未找到任何账户")

        rounds = simpledialog.askinteger("遍历轮次", "请输入遍历轮次（默认 1）", initialvalue=1, minvalue=1)
        if not rounds:
            return

        def _t():
            for r in range(1, rounds + 1):
                self.status_var.set(f"开始第 {r}/{rounds} 轮遍历")
                for acc in accounts:
                    key_file = self._get_key_file_by_name(acc)
                    if not key_file:
                        self.log(f"跳过账户 {acc}（未找到密钥）")
                        continue
                    for i, c in enumerate(cids):
                        self.status_var.set(f"第{r}/{rounds}轮 - {acc}: PIN ({i+1}/{len(cids)}) {c}")
                        if CIDValidator.is_valid_cid(c):
                            out, err, rc = self.run_aleph_command(["file", "pin", c, "--private-key-file", key_file])
                            if rc == 0:
                                self.log(f"[{acc}] PIN成功: {c}")
                            else:
                                self.log(f"[{acc}] PIN失败: {c} - {err}")
                        else:
                            self.log(f"[{acc}] 无效CID: {c}")
                        time.sleep(Constants.PIN_INTERVAL)
                if r < rounds:
                    time.sleep(Constants.PIN_INTERVAL)

            self.status_var.set("遍历PIN任务结束")
            self.master.after(0, self.clear_cid)

        threading.Thread(target=_t, daemon=True).start()

    def delete_cid(self):
        if not messagebox.askyesno("确认", "删除列表中的CID?"): return
        raw = self.cid_text.get("1.0", tk.END).split("\n")
        cids = [c.strip() for c in raw if c.strip()]
        key_file = self._get_selected_key_file()
        if not key_file:
            messagebox.showerror("错误", "未找到选中账户的密钥文件，无法删除")
            return
        def _t():
            self.status_var.set("获取文件列表...")
            out, err, rc = self.run_aleph_command(["file", "list", "--json", "--private-key-file", key_file])
            if rc == 0:
                try:
                    # 增加空值检查
                    if not out or not out.strip():
                        self.log("警告: 获取到的文件列表数据为空")
                        files = []
                    else:
                        files = json.loads(out).get('files', [])
                    
                    h_map = {f.get('file_hash'): f.get('item_hash') for f in files}
                    for c in cids:
                        ih = h_map.get(c)
                        if ih:
                            out_del, err_del, rc_del = self.run_aleph_command([
                                "file", "forget", ih, "--private-key-file", key_file
                            ])
                            if rc_del == 0: self.log(f"已删除: {c}")
                            else: self.log(f"删除失败: {err_del}")
                        else:
                            self.log(f"未在当前账户列表中找到CID: {c}")
                except json.JSONDecodeError:
                    self.log(f"解析文件列表失败，返回内容非JSON格式: {out}")
                except Exception as e:
                    self.log(f"处理删除任务时出错: {e}")
            else:
                self.log(f"获取文件列表失败: {err}")
            
            self.status_var.set("删除任务结束")
            self.show_file_list()
        threading.Thread(target=_t, daemon=True).start()

    def show_file_list(self):
        key_file = self._get_selected_key_file()
        if not key_file:
            messagebox.showerror("错误", "未找到选中账户的密钥文件，无法获取文件列表")
            return
        def _t():
            self.status_var.set("加载列表...")
            out, err, rc = self.run_aleph_command(["file", "list", "--json", "--private-key-file", key_file])
            if rc == 0:
                try:
                    # 增加空值检查
                    if not out or not out.strip():
                        self.log("文件列表为空或未返回数据")
                    else:
                        d = json.loads(out)
                        self.log(f"--- 文件列表 ({d.get('address')}) ---")
                        for f in d.get('files', []):
                            sz = f.get('size', 0)/1024/1024
                            self.log(f"{f.get('file_hash')} | {sz:.2f}MB")
                        self.log("-" * 30)
                except json.JSONDecodeError:
                    self.log(f"解析文件列表JSON失败: {out}")
                except Exception as e:
                    self.log(f"显示列表时发生错误: {e}")
                self.status_var.set("列表加载完成")
            else:
                self.log(f"刷新列表失败: {err}")
                self.status_var.set("列表加载失败")
        threading.Thread(target=_t, daemon=True).start()

class AlephIntegratedApp:
    def __init__(self, master, app_path, config_file_path=None, kubo_path=None, repo_path=None, allow_ipfs_init=True, logger=None):
        self.master = master
        self.app_path = app_path
        self.aleph_config_dir = os.path.join(self.app_path, Constants.ALEPH_CONFIG_DIR)
        self.provided_kubo_path = kubo_path
        self.repo_path = repo_path
        self.allow_ipfs_init = allow_ipfs_init
        os.environ["ALEPH_CONFIG_HOME"] = self.aleph_config_dir
        os.environ["ALEPH_HOME"] = self.aleph_config_dir
        self.logger = logger or self._setup_logger()
        self._setup_window()
        self.master.after(50, self._init_comps)

    def _init_comps(self):
        paned = tk.PanedWindow(self.master, orient=tk.HORIZONTAL, sashwidth=5)
        paned.pack(fill=tk.BOTH, expand=1)
        lf = ttk.Frame(paned, width=Constants.MIN_LEFT_FRAME_WIDTH)
        paned.add(lf, minsize=Constants.MIN_LEFT_FRAME_WIDTH)
        rf = ttk.Frame(paned)
        paned.add(rf, minsize=600)
        self.cid_calculator = CIDCalculator(
            lf,
            self.app_path,
            self.logger,
            self,
            ipfs_path=getattr(self, "provided_kubo_path", None),
            repo_dir=self.repo_path,
            allow_repo_init=self.allow_ipfs_init,
        )
        self.aleph_manager = AlephManager(rf, self.app_path, self.logger, self.cid_calculator.ipfs_path, self)

    def _setup_logger(self):
        log_dir = os.path.join(self.app_path, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        f = os.path.join(log_dir, f'aleph_{datetime.now().strftime("%Y%m%d")}.log')
        logging.basicConfig(level=logging.INFO, handlers=[logging.FileHandler(f, encoding='utf-8')])
        return logging.getLogger(__name__)

    def _setup_window(self):
        sw, sh = self.master.winfo_screenwidth(), self.master.winfo_screenheight()
        w, h = int(sw*Constants.WINDOW_WIDTH_RATIO), int(sh*Constants.WINDOW_HEIGHT_RATIO)
        self.master.geometry(f'{w}x{h}+{int((sw-w)/2)}+{int((sh-h)/2)}')
        self.master.minsize(Constants.MIN_WINDOW_WIDTH, Constants.MIN_WINDOW_HEIGHT)
        self.master.title("Aleph 分享助手 (内置版)")
        try: self.master.iconbitmap(os.path.join(self.app_path, "assets", "aleph_managerGUI.ico"))
        except: pass

def main():
    root = TkinterDnD.Tk()
    if getattr(sys, 'frozen', False): p = os.path.dirname(sys.executable)
    else: p = os.path.dirname(os.path.abspath(__file__))
    app = AlephIntegratedApp(root, p)
    root.mainloop()

if __name__ == "__main__":
    main()
