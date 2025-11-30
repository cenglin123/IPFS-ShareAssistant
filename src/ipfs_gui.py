# src\ipfs_gui.py

import os
import sys
import shutil

# ============ GUI 相关环境初始化整备 ============ 

def _prepare_tk_env(base_dir):
    """Ensure Tcl/Tk paths are set when using the embeddable runtime."""
    runtime_dir = os.path.join(base_dir, "runtime")
    tcl_dir = os.path.join(runtime_dir, "tcl")
    # 让 Windows 在运行时目录内查找 tk/tcl 相关 DLL
    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(runtime_dir)
        except Exception:
            pass
    if os.path.isdir(tcl_dir):
        os.environ.setdefault("TCL_LIBRARY", os.path.join(tcl_dir, "tcl8.6"))
        os.environ.setdefault("TK_LIBRARY", os.path.join(tcl_dir, "tk8.6"))

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # src/ipfs_gui.py -> 项目根目录

_prepare_tk_env(BASE_DIR)

# 确保项目根目录在 sys.path（嵌入式运行时默认只包含 runtime\）
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
src_dir = os.path.join(BASE_DIR, "src")
if os.path.isdir(src_dir) and src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# 切换工作目录
try:
    os.chdir(BASE_DIR)
except Exception:
    pass

def _is_dir_empty(path: str) -> bool:
    for _, dirs, files in os.walk(path):
        if files:
            return False
    return True

# 声明 Aleph 相关路径到根目录
os.environ.setdefault("ALEPH_CONFIG_HOME", os.path.join(BASE_DIR, ".aleph-im"))
os.environ.setdefault("ALEPH_HOME", os.path.join(BASE_DIR, ".aleph-im"))

# 创建 Aleph 相关目录
def _consolidate_shadow_dir(name):
    shadow_root = os.path.join(BASE_DIR, "src")
    shadow_path = os.path.join(shadow_root, name)
    target_path = os.path.join(BASE_DIR, name)
    if not os.path.exists(shadow_path):
        return
    try:
        if not os.path.exists(target_path):
            shutil.move(shadow_path, target_path)
            return
        
        if _is_dir_empty(shadow_path):
            shutil.rmtree(shadow_path, ignore_errors=True)
            return
        
        for root, dirs, files in os.walk(shadow_path):
            rel = os.path.relpath(root, shadow_path)
            dest_root = os.path.join(target_path, rel) if rel != "." else target_path
            os.makedirs(dest_root, exist_ok=True)
            for f in files:
                src_f = os.path.join(root, f)
                dst_f = os.path.join(dest_root, f)
                if not os.path.exists(dst_f):
                    shutil.move(src_f, dst_f)
        shutil.rmtree(shadow_path, ignore_errors=True)
    except Exception:
        pass

# 合并以下目录
for name in [".aleph-im", "logs", "plugins", "kubo"]:
    _consolidate_shadow_dir(name) # 

# 导入所需库
import tkinter as tk
from tkinter import messagebox, ttk, filedialog
from tkinterdnd2 import DND_FILES, TkinterDnD
import subprocess
import threading
import re
import json
import logging
from datetime import datetime
import webbrowser
import urllib.parse
from urllib.parse import urljoin, quote
import win32gui
import win32con
import win32api
import pywintypes
import concurrent.futures
import random
import shlex

# 导入助手的标准模块（感兴趣你也可以自己定义一些模块，当然我更推荐用 py 脚本放到 plugins 目录下，然后通过插件启动器启动，更方便一些）
from utils import EmbeddedKubo, IntegratedApp, save_config_file, IPFSCleaner

# 程序路径处理
application_path = BASE_DIR

# ============ GUI 相关环境初始化整备完毕 ============ 

# 创建主应用类
class IPFSApp:
    """IPFS 分享助手主应用"""
    
    WM_TASKBAR = win32con.WM_USER + 1

    def __init__(self, root):
        self.root = root
        self.app_path = application_path
        
        # 初始化基础组件
        self._init_logger()
        self._init_runtime_environment()
        self._load_config()
        self._init_ipfs()
        self._init_variables()
        
        # 设置窗口和UI
        self._setup_window()
        self._create_main_ui()
        self._bind_context_menus()
        
        # 设置窗口几何位置
        self._apply_window_geometry() 
        
        # 延迟设置分隔条位置（确保窗口已完全显示）
        if not self.simple_mode and hasattr(self, 'paned_window'):
            self.root.after(100, self._set_paned_window_position)
        
        # 系统托盘
        self.hwnd = self.create_hidden_window()
        self.create_tray_icon()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def update_filecoin_config(self):
        """更新Filecoin配置并保存"""
        try:
            config_path = os.path.join(self.app_path, 'config.json')
            new_config = {'use_filecoin': self.use_filecoin.get()}
            save_config_file(config_path, new_config, self.logger)
            self.logger.info(f"Filecoin配置已更新: {self.use_filecoin.get()}")
        except Exception as e:
            self.logger.error(f"更新Filecoin配置失败: {e}")

    def on_cid_version_changed(self, event):
        """CID版本下拉菜单选择事件处理"""
        selected_text = self.cid_version_text.get()
        # 将选择的文本转换为对应的整数值
        version_map = {
            "CID v0": 0,
            "CID v1": 1,
            "file -> v0 -> v1": 2,
            "file -> v1 -> v0": 3
        }
        self.cid_version.set(version_map.get(selected_text, 1))  # 默认为1，即 CID v1

    # ==================== 1.初始化方法 ====================
    
    def _init_logger(self):
        """初始化日志系统"""
        log_dir = os.path.join(self.app_path, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f'ipfs_importer_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
        
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Application started")

    def _init_runtime_environment(self):
        """初始化嵌入式Python运行环境相关路径"""
        self.runtime_dir = os.path.join(self.app_path, 'runtime')
        self.plugins_dir = os.path.join(self.app_path, 'plugins')
        os.makedirs(self.plugins_dir, exist_ok=True)

        self.runtime_python = self._resolve_runtime_python()
        self.runtime_pythonw = self._resolve_runtime_python(prefer_windowed=True)
        self.plugin_window = None
        self.crust_window = None
        self.aleph_window = None

        if self.runtime_python:
            self.logger.info(f"Runtime Python detected: {self.runtime_python}")
        else:
            self.logger.warning("Runtime Python not found, falling back to current interpreter")

    def _resolve_runtime_python(self, prefer_windowed=False):
        """优先选择嵌入式Python路径"""
        candidates = []
        if prefer_windowed:
            candidates.append(os.path.join(self.runtime_dir, 'pythonw.exe'))

        candidates.extend([
            os.path.join(self.runtime_dir, 'python.exe'),
            os.path.join(self.runtime_dir, 'pythonw.exe') if not prefer_windowed else None,
            sys.executable
        ])

        for candidate in candidates:
            if candidate and os.path.exists(candidate):
                return candidate
        return None

    def _load_config(self):
        """加载配置文件"""
        config_path = os.path.join(self.app_path, 'config.json')
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    self.config = json.load(f)
                self.logger.info("Configuration loaded successfully")
            except json.JSONDecodeError:
                self.logger.error("Error decoding config file")
                self.config = {'use_filecoin': False}
        else:
            self.logger.info("Config file not found, using default settings")
            self.config = {}
        
        self.repo_path = self._determine_repo_path()
        self.proxy = self.config.get('proxy', None)

    def _init_ipfs(self):
        """初始化IPFS相关组件"""
        auto_update = self.config.get('auto_update_kubo', False)
        self.kubo = EmbeddedKubo(self.app_path, self.logger, self.repo_path, auto_update)
        self.kubo.start_daemon()
        self.actual_api_address = self.config.get('api', self.kubo.api_url)
        self.logger.info(f"Kubo started. API: {self.actual_api_address}")

    def _init_variables(self):
        """初始化UI变量"""
        self.pin_var = tk.BooleanVar(value=self.config.get('pin_after_import', False))
        self.cid_version = tk.IntVar(value=1)
        self.cid_version_text = tk.StringVar(value="CID v1")  # 用于下拉菜单的字符串变量
        self.use_filecoin = tk.BooleanVar(value=self.config.get('use_filecoin', False))
        self.minimize_to_tray = tk.BooleanVar(value=self.config.get('minimize_to_tray', False))
        self.auto_update_kubo = tk.BooleanVar(value=self.config.get('auto_update_kubo', False))
        self.enable_balancer_var = tk.BooleanVar(value=self.config.get('enable_balancer_var', False))
        self.simple_mode = self.config.get('default_simple_mode', False)
        self.default_simple_mode = tk.BooleanVar(value=self.config.get('default_simple_mode', False)) # 复选框变量
        self.gateway_var = tk.StringVar()
        
        self.importing = False
        self.stop_import = False
        self.gateways = []
        self.cid_best_gateways = {}


    # ==================== 属性方法：根据当前模式返回正确的组件 ====================
    
    @property
    def cid_input_text(self):
        """返回当前模式的CID输入框"""
        return self.cid_input_text_simple if self.simple_mode else self.cid_input_text_advanced
    
    @property
    def cid_output_text(self):
        """返回当前模式的CID输出框"""
        return self.cid_output_text_simple if self.simple_mode else self.cid_output_text_advanced
    
    @property
    def links_text(self):
        """返回当前模式的链接文本框"""
        return self.links_text_simple if self.simple_mode else self.links_text_advanced
    
    @property
    def gateway_dropdown(self):
        """返回当前模式的网关下拉框"""
        return self.gateway_dropdown_simple if self.simple_mode else self.gateway_dropdown_advanced

    
    @property
    def speed_test_button(self):
        """返回当前模式的测速按钮"""
        return self.speed_test_button_simple if self.simple_mode else self.speed_test_button_advanced
    
    @property
    def cid_calc_button_gui(self):
        """返回当前模式的CID计算按钮"""
        return self.cid_calc_button_gui_simple if self.simple_mode else self.cid_calc_button_gui_advanced
    
    @property
    def webui_button(self):
        """返回当前模式的WebUI按钮"""
        return self.webui_button_simple if self.simple_mode else self.webui_button_advanced
    
    @property
    def cid_status_label(self):
        """返回当前模式的CID状态标签"""
        return self.cid_status_label_simple if self.simple_mode else self.cid_status_label_advanced
    
    @property
    def progress_bar(self):
        """返回当前模式的进度条"""
        return self.progress_bar_simple if self.simple_mode else self.progress_bar_advanced

    # ==================== 辅助方法 ====================
    
    def _update_button_state(self, button_base_name, state):
        """同时更新两个模式中的按钮状态"""
        simple_button = getattr(self, f'{button_base_name}_simple', None)
        advanced_button = getattr(self, f'{button_base_name}_advanced', None)
        
        if simple_button:
            simple_button.config(state=state)
        if advanced_button:
            advanced_button.config(state=state)

    def _determine_repo_path(self):
        """确定IPFS仓库路径"""
        repo_path = self.config.get('repo_path')
        if not repo_path:
            repo_path = self._find_ipfs_repo()
        self.logger.info(f"Using IPFS repository path: {repo_path}")
        return repo_path

    def _find_ipfs_repo(self):
        """查找IPFS仓库位置"""
        possible_locations = []
        
        # 从配置文件
        config_file = os.path.join(self.app_path, "config.json")
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                config = json.load(f)
                if "repo_path" in config:
                    possible_locations.append(config["repo_path"])
        
        # 从环境变量
        if "IPFS_PATH" in os.environ:
            possible_locations.append(os.environ["IPFS_PATH"])
        
        # 默认位置
        possible_locations.extend([
            os.path.join(os.path.expanduser("~"), ".ipfs"),
            os.path.join(os.getenv("APPDATA"), "IPFS"),
            os.path.join(self.app_path, ".ipfs"),
        ])
        
        # 检查路径
        for location in possible_locations:
            if location and os.path.exists(os.path.join(location, "config")):
                self.logger.info(f"Found IPFS repository at: {location}")
                return location
        
        default_path = os.path.join(self.app_path, ".ipfs")
        self.logger.info(f"No existing IPFS repository found. Using: {default_path}")
        return default_path

    # ==================== 2.窗口设置 ====================
    
    def _setup_window(self):
        """设置主窗口"""
        mode_text = " - 简洁模式" if self.simple_mode else ""
        self.root.title(f"IPFS 分享助手 v1.2.2-20251130 by 层林尽染{mode_text}")

        # 窗口大小在 _apply_window_geometry 中设置
        
        # 图标
        self.icon_path = os.path.join(self.app_path, "assets", "ipfs_importer_icon.ico")
        if os.path.exists(self.icon_path):
            self.root.iconbitmap(self.icon_path)
        
        # 样式
        self.set_global_style()

    def set_global_style(self):
        """设置全局样式"""
        style = ttk.Style()
        font_family = self.get_system_font()
        
        # 配置所有ttk组件
        for widget_type in ['TEntry', 'TButton', 'TCheckbutton', 'TRadiobutton', 
                           'TCombobox', 'TFrame', 'TLabelframe', 'TNotebook']:
            style.configure(widget_type, font=(font_family, 10))
        
        style.configure('TNotebook.Tab', font=(font_family, 10))
        style.configure('BigTitle.TLabelframe', font=(font_family, 14, 'bold'))
        style.configure('BigTitle.TLabelframe.Label', font=(font_family, 14, 'bold'))
        
        self.root.option_add('*Text*highlightThickness', 0)

    def get_system_font(self):
        """获取系统字体"""
        if sys.platform.startswith('win'):
            return 'Microsoft YaHei'
        elif sys.platform.startswith('darwin'):
            return 'PingFang SC'
        else:
            return 'Noto Sans CJK SC'

    # ==================== 3.UI创建 ====================
    
    def _create_main_ui(self):
        """创建主界面"""
        self.main_frame = tk.Frame(self.root)
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 先创建状态栏（pack到底部）
        self._create_status_bar()
        
        # 创建两个容器，分别存放简洁模式和高级模式的UI
        self.simple_container = tk.Frame(self.main_frame)
        self.advanced_container = tk.Frame(self.main_frame)
        
        # 先创建高级模式UI（因为它是主容器）
        self._create_advanced_mode_ui_in_container()
        
        # 再创建简洁模式UI
        self._create_simple_mode_ui_in_container()
        
        # 根据当前模式显示对应的UI（pack到剩余空间）
        if self.simple_mode:
            self.advanced_container.pack_forget()
            self.simple_container.pack(fill=tk.BOTH, expand=True)
        else:
            self.simple_container.pack_forget()
            self.advanced_container.pack(fill=tk.BOTH, expand=True)
        
        # 加载网关
        self.load_gateways()

    def _create_advanced_mode_ui_in_container(self):
        """在容器中创建高级模式界面"""
        # 左右分栏
        self.paned_window = ttk.PanedWindow(self.advanced_container, orient=tk.HORIZONTAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True)
        
        self.left_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(self.left_frame, weight=1)
        
        self.right_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(self.right_frame, weight=1)
        
        # 创建左右面板
        self.create_left_widgets()
        self.create_right_widgets()

    def create_left_widgets(self):
        """创建左侧面板"""
        outer_frame, scrollable_frame = self._create_smart_scrollable_frame(self.left_frame)
        outer_frame.pack(fill="both", expand=True)
        
        # 主输入框区域
        self._create_main_inputs_section(scrollable_frame)
        
        # 配置区域
        self._create_config_section(scrollable_frame)

    def _create_main_inputs_section(self, parent):
        """创建主输入框区域"""
        frame = ttk.LabelFrame(parent, text="MAIN INPUTs 主输入框", style='BigTitle.TLabelframe')
        frame.pack(fill="both", expand=True, padx=20, pady=(10, 5))
        
        # CID输入
        ttk.Label(frame, text="1. a.输入CIDs从IPFS路径导入(每行一个)    或 b.拖入本地文件/文件夹从本地导入").pack(anchor="w")
        cid_frame, self.cid_text_advanced = self._create_scrolled_text(frame, height=6)
        cid_frame.pack(fill="both", expand=True, pady=1)
        ttk.Label(frame, text="示例: bafybei...xf44 或 C:\\path\\to\\file.txt").pack(anchor="w")
        self.cid_text_advanced.drop_target_register(DND_FILES)
        self.cid_text_advanced.dnd_bind('<<Drop>>', self.drop_on_cid)
        
        # 文件名输入
        ttk.Label(frame, text="2. 输入文件名 (每行一个)     或直接拖入文件获取文件名，在 1.b 中拖入本地文件时会自动填充文件名").pack(anchor="w", pady=(8, 0))
        name_frame, self.name_text_advanced = self._create_scrolled_text(frame, height=6)
        name_frame.pack(fill="both", expand=True, pady=1)
        ttk.Label(frame, text="当输入为 CID 时与 CID 逐一对应, 若不指定文件名则默认以 CID 作为文件名, 示例: myfile.7z.006").pack(anchor="w")
        self.name_text_advanced.drop_target_register(DND_FILES)
        self.name_text_advanced.dnd_bind('<<Drop>>', self.drop_on_filename)
        
        # 目标路径
        ttk.Label(frame, text="3. 导入至 IPFS 目标路径:").pack(anchor="w", pady=(5, 0))
        self.path_entry_advanced = ttk.Entry(frame)
        self.path_entry_advanced.pack(fill="x", pady=1)
        ttk.Label(frame, text="默认为根目录, 可用 / 组织文件路径, 示例: /upperfolder/subfolder").pack(anchor="w")
        self.path_entry_advanced.drop_target_register(DND_FILES)
        self.path_entry_advanced.dnd_bind('<<Drop>>', self.drop_on_path)
        
        # 按钮组
        self._create_main_buttons(frame)

    def _create_main_buttons(self, parent):
        """创建主输入框的按钮组"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(pady=2, fill="x")
        
        self.execute_button_advanced = ttk.Button(button_frame, text="导入", command=self.start_execute, width=10)
        self.execute_button_advanced.pack(side="left", padx=2, pady=5)
        
        self.stop_button_advanced = ttk.Button(button_frame, text="停止", command=self.stop_execute, state=tk.DISABLED, width=10)
        self.stop_button_advanced.pack(side="left", padx=2, pady=5)
        
        self.import_json_button_advanced = ttk.Button(button_frame, text="读取JSON文件", width=15, command=self.import_json_file_gui)
        self.import_json_button_advanced.pack(side="left", padx=2, pady=5)
        
        self.clear_button_advanced = ttk.Button(button_frame, text="程序复位", command=self.clear_window, width=12)
        self.clear_button_advanced.pack(side="left", padx=2, pady=5)

    def _create_config_section(self, parent):
        """创建配置区域"""
        frame = ttk.LabelFrame(parent, text="CONFIG 配置", style='BigTitle.TLabelframe')
        frame.pack(fill="x", padx=20, pady=(5, 10))
        
        # 仓库地址
        ttk.Label(frame, text="4. IPFS 仓库地址:").pack(anchor="w", pady=(5, 0))
        repo_frame = ttk.Frame(frame)
        repo_frame.pack(fill="x", pady=2)
        self.repo_entry_advanced = ttk.Entry(repo_frame)
        self.repo_entry_advanced.pack(side="left", fill="x", expand=True)
        self.repo_entry_advanced.insert(0, self.repo_path)
        ttk.Button(repo_frame, text="浏览", command=self.browse_repo).pack(side="right")
        ttk.Label(frame, text="默认为环境变量中的仓库地址, 可自行改动, 示例: C:\\Users\\xxx\\.ipfs").pack(anchor="w")
        
        # API地址
        ttk.Label(frame, text="5. IPFS API 地址").pack(anchor="w", pady=(5, 0))
        api_frame = ttk.Frame(frame)
        api_frame.pack(fill="x", pady=2)
        self.api_entry_advanced = ttk.Entry(api_frame)
        self.api_entry_advanced.pack(side="left", fill="x", expand=True)
        if self.actual_api_address:
            self.api_entry_advanced.insert(0, self.actual_api_address)
        ttk.Button(api_frame, text="应用此API", command=self.set_api_from_ui).pack(side="right")
        ttk.Label(frame, text="此地址为仓库所对应的 IPFS API 地址, 修改仓库地址时会自动获取, 也可以自行设置服务器api").pack(anchor="w")
        
        # 代理设置
        ttk.Label(frame, text="6. 代理设置 (用于特定网关测速):").pack(anchor="w", pady=(5, 0))
        proxy_frame = ttk.Frame(frame)
        proxy_frame.pack(fill="x", pady=2)
        self.proxy_entry_advanced = ttk.Entry(proxy_frame)
        self.proxy_entry_advanced.pack(side="left", fill="x", expand=True)
        if self.proxy:
            self.proxy_entry_advanced.insert(0, self.proxy)
        ttk.Button(proxy_frame, text="应用此代理", command=self.set_proxy_from_ui).pack(side="right")
        ttk.Label(frame, text="示例: http://127.0.0.1:7879 或 socks5://127.0.0.1:1080 根据自己的代理端口进行设置").pack(anchor="w")
        
        # 复选框
        self._create_config_checkboxes(frame)

    def _create_config_checkboxes(self, parent):
        """创建配置复选框"""
        cb_frame = ttk.Frame(parent)
        cb_frame.pack(anchor="w", pady=(5, 5))
        
        ttk.Checkbutton(cb_frame, text="导入后固定在本地(可能会降低运行速度)", 
                    variable=self.pin_var).grid(row=0, column=0, padx=5)
        ttk.Checkbutton(cb_frame, text="关闭时最小化到任务栏", 
                    variable=self.minimize_to_tray, 
                    command=self.update_minimize_to_tray).grid(row=0, column=1, padx=5)
        ttk.Checkbutton(cb_frame, text="自动更新kubo", # 勾选后程序启动时会检测kubo更新，如果网速慢可能会卡一会
                    variable=self.auto_update_kubo, 
                    command=self.update_auto_update_kubo).grid(row=0, column=2, padx=5)
        
        ttk.Checkbutton(cb_frame, text="启用下载链接生成器的网关负载均衡功能", 
                    variable=self.enable_balancer_var).grid(row=1, column=0, columnspan=3, padx=5, pady=(5, 0), sticky="w")
        
        ttk.Checkbutton(cb_frame, text="默认使用简洁模式", 
                    variable=self.default_simple_mode,
                    command=self.update_default_simple_mode).grid(row=1, column=1, columnspan=3, padx=5, pady=(5, 0), sticky="w")

    def create_right_widgets(self):
        """创建右侧面板"""
        outer_frame, scrollable_frame = self._create_smart_scrollable_frame(self.right_frame)
        outer_frame.pack(fill="both", expand=True)
        
        # 顶部：模式切换按钮
        top_frame = ttk.Frame(scrollable_frame)
        top_frame.pack(fill="x", padx=20, pady=(10, 0))
        
        self.mode_toggle_button_advanced = ttk.Button(
            top_frame,
            text="切换到简洁模式",
            command=self.toggle_mode,
            width=20
        )
        self.mode_toggle_button_advanced.pack(side="right")
        
        # GitHub链接
        github_link = ttk.Label(top_frame, text="访问本程序 Github", foreground="blue", cursor="hand2")
        github_link.pack(side="right", padx=(0, 10))
        github_link.bind("<Button-1>", lambda e: webbrowser.open_new("https://github.com/cenglin123/IPFS-ShareAssistant"))
        github_link.bind("<Enter>", lambda e: github_link.config(foreground="purple"))
        github_link.bind("<Leave>", lambda e: github_link.config(foreground="blue"))
        
        # 下载链接生成器
        self._create_links_section(scrollable_frame)
        
        # CID计算器
        self._create_cid_calculator_section(scrollable_frame)
        
        # 底部按钮
        self._create_bottom_buttons(scrollable_frame)

    def _create_links_section(self, parent):
        """创建下载链接生成器区域"""
        frame = ttk.LabelFrame(parent, text="LINKs 下载链接生成器", style='BigTitle.TLabelframe')
        frame.pack(fill="x", padx=20, pady=(10, 10))
        
        ttk.Label(frame, text="根据输入框 1. 和 2. 中的内容生成下载链接").pack(anchor="w", padx=5, pady=(5, 0))
        
        # 链接文本框
        links_text_frame, self.links_text_advanced = self._create_scrolled_text(frame, height=7, width=70)
        links_text_frame.pack(fill="x", padx=5, pady=5)
        
        # 网关选择
        gateway_frame = ttk.Frame(frame)
        gateway_frame.pack(fill="x", padx=5, pady=5)
        ttk.Label(gateway_frame, text="选择网关:").pack(side="left")
        self.gateway_dropdown_advanced = ttk.Combobox(gateway_frame, textvariable=self.gateway_var, state="readonly")
        self.gateway_dropdown_advanced.pack(side="left", fill="x", expand=True)
        
        # 按钮组
        self._create_links_buttons(frame)
        
        # 进度条
        self.progress_bar_advanced = ttk.Progressbar(frame, orient="horizontal", length=400, mode="determinate")
        self.progress_bar_advanced.pack(pady=5)
        self.progress_bar_advanced['value'] = 0

    def _create_links_buttons(self, parent):
        """创建链接生成器按钮组"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(pady=5)
        
        ttk.Button(button_frame, text="生成下载链接", command=self.generate_links).pack(side="left", padx=2)
        ttk.Button(button_frame, text="复制下载链接", command=self.copy_links).pack(side="left", padx=2)
        
        self.speed_test_button_advanced = ttk.Button(button_frame, text="CID网关测速", command=self.start_speed_test) # 保存引用到实例变量，以便后续更新状态
        self.speed_test_button_advanced.pack(side="left", padx=2)
        
        ttk.Button(button_frame, text="重新加载网关", command=self.clear_links).pack(side="left", padx=2)
        ttk.Button(button_frame, text="获取更多网关", command=self.open_gateway).pack(side="left", padx=2)

    def _create_cid_calculator_section(self, parent):
        """创建CID计算器区域"""
        frame = ttk.LabelFrame(parent, text="CIDs 简易计算器", style='BigTitle.TLabelframe')
        frame.pack(fill="x", padx=20, pady=(10, 10))
        
        ttk.Label(frame, text="输入需要计算CID的文件/文件夹路径或v0格式CID").pack(anchor="w")
        
        # 输入框
        cid_input_frame, self.cid_input_text_advanced = self._create_scrolled_text(frame, height=7)
        cid_input_frame.pack(fill="x", pady=2)
        self.cid_input_text_advanced.drop_target_register(DND_FILES)
        self.cid_input_text_advanced.dnd_bind('<<Drop>>', self.drop_on_cid_calculator)
        
        # CID版本选择和Filecoin选项
        options_frame = ttk.Frame(frame)
        options_frame.pack(fill="x", pady=3)
        
        # CID版本选择
        version_frame = ttk.Frame(options_frame)
        version_frame.pack(side="left", padx=(0, 20))
        ttk.Label(version_frame, text="CID版本:").pack(side="left", padx=(0, 5))
        self.cid_version_dropdown_advanced = ttk.Combobox(version_frame, textvariable=self.cid_version_text, 
                                               values=["CID v0", "CID v1", "file -> v0 -> v1", "file -> v1 -> v0"],
                                               state="readonly", width=20)
        self.cid_version_dropdown_advanced.pack(side="left")
        self.cid_version_dropdown_advanced.current(1)  # 默认选择CID v1
        # 绑定下拉菜单选择事件，转换为整数
        self.cid_version_dropdown_advanced.bind("<<ComboboxSelected>>", self.on_cid_version_changed)
        
        # Filecoin选项
        filecoin_frame = ttk.Frame(options_frame)
        filecoin_frame.pack(side="left")
        ttk.Checkbutton(filecoin_frame, text="使用Filecoin参数 (chunker=size-1048576)", 
                       variable=self.use_filecoin, command=self.update_filecoin_config).pack(side="left")
        
        # 按钮组
        self._create_cid_calc_buttons(frame)
        
        # 输出框
        cid_output_frame, self.cid_output_text_advanced = self._create_scrolled_text(frame, height=7)
        cid_output_frame.pack(fill="x", pady=2)
        
        # 状态栏
        self.cid_status_frame_advanced = ttk.Frame(frame, height=30)
        self.cid_status_frame_advanced.pack(fill="x", pady=(5, 0))
        self.cid_status_frame_advanced.pack_propagate(False)
        self.cid_status_label_advanced = ttk.Label(self.cid_status_frame_advanced, text="", anchor='w', justify='left')
        self.cid_status_label_advanced.pack(fill="both", expand=True)

    def _create_cid_calc_buttons(self, parent):
        """创建CID计算器按钮组"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(pady=3)
        
        self.cid_calc_button_gui_advanced = ttk.Button(button_frame, text="计算CID", width=10, command=self.calculate_cid_gui)
        self.cid_calc_button_gui_advanced.pack(side="left", padx=2, pady=5)
        
        self.cid_copy_button = ttk.Button(button_frame, text="复制CID", width=10, command=self.copy_cids)
        self.cid_copy_button.pack(side="left", padx=2, pady=5)
        
        self.fill_input_button = ttk.Button(button_frame, text="填写到主输入框", width=13, command=self.fill_input_box)
        self.fill_input_button.pack(side="left", padx=2, pady=5)

        self.export_json_button = ttk.Button(button_frame, text="导出为JSON文件", width=13, command=self.export_json_file_gui)
        self.export_json_button.pack(side="left", padx=2, pady=5)
        
        self.reset_cid_calc_button_gui = ttk.Button(button_frame, text="重置计算器", width=10, command=self.clear_cid_calculator_gui)
        self.reset_cid_calc_button_gui.pack(side="left", padx=2, pady=5)

    def _create_bottom_buttons(self, parent):
        """创建底部按钮区域"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(pady=5)
        
        # 第一排按钮
        button_row = ttk.Frame(button_frame)
        button_row.pack(fill="x")
        
        # 保存按钮引用到实例变量
        self.crust_pinner_button = ttk.Button(button_row, text="Crust", command=self.open_crust_pinner, width=9)
        self.crust_pinner_button.pack(side="left", padx=2)

        self.aleph_manager_button_advanced  = ttk.Button(button_row, text="Aleph", command=self.open_aleph_manager, width=9)
        self.aleph_manager_button_advanced.pack(side="left", padx=2)

        self.plugin_launcher_button = ttk.Button(button_row, text="插件启动器", command=self.open_plugin_launcher, width=9)
        self.plugin_launcher_button.pack(side="left", padx=2)
        
        self.webui_button_advanced = ttk.Button(button_row, text="WebUI", command=self.open_webui, width=9)
        self.webui_button_advanced.pack(side="left", padx=2)
        
        self.gc_button = ttk.Button(button_row, text="执行GC", command=self.start_gc, width=9)
        self.gc_button.pack(side="left", padx=2)
        
        self.clear_button_bottom = ttk.Button(button_row, text="程序复位", command=self.clear_window, width=9)
        self.clear_button_bottom.pack(side="left", padx=2)

    def _create_status_bar(self):
        """创建状态栏"""
        self.status_frame = tk.Frame(self.main_frame, height=12)
        self.status_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 15))
        self.status_frame.pack_propagate(False)
        
        self.status_label = tk.Label(self.status_frame, text="", font=("Arial", 10), anchor='w', justify='left')
        self.status_label.pack(fill=tk.BOTH, expand=True)
        
        # 工具提示
        self.tooltip = tk.Toplevel(self.root)
        self.tooltip.withdraw()
        self.tooltip.overrideredirect(True)
        self.tooltip_label = tk.Label(self.tooltip, text="", justify='left', background="#ffffe0", relief="solid", borderwidth=1)
        self.tooltip_label.pack()
        
        self.status_label.bind("<Enter>", self.show_tooltip)
        self.status_label.bind("<Leave>", self.hide_tooltip)
        self.status_label.bind("<Motion>", self.update_tooltip_position)

    def _create_simple_mode_ui_in_container(self):
        """3.1 在容器中创建简洁模式界面"""
        # 创建主容器 - 使用grid布局以更好地控制空间分配
        main_container = ttk.Frame(self.simple_container)
        main_container.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 配置grid权重
        main_container.grid_rowconfigure(1, weight=45)  # CID计算器区域占45%
        main_container.grid_rowconfigure(2, weight=55)  # 链接生成器区域占55%
        main_container.grid_columnconfigure(0, weight=1)
        
        # 顶部：模式切换按钮和GitHub链接
        top_frame = ttk.Frame(main_container)
        top_frame.grid(row=0, column=0, sticky="ew", padx=15, pady=(5, 10))
        
        self.mode_toggle_button_simple = ttk.Button(
            top_frame,
            text="切换到高级模式",
            command=self.toggle_mode,
            width=20
        )
        self.mode_toggle_button_simple.pack(side="right")
        
        # GitHub链接
        github_link = ttk.Label(top_frame, text="访问本程序 Github", foreground="blue", cursor="hand2")
        github_link.pack(side="right", padx=(0, 10))
        github_link.bind("<Button-1>", lambda e: webbrowser.open_new("https://github.com/cenglin123/IPFS-ShareAssistant"))
        github_link.bind("<Enter>", lambda e: github_link.config(foreground="purple"))
        github_link.bind("<Leave>", lambda e: github_link.config(foreground="blue"))
        
        # ==================== CID 计算器区域 ====================
        cid_calc_frame = ttk.LabelFrame(
            main_container,
            text="CID 计算器",
            style='BigTitle.TLabelframe'
        )
        cid_calc_frame.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 5))
        
        # 配置CID计算器内部的grid权重
        cid_calc_frame.grid_rowconfigure(1, weight=1)  # 输入框
        cid_calc_frame.grid_rowconfigure(4, weight=1)  # 输出框
        cid_calc_frame.grid_columnconfigure(0, weight=1)
        
        # 文件输入标签
        ttk.Label(
            cid_calc_frame,
            text="1. 拖入文件/文件夹或输入路径："
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        
        # 文件输入框
        input_frame, self.cid_input_text_simple = self._create_scrolled_text(cid_calc_frame, height=4)
        input_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=2)
        self.cid_input_text_simple.drop_target_register(DND_FILES)
        self.cid_input_text_simple.dnd_bind('<<Drop>>', self.drop_on_cid_calculator)
        
        # CID版本选择
        version_frame = ttk.Frame(cid_calc_frame)
        version_frame.grid(row=2, column=0, sticky="w", padx=10, pady=3)
        ttk.Label(version_frame, text="CID版本:").pack(side="left", padx=(0, 5))
        self.cid_version_dropdown_simple = ttk.Combobox(version_frame, textvariable=self.cid_version_text, 
                                                       values=["CID v0", "CID v1", "file -> v0 -> v1", "file -> v1 -> v0"],
                                                       state="readonly", width=20)
        self.cid_version_dropdown_simple.pack(side="left")
        self.cid_version_dropdown_simple.current(1)  # 默认选择CID v1
        # 绑定下拉菜单选择事件，转换为整数
        self.cid_version_dropdown_simple.bind("<<ComboboxSelected>>", self.on_cid_version_changed)
        
        # Filecoin选项
        filecoin_frame = ttk.Frame(cid_calc_frame)
        filecoin_frame.grid(row=2, column=0, sticky="e", padx=10, pady=3)
        ttk.Checkbutton(filecoin_frame, text="使用Filecoin参数 (chunker=size-1048576)", 
                       variable=self.use_filecoin, command=self.update_filecoin_config).pack(side="left")
        
        # 简洁模式按钮组
        calc_button_frame = ttk.Frame(cid_calc_frame)
        calc_button_frame.grid(row=3, column=0, sticky="w", padx=10, pady=3)
        
        self.cid_calc_button_gui_simple = ttk.Button(calc_button_frame, text="计算CID", command=self.calculate_cid_gui, width=12)
        self.cid_calc_button_gui_simple.pack(side="left", padx=2)
        
        self.copy_cid_button = ttk.Button(calc_button_frame, text="复制CID", command=self.copy_cids, width=12)
        self.copy_cid_button.pack(side="left", padx=2)

        self.webui_button_advanced = ttk.Button(calc_button_frame, text="WebUI", command=self.open_webui, width=12)
        self.webui_button_advanced.pack(side="left", padx=2)

        self.aleph_manager_button_simple  = ttk.Button(calc_button_frame, text="Aleph", command=self.open_aleph_manager, width=12)
        self.aleph_manager_button_simple.pack(side="left", padx=2)

        self.reset_cid_calc_button_gui = ttk.Button(calc_button_frame, text="重置计算器", command=self.clear_cid_calculator_gui, width=12)
        self.reset_cid_calc_button_gui.pack(side="left", padx=2)
        
        # CID 输出框（带内嵌标签）
        output_label_frame = ttk.Frame(cid_calc_frame)
        output_label_frame.grid(row=4, column=0, sticky="nsew", padx=10, pady=(8, 2))
        output_label_frame.grid_rowconfigure(1, weight=1)
        output_label_frame.grid_columnconfigure(0, weight=1)
        
        ttk.Label(output_label_frame, text="2. CID计算结果（或直接输入CID开始测速）：").grid(row=0, column=0, sticky="w", pady=(0, 2))
        output_frame, self.cid_output_text_simple = self._create_scrolled_text(output_label_frame, height=4)
        output_frame.grid(row=1, column=0, sticky="nsew")
        
        # CID 状态标签
        self.cid_status_frame_simple = ttk.Frame(cid_calc_frame, height=25)
        self.cid_status_frame_simple.grid(row=5, column=0, sticky="ew", padx=10, pady=(3, 10))
        self.cid_status_frame_simple.grid_propagate(False)
        self.cid_status_label_simple = ttk.Label(self.cid_status_frame_simple, text="", anchor='w')
        self.cid_status_label_simple.pack(fill="both", expand=True)
        
        # ==================== 下载链接生成器区域 ====================
        links_frame = ttk.LabelFrame(
            main_container,
            text="下载链接生成器",
            style='BigTitle.TLabelframe'
        )
        links_frame.grid(row=2, column=0, sticky="nsew", padx=15, pady=(5, 10))
        
        # 配置链接生成器内部的grid权重
        links_frame.grid_rowconfigure(3, weight=1)  # 链接输出框
        links_frame.grid_columnconfigure(0, weight=1)
        
        # 网关选择
        gateway_frame = ttk.Frame(links_frame)
        gateway_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))
        gateway_frame.grid_columnconfigure(1, weight=1)
        
        ttk.Label(gateway_frame, text="选择网关:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.gateway_dropdown_simple = ttk.Combobox(
            gateway_frame,
            textvariable=self.gateway_var,
            state="readonly"
        )
        self.gateway_dropdown_simple.grid(row=0, column=1, sticky="ew")
        
        # 按钮组
        links_button_frame = ttk.Frame(links_frame)
        links_button_frame.grid(row=1, column=0, sticky="w", padx=10, pady=5)

        row1 = ttk.Frame(links_button_frame)
        row1.pack(anchor="w")
        self.generate_links_button = ttk.Button(row1, text="生成下载链接", command=self.generate_links_simple, width=15)
        self.generate_links_button.pack(side="left", padx=2, pady=2)
        self.copy_links_button = ttk.Button(row1, text="复制链接", command=self.copy_links, width=15)
        self.copy_links_button.pack(side="left", padx=2, pady=2)
        self.speed_test_button_simple = ttk.Button(row1, text="网关测速", command=self.start_speed_test_simple, width=15)
        self.speed_test_button_simple.pack(side="left", padx=2, pady=2)
        self.clear_links_button = ttk.Button(row1, text="重新加载网关", command=self.clear_links, width=15)
        self.clear_links_button.pack(side="left", padx=2, pady=2)

        row2 = ttk.Frame(links_button_frame)
        row2.pack(anchor="w")
        self.plugin_launcher_button_simple = ttk.Button(row2, text="插件启动器", command=self.open_plugin_launcher, width=15)
        self.plugin_launcher_button_simple.pack(side="left", padx=2, pady=2)
        
        # 进度条
        self.progress_bar_simple = ttk.Progressbar(
            links_frame,
            orient="horizontal",
            mode="determinate"
        )
        self.progress_bar_simple.grid(row=2, column=0, sticky="ew", padx=5, pady=5)
        self.progress_bar_simple['value'] = 0
        
        # 下载链接输出框（带内嵌标签）
        links_output_label_frame = ttk.Frame(links_frame)
        links_output_label_frame.grid(row=3, column=0, sticky="nsew", padx=10, pady=(5, 5))
        links_output_label_frame.grid_rowconfigure(1, weight=1)
        links_output_label_frame.grid_columnconfigure(0, weight=1)
        
        ttk.Label(links_output_label_frame, text="下载链接：").grid(row=0, column=0, sticky="w", pady=(0, 2))
        links_output_frame, self.links_text_simple = self._create_scrolled_text(links_output_label_frame, height=4)
        links_output_frame.grid(row=1, column=0, sticky="nsew")

    # ==================== 4.UI辅助方法 ====================
    
    def _create_scrolled_text(self, parent, height=5, width=50):
        """创建带滚动条的文本框"""
        frame = ttk.Frame(parent)
        
        v_scrollbar = ttk.Scrollbar(frame, orient='vertical')
        v_scrollbar.pack(side='right', fill='y')
        
        h_scrollbar = ttk.Scrollbar(frame, orient='horizontal')
        h_scrollbar.pack(side='bottom', fill='x')
        
        text = tk.Text(
            frame,
            height=height,
            width=width,
            wrap='none',
            yscrollcommand=v_scrollbar.set,
            xscrollcommand=h_scrollbar.set,
            undo=True,
            autoseparators=True,
        )
        text.pack(side='left', fill='both', expand=True)
        self._attach_context_menu(text)
        
        v_scrollbar.config(command=text.yview)
        h_scrollbar.config(command=text.xview)
        
        return frame, text

    def _create_smart_scrollable_frame(self, parent):
        """创建智能滚动框架（自动显示/隐藏滚动条）"""
        outer_frame = ttk.Frame(parent)
        canvas = tk.Canvas(outer_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer_frame, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        
        # 用于存储绑定标识符
        bindings = {'scrollable': [], 'canvas': [], 'mousewheel_bound': False}
        
        def configure_scroll_region(event=None):
            try:
                outer_frame.after_idle(lambda: canvas.configure(scrollregion=canvas.bbox("all")) if canvas.winfo_exists() else None)
            except:
                pass
        
        def on_canvas_configure(event):
            try:
                if canvas.winfo_exists():
                    canvas.itemconfig(canvas_window, width=event.width)
                    outer_frame.after_idle(check_scrollbar_necessity)
            except:
                pass
        
        def check_scrollbar_necessity():
            try:
                if not canvas.winfo_exists():
                    return
                    
                canvas.update_idletasks()
                bbox = canvas.bbox("all")
                if bbox:
                    content_height = bbox[3] - bbox[1]
                    canvas_height = canvas.winfo_height()
                    buffer = 5
                    
                    if content_height > canvas_height + buffer:
                        if not scrollbar.winfo_manager():
                            scrollbar.pack(side="right", fill="y")
                            canvas.configure(yscrollcommand=scrollbar.set)
                    else:
                        if scrollbar.winfo_manager():
                            scrollbar.pack_forget()
                            canvas.configure(yscrollcommand=None)
            except:
                pass
        
        # 绑定事件
        bindings['scrollable'].append(scrollable_frame.bind("<Configure>", configure_scroll_region))
        bindings['canvas'].append(canvas.bind("<Configure>", on_canvas_configure))
        canvas.pack(side="left", fill="both", expand=True)
        
        def on_mousewheel(event):
            try:
                if canvas.winfo_exists() and scrollbar.winfo_exists() and scrollbar.winfo_manager():
                    canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            except:
                pass
        
        def bind_mousewheel(event):
            try:
                if not bindings['mousewheel_bound']:
                    canvas.bind_all("<MouseWheel>", on_mousewheel)
                    bindings['mousewheel_bound'] = True
            except:
                pass
        
        def unbind_mousewheel(event=None):
            try:
                if bindings['mousewheel_bound']:
                    canvas.unbind_all("<MouseWheel>")
                    bindings['mousewheel_bound'] = False
            except:
                pass
        
        bindings['canvas'].append(canvas.bind("<Enter>", bind_mousewheel))
        bindings['canvas'].append(canvas.bind("<Leave>", unbind_mousewheel))
        
        # 清理函数
        def cleanup():
            try:
                # 解绑鼠标滚轮
                unbind_mousewheel()
                
                # 解绑scrollable_frame的事件
                for bind_id in bindings['scrollable']:
                    try:
                        if bind_id:
                            scrollable_frame.unbind("<Configure>", bind_id)
                    except:
                        pass
                
                # 解绑canvas的事件
                for bind_id in bindings['canvas']:
                    try:
                        if bind_id:
                            canvas.unbind("<Configure>", bind_id)
                            canvas.unbind("<Enter>", bind_id)
                            canvas.unbind("<Leave>", bind_id)
                    except:
                        pass
                
                bindings['scrollable'].clear()
                bindings['canvas'].clear()
            except:
                pass
        
        # 将清理函数附加到外框架
        outer_frame.cleanup = cleanup
        
        # 初始检查
        outer_frame.after(100, check_scrollbar_necessity)

        return outer_frame, scrollable_frame

    def _bind_context_menus(self):
        """为输入部件绑定右键菜单"""
        for cls in ("Text", "Entry", "TEntry", "TCombobox"):
            try:
                self.root.bind_class(cls, "<Button-3>", self._show_context_menu, add="+")
            except Exception:
                continue

    def _attach_context_menu(self, widget):
        """给指定部件挂载右键菜单（用于未走类绑定的部件）"""
        try:
            widget.bind("<Button-3>", self._show_context_menu, add="+")
        except Exception:
            pass

    def _show_context_menu(self, event):
        """显示剪切/复制/粘贴/全选菜单"""
        widget = event.widget
        try:
            widget.focus_force()
        except Exception:
            pass

        menu = getattr(widget, "_context_menu", None)
        if not menu:
            menu = tk.Menu(widget, tearoff=0)
            menu.add_command(label="撤销(Ctrl+Z)", command=lambda w=widget: self._do_undo(w))
            menu.add_command(label="重做(Ctrl+Y)", command=lambda w=widget: self._do_redo(w))
            menu.add_separator()
            menu.add_command(label="剪切(Ctrl+X)", command=lambda w=widget: w.event_generate("<<Cut>>"))
            menu.add_command(label="复制(Ctrl+C)", command=lambda w=widget: w.event_generate("<<Copy>>"))
            menu.add_command(label="粘贴(Ctrl+V)", command=lambda w=widget: w.event_generate("<<Paste>>"))
            menu.add_command(label="删除(Delete)", command=lambda w=widget: self._delete_selection(w))
            menu.add_separator()
            menu.add_command(label="全选(Ctrl+A)", command=lambda w=widget: w.event_generate("<<SelectAll>>"))
            widget._context_menu = menu

        state = ""
        try:
            state = widget.cget("state")
        except Exception:
            pass
        readonly = state in ("disabled", "readonly")

        has_selection = False
        try:
            if isinstance(widget, tk.Text):
                has_selection = bool(widget.tag_ranges("sel"))
            else:
                has_selection = bool(widget.selection_present())
        except Exception:
            has_selection = False

        menu.entryconfig("撤销(Ctrl+Z)", state="normal" if not readonly else "disabled")
        menu.entryconfig("重做(Ctrl+Y)", state="normal" if not readonly else "disabled")
        menu.entryconfig("剪切(Ctrl+X)", state="normal" if (not readonly and has_selection) else "disabled")
        menu.entryconfig("复制(Ctrl+C)", state="normal" if has_selection else "disabled")
        menu.entryconfig("粘贴(Ctrl+V)", state="normal" if not readonly else "disabled")
        menu.entryconfig("删除(Delete)", state="normal" if (not readonly and has_selection) else "disabled")
        menu.entryconfig("全选(Ctrl+A)", state="normal")

        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
        return "break"

    def _delete_selection(self, widget):
        """删除当前选中内容"""
        try:
            if isinstance(widget, tk.Text):
                if widget.tag_ranges("sel"):
                    widget.delete("sel.first", "sel.last")
            else:
                if widget.selection_present():
                    start = widget.index("sel.first")
                    end = widget.index("sel.last")
                    widget.delete(start, end)
        except Exception:
            pass

    def _do_undo(self, widget):
        """撤销"""
        try:
            widget.event_generate("<<Undo>>")
        except Exception:
            pass

    def _do_redo(self, widget):
        """重做"""
        for sequence in ("<<Redo>>", "<Control-y>"):
            try:
                widget.event_generate(sequence)
                break
            except Exception:
                continue
    def toggle_mode(self):
        """切换简洁/高级模式（使用隐藏/显示而不是删除/重建）"""
        self.simple_mode = not self.simple_mode
        
        # 更新窗口标题
        mode_text = " - 简洁模式" if self.simple_mode else ""
        self.root.title(f"IPFS 分享助手 v1.2.2-20251130 by 层林尽染{mode_text}")

        # 先解绑所有全局鼠标滚轮事件
        try:
            self.root.unbind_all("<MouseWheel>")
        except:
            pass
        
        # 使用pack_forget隐藏当前容器，然后显示另一个容器
        if self.simple_mode:
            # 切换到简洁模式
            self.advanced_container.pack_forget()
            self.simple_container.pack(fill=tk.BOTH, expand=True)
        else:
            # 切换到高级模式
            self.simple_container.pack_forget()
            self.advanced_container.pack(fill=tk.BOTH, expand=True)
        
        # 应用正确的窗口几何形状（统一调用，解决尺寸不一致问题）
        self._apply_window_geometry()
        
        # 更新状态
        self.update_status_label(f"已切换到{'简洁' if self.simple_mode else '高级'}模式")
        
        self.logger.info(f"Switched to {'simple' if self.simple_mode else 'advanced'} mode")

    def _safe_destroy_children(self, parent):
        """安全地销毁所有子widget"""
        children = list(parent.winfo_children())
        
        for widget in children:
            try:
                # 递归处理子widget
                if widget.winfo_children():
                    self._safe_destroy_children(widget)
                
                # 解绑所有事件
                try:
                    for sequence in widget.bind():
                        widget.unbind(sequence)
                except:
                    pass
                
                # 如果有cleanup方法，调用它
                if hasattr(widget, 'cleanup'):
                    try:
                        widget.cleanup()
                    except:
                        pass
                
                # 销毁widget
                widget.destroy()
                
            except Exception as e:
                self.logger.warning(f"Error destroying widget: {e}")
                # 即使出错也继续处理下一个
                continue
        
        # 强制垃圾回收
        parent.update_idletasks()

    def _apply_window_geometry(self):
        """
        在所有组件布局完成后，应用正确的窗口尺寸并居中。
        简洁模式采用高级模式的相对尺寸逻辑，但乘以一个缩小系数。
        """
        # 简洁模式缩小系数
        SIMPLE_SCALE_FACTOR = 0.5 
        
        # 强制更新布局
        self.root.update_idletasks()

        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()

        # 计算高级模式的基础尺寸 (作为参照)
        ADVANCED_WIDTH = int(screen_width * 0.62)
        ADVANCED_HEIGHT = int(screen_height * 0.750)
        
        # 根据模式计算尺寸
        if self.simple_mode:
            # 简洁模式：基于高级模式的相对尺寸，乘以缩小系数
            window_width = int(ADVANCED_WIDTH * SIMPLE_SCALE_FACTOR)
            window_height = int(ADVANCED_HEIGHT * SIMPLE_SCALE_FACTOR)
            
            # 确保窗口不会小于设定的最小尺寸
            min_w, min_h = 620, 700
            window_width = max(window_width, min_w)
            window_height = max(window_height, min_h)
            self.logger.info( f"Calculated simple mode size: {window_width}x{window_height}" )

        else:
            # 高级模式：使用设定的相对尺寸（保持不变）
            window_width = ADVANCED_WIDTH
            window_height = ADVANCED_HEIGHT
            self.logger.info( f"Calculated advanced mode size: {window_width}x{window_height}" )
            
        # 居中计算
        center_x = int(screen_width / 2 - window_width / 2)
        center_y = int(screen_height / 2 - window_height / 2)

        # 重新应用尺寸和位置
        self.root.geometry(f'{window_width}x{window_height}+{center_x}+{center_y}')
        
        # 重新设置最小尺寸
        if self.simple_mode:
            self.root.minsize(620, 800)
        else:
            self.root.minsize(1210, 842)
            
        self.logger.info(f"Applied geometry: {window_width}x{window_height} for {'simple' if self.simple_mode else 'advanced'} mode")
        
        # 设置高级模式的分隔条位置（在窗口尺寸确定后）
        if not self.simple_mode:
            self._set_paned_window_position()

    def _set_paned_window_position(self):
        """设置高级模式的分隔条位置"""
        if hasattr(self, 'paned_window'):
            self.root.update_idletasks()  # 确保布局已更新
            paned_width = self.paned_window.winfo_width()
            if paned_width > 1:  # 确保已正确获取宽度
                target_pos = int(paned_width * 0.51)
                self.paned_window.sashpos(0, target_pos)
                self.logger.info(f"Set paned window sash position: {target_pos}/{paned_width}")
            else:
                self.logger.warning(f"Paned window width not ready: {paned_width}")

    def update_default_simple_mode(self):
        """更新默认简洁模式设置"""
        self.save_main_config()
        self.logger.info(f"Default simple mode: {self.default_simple_mode.get()}")

    # ==================== 5.配置管理 ====================
    
    def save_main_config(self):
        """保存主配置"""
        config = {
            'repo_path': self.repo_entry_advanced.get() if hasattr(self, 'repo_entry_advanced') and self.repo_entry_advanced else self.repo_path,
            'pin_after_import': self.pin_var.get(),
            'minimize_to_tray': self.minimize_to_tray.get(),
            'auto_update_kubo': self.auto_update_kubo.get(),
            'enable_balancer_var': self.enable_balancer_var.get(),
            'default_simple_mode': self.default_simple_mode.get(),
            'proxy': self.proxy,
            'api': self.actual_api_address,
        }
        config_path = os.path.join(self.app_path, 'config.json')
        save_config_file(config_path, config, self.logger)

    def update_minimize_to_tray(self):
        """更新最小化到托盘设置"""
        self.save_main_config()
        self.logger.info(f"Minimize to tray: {self.minimize_to_tray.get()}")

    def update_auto_update_kubo(self):
        """更新自动更新Kubo设置"""
        self.save_main_config()
        self.logger.info(f"Auto-update Kubo: {self.auto_update_kubo.get()}")

    def set_proxy_from_ui(self):
        """从UI设置代理"""
        proxy = self.proxy_entry_advanced.get().strip()
        self.proxy = proxy if proxy else None
        self.save_main_config()
        messagebox.showinfo("成功", f"代理已设置为: {proxy}" if proxy else "代理已清除")

    def set_api_from_ui(self):
        """从UI设置API"""
        api = self.api_entry_advanced.get().strip()
        if api:
            self.actual_api_address = api
            self.save_main_config()
            messagebox.showinfo("成功", f"IPFS API 已设置为: {api}")

    def browse_repo(self):
        """浏览并选择仓库路径"""
        repo_path = filedialog.askdirectory(initialdir=self.repo_path)
        if repo_path:
            self.repo_entry_advanced.delete(0, tk.END)
            self.repo_entry_advanced.insert(0, repo_path)
            self.repo_path = repo_path
            
            # 自动更新API地址
            self.actual_api_address = self._get_api_address_from_repo()
            self.api_entry_advanced.delete(0, tk.END)
            self.api_entry_advanced.insert(0, self.actual_api_address)
            
            self.save_main_config()
            self.logger.info(f"Repository path updated: {repo_path}")

    def _get_api_address_from_repo(self):
        """从仓库文件读取API地址"""
        api_file = os.path.join(self.repo_path, 'api')
        if os.path.exists(api_file):
            with open(api_file, 'r') as f:
                api_address = f.read().strip()
            parts = api_address.split('/')
            if len(parts) >= 5:
                return f"http://{parts[2]}:{parts[4]}"
        return self.actual_api_address

    # ==================== 6.网关管理 ====================
    
    def load_gateways(self):
        """加载网关列表"""
        gateway_files = [
            ("ipfs_gateway.txt", None),
            ("ipfs_gateway_side.txt", 50)
        ]
        
        all_gateways = []
        for filename, limit in gateway_files:
            filepath = os.path.join(self.app_path, "assets", filename)
            try:
                with open(filepath, 'r') as f:
                    gateways = [line.strip() for line in f if line.strip()]
                    if limit:
                        gateways = random.sample(gateways, min(limit, len(gateways)))
                    all_gateways.extend(gateways)
            except FileNotFoundError:
                self.logger.warning(f"Gateway file not found: {filepath}")
        
        self.gateways = list(dict.fromkeys(all_gateways))
        
        # 更新两个模式的网关下拉框
        if hasattr(self, 'gateway_dropdown_simple'):
            self.gateway_dropdown_simple['values'] = self.gateways
            if self.gateways:
                self.gateway_dropdown_simple.set(self.gateways[0])
        
        if hasattr(self, 'gateway_dropdown_advanced'):
            self.gateway_dropdown_advanced['values'] = self.gateways
            if self.gateways:
                self.gateway_dropdown_advanced.set(self.gateways[0])
        
        self.logger.info(f"Loaded {len(self.gateways)} unique gateways")

    def start_speed_test(self):
        """启动网关测速（高级模式）"""
        identifiers = self._get_text_lines(self.cid_text_advanced)
        if not identifiers:
            messagebox.showwarning("警告", "请先在主输入框中输入至少一个有效的CID或IPNS地址")
            return
        
        # 验证CID
        valid_ids = [id for id in identifiers if self._is_valid_cid(id)]
        if not valid_ids:
            messagebox.showwarning("警告", "没有有效的CID或IPNS地址")
            return
        
        if len(valid_ids) != len(identifiers):
            invalid = set(identifiers) - set(valid_ids)
            messagebox.showwarning("警告", f"以下 CID 无效，将被跳过:\n{', '.join(invalid)}")
        
        # 多个CID时询问测速范围
        if len(valid_ids) > 1:
            self._show_speed_test_dialog(valid_ids)
        else:
            self._run_speed_test_thread(valid_ids)

    def _show_speed_test_dialog(self, identifiers):
        """显示测速选项对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title("测速选项")
        dialog.geometry("300x150")
        dialog.transient(self.root)
        dialog.grab_set()
        
        if os.path.exists(self.icon_path):
            dialog.iconbitmap(self.icon_path)
        
        # 居中
        dialog.update_idletasks()
        x = (dialog.winfo_screenwidth() - dialog.winfo_width()) // 2
        y = (dialog.winfo_screenheight() - dialog.winfo_height()) // 2
        dialog.geometry(f'+{x}+{y}')
        
        ttk.Label(dialog, text=f"检测到 {len(identifiers)} 个 CID，请选择测速范围：", 
                 wraplength=280).pack(pady=10)
        
        def handle_choice(choice):
            dialog.destroy()
            ids = identifiers if choice == "all" else [identifiers[0]]
            self._run_speed_test_thread(ids)
        
        button_frame = ttk.Frame(dialog)
        button_frame.pack(side=tk.BOTTOM, pady=20)
        
        ttk.Button(button_frame, text="测试全部", command=lambda: handle_choice("all")).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="仅测试第一个", command=lambda: handle_choice("first")).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="取消", command=dialog.destroy).pack(side=tk.LEFT, padx=5)

    def _run_speed_test_thread(self, identifiers):
        """在新线程中运行测速"""
        self._update_button_state('speed_test_button', tk.DISABLED)
        self.update_status_label("正在进行网关测速...")
        self.progress_bar["value"] = 0
        threading.Thread(target=self.run_speed_test, args=(identifiers,), daemon=True).start()

    def run_speed_test(self, cids):
        """执行网关测速"""
        all_results = {}
        total_tests = len(cids) * len(self.gateways)
        completed = 0
        
        for cid in cids:
            results = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
                future_to_gateway = {
                    executor.submit(self._check_gateway, gateway, cid): gateway
                    for gateway in self.gateways
                }
                
                for future in concurrent.futures.as_completed(future_to_gateway):
                    gateway = future_to_gateway[future]
                    try:
                        result = future.result()
                        results.append(result)
                    except Exception as exc:
                        self.logger.error(f"Gateway {gateway} error: {exc}")
                        results.append((gateway, "Error", None, None, 0))
                    
                    completed += 1
                    self.root.after(0, self._update_progress, completed, total_tests)
            
            # 排序结果
            sorted_results = sorted(results, key=lambda x: (
                x[1] not in ["200", "206"],
                x[4] == 0,
                -x[4]
            ))
            all_results[cid] = sorted_results
            
            # 记录最佳网关
            for gw, status, _, _, speed in sorted_results:
                if status in ["200", "206"] and speed > 0:
                    self.cid_best_gateways[cid] = gw
                    break
        
        # 更新UI
        self.root.after(0, self._update_gateway_dropdown_after_test, all_results)
        self.root.after(0, lambda: self._update_button_state('speed_test_button', tk.NORMAL))
        self.root.after(0, self.update_status_label, "网关测速完成")
        self.root.after(0, self._show_detailed_results, all_results)

    def _check_gateway(self, gateway, identifier):
        """检查单个网关速度"""
        is_ipns = identifier.startswith('k51')
        path_prefix = 'ipns' if is_ipns else 'ipfs'
        url = urljoin(gateway, f"{path_prefix}/{identifier}")
        
        # 代理设置
        proxy_required = gateway in ["https://ipfs.io", "https://dweb.link", "https://w3s.link"]
        cmd = [
            'curl', '-L', '-w', '%{http_code} %{time_starttransfer} %{speed_download} %{size_download}\n',
            '-o', 'NUL', '-s', '--max-time', '10',
            '--range', '0-1048576',
            url
        ]
        if proxy_required and self.proxy:
            cmd.extend(['-x', self.proxy])
        
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
                **self._get_subprocess_args()
            )
            
            parts = result.stdout.strip().split()
            if len(parts) == 4:
                status, time_s, _, size = parts
                time_s = float(time_s)
                size = int(size)
                
                if size == 0:
                    return (gateway, status, None, url, 0)
                
                speed = size / time_s if time_s > 0 else 0
                return (gateway, status, int(time_s * 1000), url, speed)
            
            return (gateway, "Invalid Output", None, url, 0)
        except subprocess.TimeoutExpired:
            return (gateway, "Timeout", None, url, 0)
        except Exception as e:
            return (gateway, f"Error: {str(e)}", None, url, 0)

    def _update_progress(self, completed, total):
        """更新进度条"""
        progress = (completed / total) * 100
        self.progress_bar["value"] = progress
        self.update_status_label(f"正在进行网关测速... ({completed}/{total})")

    def _update_gateway_dropdown_after_test(self, all_results):
        """测速后更新网关下拉框"""
        cids = list(all_results.keys())
        
        if len(cids) == 1:
            # 单个CID：显示所有网关及速度
            gateway_speeds = {}
            for gw, status, time_ms, _, speed in all_results[cids[0]]:
                gateway_speeds[gw] = (status, speed, time_ms)
            
            new_values = []
            for gw, (status, speed, time_ms) in gateway_speeds.items():
                # 检查 None 情况
                if speed == 0 or time_ms is None:
                    new_values.append(f"{gw} - 暂时无法对此网关测速，请生成下载链接后尝试")
                else:
                    new_values.append(f"{gw} - {self._format_speed(speed)}/s ({time_ms:.0f}ms)")
        else:
            # 多个CID：计算平均速度并标记最佳网关
            gateway_data = {}
            for results in all_results.values():
                for gw, status, time_ms, _, speed in results:
                    if gw not in gateway_data:
                        gateway_data[gw] = {'speeds': [], 'times': [], 'best_for': []}
                    # 确保只添加有效数据
                    if status in ["200", "206"] and speed and time_ms is not None:
                        gateway_data[gw]['speeds'].append(speed)
                        gateway_data[gw]['times'].append(time_ms)
            
            # 标记最佳网关
            for cid, best_gw in self.cid_best_gateways.items():
                if best_gw in gateway_data:
                    gateway_data[best_gw]['best_for'].append(cid)
            
            new_values = []
            for gw, data in gateway_data.items():
                if data['speeds']:
                    avg_speed = sum(data['speeds']) / len(data['speeds'])
                    avg_time = sum(data['times']) / len(data['times'])
                    label = f"{gw} - {self._format_speed(avg_speed)}/s ({avg_time:.0f}ms)"
                    
                    if data['best_for']:
                        cid_ids = ["..." + c[-4:] for c in data['best_for']]
                        label += f" [最快网关: {', '.join(cid_ids)}]"
                    
                    new_values.append(label)
                else:
                    new_values.append(f"{gw} - 暂时无法对此网关测速")
            
            new_values.sort(key=lambda x: "[最快网关" in x, reverse=True)
        
        # 同步两个模式的下拉框选项
        for dropdown in (getattr(self, "gateway_dropdown_simple", None), getattr(self, "gateway_dropdown_advanced", None)):
            if dropdown:
                dropdown['values'] = new_values

        if new_values:
            self.gateway_var.set(new_values[0])

        self._auto_generate_links_after_speed_test()

    def _auto_generate_links_after_speed_test(self):
        """测速完成后自动生成一次下载链接，确保最新网关即时生效"""
        def generate():
            try:
                if self.simple_mode:
                    self.generate_links_simple()
                else:
                    self.generate_links()
            except Exception as exc:
                self.logger.error(f"Auto generate links after speed test failed: {exc}")
        self.root.after(0, generate)

    def _show_detailed_results(self, all_results):
        """显示详细测速结果"""
        window = tk.Toplevel(self.root)
        window.title("详细测速结果")
        if os.path.exists(self.icon_path):
            window.iconbitmap(self.icon_path)
        
        # 设置窗口大小和位置
        window_width, window_height = 800, 600
        x = (window.winfo_screenwidth() - window_width) // 2
        y = (window.winfo_screenheight() - window_height) // 2
        window.geometry(f'{window_width}x{window_height}+{x}+{y}')
        
        # 文本区域
        text_frame = tk.Frame(window)
        text_frame.pack(expand=True, fill=tk.BOTH)
        
        text_widget = tk.Text(text_frame, wrap=tk.WORD, width=100, height=28)
        text_widget.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        
        scrollbar = tk.Scrollbar(text_frame, orient=tk.VERTICAL, command=text_widget.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text_widget.config(yscrollcommand=scrollbar.set)
        
        # 填充结果
        copy_info = {}
        for cid, results in all_results.items():
            text_widget.insert(tk.END, f"CID: {cid}\n")
            copy_info[cid] = [cid]
            
            for gw, status, time_ms, final_url, speed in results:
                if status in ["200", "206"]:
                    # 检查 time_ms 是否为 None
                    if time_ms is not None:
                        text_widget.insert(tk.END, f"  {gw}: {self._format_speed(speed)}/s (响应时间: {time_ms:.0f}ms, 状态: {status})\n")
                    else:
                        text_widget.insert(tk.END, f"  {gw}: {self._format_speed(speed)}/s (响应时间: N/A, 状态: {status})\n")
                    
                    if gw not in final_url:
                        text_widget.insert(tk.END, f"    重定向到: {final_url}\n")
                    copy_info[cid].append(gw)
                else:
                    text_widget.insert(tk.END, f"  {gw}: {status}\n")
            
            text_widget.insert(tk.END, "\n")
        
        text_widget.config(state=tk.DISABLED)
        
        # 复制按钮
        def copy_results():
            text = ""
            for cid, gateways in copy_info.items():
                is_ipns = cid.startswith('k51')
                prefix = 'ipns' if is_ipns else 'ipfs'
                text += f"CID: {cid}\n"
                for gw in gateways[1:]:
                    text += f"{gw.rstrip('/')}/{prefix}/{cid}\n"
                text += "\n"
            
            self.root.clipboard_clear()
            self.root.clipboard_append(text.strip())
            messagebox.showinfo("复制成功", "CID 和可用网关已复制到剪贴板")
        
        tk.Button(window, text="复制 CID 和所有可用网关", command=copy_results).pack(pady=10)

    def start_speed_test_simple(self):
        """简洁模式：从CID输出框获取CID进行测速"""
        cids = self._get_text_lines(self.cid_output_text)
        if not cids:
            messagebox.showwarning("警告", "请先计算CID")
            return
        
        # 验证CID
        valid_cids = [cid for cid in cids if self._is_valid_cid(cid)]
        if not valid_cids:
            messagebox.showwarning("警告", "没有有效的CID")
            return
        
        if len(valid_cids) != len(cids):
            invalid = set(cids) - set(valid_cids)
            messagebox.showwarning("警告", f"以下 CID 无效，将被跳过:\n{', '.join(invalid)}")
        
        # 多个CID时询问测速范围
        if len(valid_cids) > 1:
            self._show_speed_test_dialog(valid_cids)
        else:
            self._run_speed_test_thread(valid_cids)

    # ==================== 7.链接生成 ====================
    
    def generate_links(self):
        """生成下载链接"""
        identifiers = self._get_text_lines(self.cid_text_advanced)
        if not identifiers:
            messagebox.showwarning("警告", "请输入至少一个CID或IPNS地址")
            return
        
        names = self._get_text_lines(self.name_text_advanced)
        names.extend([""] * (len(identifiers) - len(names)))
        
        current_gateway = self.gateway_var.get().split(' - ')[0]
        available_gateways = [gw.split(' - ')[0] for gw in self.gateway_dropdown['values']]
        
        if not available_gateways:
            messagebox.showwarning("警告", "没有可用的IPFS网关")
            return
        
        # 确定网关使用策略
        if len(identifiers) == 1 or not self.enable_balancer_var.get():
            gateways = [current_gateway]
        elif hasattr(self, 'cid_best_gateways') and len(self.cid_best_gateways) > 1:
            gateways = [self.cid_best_gateways.get(cid, current_gateway) for cid in identifiers]
        else:
            try:
                idx = available_gateways.index(current_gateway)
                gateways = available_gateways[idx:] + available_gateways[:idx]
            except ValueError:
                gateways = available_gateways
        
        # 生成链接
        links = []
        gateway_dist = {}
        gateway_idx = 0
        
        for identifier, name in zip(identifiers, names):
            if os.path.exists(identifier):
                continue
            
            # 选择网关
            if len(identifiers) == 1 or not self.enable_balancer_var.get():
                gw = gateways[0]
            elif hasattr(self, 'cid_best_gateways') and len(self.cid_best_gateways) > 1:
                gw = self.cid_best_gateways.get(identifier, gateways[gateway_idx % len(gateways)])
            else:
                gw = gateways[gateway_idx % len(gateways)]
                gateway_idx += 1
            
            gateway_dist[gw] = gateway_dist.get(gw, 0) + 1
            
            # 生成URL
            is_ipns = identifier.startswith('k51')
            prefix = 'ipns' if is_ipns else 'ipfs'
            
            if is_ipns or not name:
                link = f"{gw}/{prefix}/{identifier}"
            else:
                link = f"{gw}/{prefix}/{identifier}?filename={quote(name)}"
            
            links.append(link)
        
        # 更新UI
        self.links_text.delete("1.0", tk.END)
        self.links_text.insert(tk.END, "\n".join(links))
        
        # 状态消息
        ipns_count = sum(1 for l in links if '/ipns/' in l)
        status = f"已生成 {len(links)} 个下载链接"
        if ipns_count:
            status += f" (IPFS: {len(links) - ipns_count}, IPNS: {ipns_count})"
        if len(gateway_dist) > 1 and self.enable_balancer_var.get():
            status += "\n网关分配情况："
            for gw, count in gateway_dist.items():
                status += f"\n{gw}: {count}个链接"
        
        self.update_status_label(status)

    def generate_links_simple(self):
        """简洁模式：从CID计算器直接生成下载链接"""
        # 获取计算出的CID
        cids = self._get_text_lines(self.cid_output_text)
        if not cids:
            messagebox.showwarning("警告", "请先计算CID")
            return
        
        # 获取对应的文件名
        inputs = self._get_text_lines(self.cid_input_text)
        names = []
        for input_item in inputs:
            if os.path.exists(input_item):
                names.append(os.path.basename(input_item))
            else:
                names.append(input_item)
        
        # 补齐名称列表
        while len(names) < len(cids):
            names.append(cids[len(names)])

        # 清洗文件名：去掉父文件夹路径，只保留文件名+后缀（但保留与 CID 相同的条目）
        cleaned_names = []
        for cid, name in zip(cids, names):
            if not name or name == cid:
                cleaned_names.append(name)
            else:
                cleaned_names.append(os.path.basename(name))
        names = cleaned_names
        
        # 获取当前网关
        current_gateway = self.gateway_var.get().split(' - ')[0]
        if not current_gateway:
            messagebox.showwarning("警告", "请选择网关")
            return
        
        # 生成链接
        links = []
        for cid, name in zip(cids, names):
            is_ipns = cid.startswith('k51')
            prefix = 'ipns' if is_ipns else 'ipfs'
            
            if is_ipns or not name or name == cid:
                link = f"{current_gateway}/{prefix}/{cid}"
            else:
                link = f"{current_gateway}/{prefix}/{cid}?filename={quote(name)}"
            
            links.append(link)
        
        # 显示链接
        self.links_text.delete("1.0", tk.END)
        self.links_text.insert(tk.END, "\n".join(links))
        
        # 更新状态
        ipns_count = sum(1 for l in links if '/ipns/' in l)
        status = f"已生成 {len(links)} 个下载链接"
        if ipns_count:
            status += f" (IPFS: {len(links) - ipns_count}, IPNS: {ipns_count})"
        
        self.update_status_label(status)
        self.logger.info(f"Generated {len(links)} links in simple mode")


    def copy_links(self):
        """复制链接"""
        links = self.links_text.get("1.0", tk.END).strip()
        if links:
            self.root.clipboard_clear()
            self.root.clipboard_append(links)
            self.update_status_label("下载链接已复制到剪贴板")
        else:
            messagebox.showwarning("警告", "没有可复制的下载链接")

    def clear_links(self):
        """清除链接并重新加载网关"""
        self.links_text.delete("1.0", tk.END)
        if hasattr(self, 'cid_best_gateways'):
            self.cid_best_gateways.clear()
        self.load_gateways()
        self.progress_bar['value'] = 0
        self.update_status_label("已清除链接并重新加载网关")

    # ==================== 8.CID计算 ====================
    
    def calculate_cid_gui(self):
        """计算CID（支持文件和CID转换）"""
        items = self._get_text_lines(self.cid_input_text)
        if not items:
            messagebox.showwarning("警告", "请输入文件路径或CID")
            return
        
        self._update_button_state('cid_calc_button_gui', tk.DISABLED)
        self.cid_output_text.delete("1.0", tk.END)
        
        threading.Thread(target=self._calculate_cids_thread, args=(items,), daemon=True).start()

    def _calculate_cids_thread(self, items):
        """CID计算线程"""
        total = len(items)
        subprocess_args = self._get_subprocess_args()
        
        for idx, item in enumerate(items, 1):
            if not item.strip():
                continue
            
            try:
                self._update_cid_status(f"正在处理 ({idx}/{total}): {item}")
                
                # 判断输入类型
                if os.path.exists(item):
                    cid = self._calc_file_cid(item, subprocess_args)
                elif self._is_cid_v0(item):
                    cid = item
                elif self._is_cid_v1(item):
                    cid = self._convert_cid(item, 0, subprocess_args)
                else:
                    self.cid_output_text.insert(tk.END, f"无效输入: {item}\n")
                    continue
                
                # 转换 CID 版本
                if cid:
                    # value=2: v0 -> v1
                    if self.cid_version.get() == 2 and self._is_cid_v0(cid):
                        cid = self._convert_cid(cid, 1, subprocess_args)
                    # value=3: v1 -> v0
                    elif self.cid_version.get() == 3 and self._is_cid_v1(cid):
                        try:
                            cid = self._convert_cid(cid, 0, subprocess_args)
                        except ValueError as e:
                            # 内联 CID 无法转换为 v0
                            if "inline" in str(e).lower() or "cannot" in str(e).lower():
                                error_msg = f"{item}: 此文件为内联CID，无法转换为v0格式"
                                self.cid_output_text.insert(tk.END, f"{error_msg}\n")
                                self.logger.warning(error_msg)
                                continue
                            else:
                                raise
                    # value=1: 仅转 v1
                    elif self.cid_version.get() == 1 and self._is_cid_v0(cid):
                        cid = self._convert_cid(cid, 1, subprocess_args)
                
                if cid:
                    self.cid_output_text.insert(tk.END, f"{cid}\n")
                
            except Exception as e:
                self.cid_output_text.insert(tk.END, f"错误: {str(e)}\n")
                self.logger.error(f"CID calculation error: {e}")
        
        self.root.after(0, lambda: self._update_button_state('cid_calc_button_gui', tk.NORMAL))
        self.root.after(0, lambda: self.cid_output_text.see(tk.END))
        self.root.after(0, lambda: self._update_cid_status("CID 计算完成"))

    def _calc_file_cid(self, filepath, subprocess_args):
        """计算文件CID"""
        # 根据选项决定初始计算的版本
        # value=2: file -> v0 -> v1，先算 v0
        # value=3: file -> v1 -> v0，先算 v1
        if self.cid_version.get() == 2:
            cid_version = 0
        elif self.cid_version.get() == 3:
            cid_version = 1
        else:
            cid_version = self.cid_version.get()
        
        cmd = [
            self.kubo.kubo_path, "add", "--only-hash", "-Q",
            f"--cid-version={cid_version}",
            "--repo-dir", self.repo_path
        ]
        
        # 根据Filecoin选项设置chunker参数
        if self.use_filecoin.get():
            chunker_size = "size-1048576"  # 1MB (Filecoin专用)
        else:
            chunker_size = "size-262144"   # 256KB (默认)
        
        cmd.extend(["--chunker", chunker_size])
        
        # 记录参数配置日志
        self.logger.info(f"CID计算参数: filecoin={self.use_filecoin.get()}, chunker={chunker_size}, cid_version={cid_version}")
        
        if os.path.isdir(filepath):
            cmd.append("-r")
        cmd.append(filepath)
        
        result = subprocess.run(cmd, capture_output=True, text=True, **subprocess_args)
        if result.returncode == 0:
            return result.stdout.strip()
        raise ValueError(f"计算失败: {result.stderr}")

    def _convert_cid(self, cid, target_version, subprocess_args):
        """转换CID版本"""
        cmd = [self.kubo.kubo_path, "cid", "format", "-v", str(target_version)]
        if target_version == 1:
            cmd.extend(["-b", "base32"])
        cmd.append(cid)
        
        result = subprocess.run(cmd, capture_output=True, text=True, **subprocess_args)
        if result.returncode == 0:
            return result.stdout.strip()
        raise ValueError(f"转换失败: {result.stderr}")

    def copy_cids(self):
        """复制CID"""
        cids = self.cid_output_text.get("1.0", tk.END).strip()
        if cids:
            self.root.clipboard_clear()
            self.root.clipboard_append(cids)
            self._update_cid_status("CID已复制到剪贴板")
        else:
            messagebox.showwarning("警告", "没有可复制的CID")

    def fill_input_box(self):
        """将计算的CID填入主输入框"""
        cids = self._get_text_lines(self.cid_output_text)
        inputs = self._get_text_lines(self.cid_input_text)
        
        if not cids:
            self._update_cid_status("没有可填写的CID")
            return
        
        self.cid_text_advanced.delete("1.0", tk.END)
        self.name_text_advanced.delete("1.0", tk.END)
        
        for cid, input_item in zip(cids, inputs):
            self.cid_text_advanced.insert(tk.END, cid + '\n')
            name = os.path.basename(input_item) if os.path.exists(input_item) else input_item
            self.name_text_advanced.insert(tk.END, name + '\n')
        
        self._update_cid_status("已填写到主输入框")

    def clear_cid_calculator_gui(self):
        """清除CID计算器"""
        self.cid_input_text.delete("1.0", tk.END)
        self.cid_output_text.delete("1.0", tk.END)
        self.cid_version.set(1)
        self.cid_status_label.config(text="")

    # ==================== 9.导入功能 ====================
    
    def start_execute(self):
        """开始导入"""
        self.root.focus_set()
        self.execute_button_advanced.config(state=tk.DISABLED)
        self.stop_button_advanced.config(state=tk.NORMAL)
        self.importing = True
        self.stop_import = False
        threading.Thread(target=self.execute, daemon=True).start()

    def stop_execute(self):
        """停止导入"""
        self.stop_import = True
        self.stop_button_advanced.config(state=tk.DISABLED)
        self.update_status_label("正在停止导入...")

    def execute(self):
        """执行导入流程"""
        try:
            # 获取输入
            items = self._get_text_lines(self.cid_text_advanced)
            if not items:
                messagebox.showwarning("警告", "请输入至少一个 CID 或文件路径")
                return
            
            names = self._get_text_lines(self.name_text_advanced)
            names.extend([""] * (len(items) - len(names)))
            
            folder = self.path_entry_advanced.get().strip().strip('/')
            
            # 准备IPFS命令参数
            subprocess_args = self._get_subprocess_args()
            parsed_api = urllib.parse.urlparse(self.actual_api_address)
            base_cmd = [
                self.kubo.kubo_path,
                "--repo-dir", self.repo_path,
                "--api", f"/ip4/{parsed_api.hostname}/tcp/{parsed_api.port}"
            ]
            
            # 导入统计
            stats = {'success': 0, 'skipped': 0, 'pinned': 0, 'failed': []}
            total = len(items)
            
            for idx, (item, name) in enumerate(zip(items, names), 1):
                if self.stop_import:
                    break
                
                try:
                    name = name or (os.path.basename(item) if os.path.exists(item) else item)
                    dest_path = f"{folder}/{name}".lstrip('/')
                    
                    # 添加到IPFS
                    if os.path.exists(item):
                        cid = self._add_to_ipfs(item, idx, total, base_cmd, subprocess_args)
                    else:
                        cid = item
                    
                    # 复制到MFS
                    status = self._copy_to_mfs(cid, dest_path, idx, total, base_cmd, subprocess_args, 
                                              is_cid=not os.path.exists(item))
                    
                    if status == "exists":
                        stats['skipped'] += 1
                    elif status == "success":
                        stats['success'] += 1
                        
                        # 固定
                        if self.pin_var.get():
                            if self._pin_cid(cid, idx, total, base_cmd, subprocess_args):
                                stats['pinned'] += 1
                    
                except TimeoutError as e:
                    stats['failed'].append({'cid': item, 'name': name, 'error': str(e)})
                except Exception as e:
                    if os.path.exists(item):
                        raise  # 本地文件错误要中断
                    stats['failed'].append({'cid': item, 'name': name, 'error': str(e)})
                
                # 更新进度
                self.progress_bar['value'] = (idx / total) * 100
                self.root.update_idletasks()
            
            # 显示结果
            self._show_import_results(stats)
            
        except Exception as e:
            self.logger.error(f"Import error: {e}")
            messagebox.showerror("错误", f"导入失败: {e}")
        finally:
            self.execute_button_advanced.config(state=tk.NORMAL)
            self.stop_button_advanced.config(state=tk.DISABLED)
            self.importing = False
            self.progress_bar['value'] = 0

    def _add_to_ipfs(self, filepath, idx, total, base_cmd, subprocess_args):
        """添加文件到IPFS"""
        cmd = base_cmd + [
            "add", "-Q", f"--cid-version={self.cid_version.get()}", "--progress"
        ]
        if os.path.isdir(filepath):
            cmd.append("-r")
        cmd.append(filepath)
        
        self.update_status_label(f"正在添加到IPFS ({idx}/{total}): {filepath}")
        
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, **subprocess_args
        )
        
        cid = None
        for line in process.stdout:
            if self.stop_import:
                process.terminate()
                break
            
            # 更新进度
            percentage = self._parse_progress_line(line)
            if percentage is not None:
                self.progress_bar['value'] = percentage
                self.root.update_idletasks()
            
            # 提取CID
            for prefix in ['baf', 'Qm']:
                if prefix in line:
                    idx_start = line.find(prefix)
                    if idx_start != -1:
                        cid = line[idx_start:].split()[0]
        
        process.wait()
        if process.returncode != 0 or not cid:
            raise ValueError(f"添加失败: {filepath}")
        
        return cid

    def _copy_to_mfs(self, cid, dest_path, idx, total, base_cmd, subprocess_args, is_cid=False):
        """复制到MFS"""
        dir_path = os.path.dirname(dest_path)
        
        # 创建目录
        if dir_path:
            mkdir_cmd = base_cmd + ["files", "mkdir", "-p", f"/{dir_path}"]
            if self.cid_version.get() == 1:
                mkdir_cmd.append("--cid-version=1")
            
            subprocess.run(mkdir_cmd, check=True, **subprocess_args)
        
        if self.stop_import:
            return "stopped"
        
        # 复制文件
        cp_cmd = base_cmd + ["files", "cp", f"/ipfs/{cid}", f"/{dest_path}"]
        self.update_status_label(f"复制到MFS ({idx}/{total}): {dest_path}")
        
        result = subprocess.run(
            cp_cmd, capture_output=True, text=True,
            timeout=30 if is_cid else None, **subprocess_args
        )
        
        if "file already exists" in result.stderr:
            return "exists"
        elif result.returncode == 0:
            return "success"
        else:
            raise subprocess.CalledProcessError(result.returncode, cp_cmd, result.stdout, result.stderr)

    def _pin_cid(self, cid, idx, total, base_cmd, subprocess_args):
        """固定CID"""
        if self.stop_import:
            return False
        
        self.update_status_label(f"正在固定 ({idx}/{total}): {cid}")
        pin_cmd = base_cmd + ["pin", "add", cid]
        
        result = subprocess.run(
            pin_cmd, capture_output=True, text=True,
            timeout=60, **subprocess_args
        )
        
        return result.returncode == 0

    def _show_import_results(self, stats):
        """显示导入结果"""
        message = "导入已停止！\n" if self.stop_import else "导入完成！\n"
        message += f"成功导入：{stats['success']} 个文件\n"
        message += f"跳过（已存在）：{stats['skipped']} 个文件\n"
        
        if stats['failed']:
            message += f"导入失败：{len(stats['failed'])} 个文件\n"
        
        if self.pin_var.get():
            message += f"成功固定：{stats['pinned']} 个对象\n"
        
        if stats['failed']:
            message += "\n失败详情："
            for item in stats['failed']:
                name_part = f" (文件名: {item['name']})" if item['name'] != item['cid'] else ""
                message += f"\nCID: {item['cid']}{name_part} - {item['error']}"
        
        self._show_wide_dialog("执行结果", message, stats['failed'])

    def _show_wide_dialog(self, title, message, failed_items):
        """显示宽对话框"""
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        
        width, height = 800, 400
        x = (dialog.winfo_screenwidth() - width) // 2
        y = (dialog.winfo_screenheight() - height) // 2
        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog.configure(padx=20, pady=20)
        
        if os.path.exists(self.icon_path):
            dialog.iconbitmap(self.icon_path)
        
        # 文本区
        text = tk.Text(dialog, wrap=tk.NONE, width=120, height=20)
        text.grid(row=0, column=0, sticky='nsew')
        
        scrollbar_y = tk.Scrollbar(dialog, orient=tk.VERTICAL, command=text.yview)
        scrollbar_y.grid(row=0, column=1, sticky='ns')
        scrollbar_x = tk.Scrollbar(dialog, orient=tk.HORIZONTAL, command=text.xview)
        scrollbar_x.grid(row=1, column=0, sticky='ew')
        
        text.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        text.insert('1.0', message)
        text.configure(state='disabled')
        
        dialog.grid_rowconfigure(0, weight=1)
        dialog.grid_columnconfigure(0, weight=1)
        
        # 按钮
        if failed_items:
            btn_frame = tk.Frame(dialog)
            btn_frame.grid(row=2, column=0, columnspan=2, pady=(10, 0))
            
            def fill_failed():
                self.cid_text_advanced.delete('1.0', tk.END)
                self.name_text_advanced.delete('1.0', tk.END)
                
                cids = [item['cid'] for item in failed_items]
                names = [item['name'] if item['name'] != item['cid'] else item['cid'] 
                        for item in failed_items]
                
                self.cid_text_advanced.insert('1.0', '\n'.join(cids))
                self.name_text_advanced.insert('1.0', '\n'.join(names))
            
            tk.Button(btn_frame, text="填写失败CID及文件名到主输入框", width=30, command=fill_failed).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="确定", width=10, command=dialog.destroy).pack(side=tk.LEFT, padx=5)
        else:
            btn_frame = tk.Frame(dialog)
            btn_frame.grid(row=2, column=0, columnspan=2, pady=(10, 0))
            tk.Button(btn_frame, text="确定", width=10, command=dialog.destroy).pack()
        
        dialog.focus_set()
        dialog.grab_set()
        dialog.transient(self.root)

    # ==================== 10.其他功能 ====================
    
    def import_json_file_gui(self):
        """导入JSON文件"""
        filepath = filedialog.askopenfilename(
            title="选择 JSON 文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if not filepath:
            return
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            cids, names = [], []
            
            def extract_item(item):
                cid = item.get('fileCid') or item.get('Hash')
                name = item.get('fileName') or item.get('Name') or cid
                return cid, name
            
            # 处理不同格式
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        cid, name = extract_item(item)
                        if cid:
                            cids.append(cid)
                            names.append(name)
            elif isinstance(data, dict):
                if 'files' in data:
                    for item in data['files']:
                        cid, name = extract_item(item)
                        if cid:
                            cids.append(cid)
                            names.append(name)
                else:
                    cid, name = extract_item(data)
                    if cid:
                        cids.append(cid)
                        names.append(name)
            
            if not cids:
                messagebox.showwarning("警告", "未找到有效的CID")
                return
            
            self.cid_text_advanced.delete("1.0", tk.END)
            self.name_text_advanced.delete("1.0", tk.END)
            self.cid_text_advanced.insert(tk.END, "\n".join(cids))
            self.name_text_advanced.insert(tk.END, "\n".join(names))
            
            missing = sum(1 for i in range(len(cids)) if names[i] == cids[i])
            status = f"成功导入 {len(cids)} 个CID"
            if missing:
                status += f"（其中 {missing} 个使用CID作为文件名）"
            
            self.update_status_label(status)
            
        except Exception as e:
            messagebox.showerror("错误", f"导入失败：{str(e)}")

    def export_json_file_gui(self):
        """
        导出当前的CID和文件名到JSON文件
        """
        try:
            # 获取文件名和CID
            filenames = self.cid_input_text.get("1.0", tk.END).strip().split("\n")
            cids = self.cid_output_text.get("1.0", tk.END).strip().split("\n")

            # 检查是否为空（过滤可能的空字符串元素）
            filenames = [f for f in filenames if f.strip()]
            cids = [c for c in cids if c.strip()]
            
            # 检查是否至少有一个文件和CID
            if not filenames or not cids:
                messagebox.showwarning("警告", "请至少输入一个文件与CID")
                self.logger.warning("No files or CIDs provided for export")
                return
                
            # 确保文件名和CID数量一致
            if len(filenames) != len(cids):
                messagebox.showwarning("警告", "文件名和CID数量不一致，无法导出")
                self.logger.warning("Mismatch between filenames and CIDs")
                return

            total_size = 0
            total_files = len(filenames)

            # 生成文件列表，并计算文件大小
            file_data = []
            for filename, cid in zip(filenames, cids):
                basename = os.path.basename(filename)
                file_size = 0

                if os.path.exists(filename):
                    file_size = os.path.getsize(filename)
                    total_size += file_size

                file_data.append({
                    "fileCid": cid,
                    "fileName": basename,
                    "fileSize": file_size
                })
                self.root.update()

            # 构造JSON数据
            data = {
                "meta": {
                    "generatedBy": "IPFSShareAssistant",
                    "version": "1.0",
                    "created": datetime.now().isoformat(),
                    "totalFiles": total_files,
                    "totalSize": total_size
                },
                "files": file_data
            }

            # 打开文件保存对话框
            file_path = filedialog.asksaveasfilename(
                title="保存 JSON 文件",
                defaultextension=".json",
                initialfile="output.json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )

            if not file_path:
                return

            # 写入JSON文件
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)

            self.logger.info(f"Successfully exported JSON to {file_path}")

            # 创建自定义对话框
            dialog = tk.Toplevel(self.root)
            dialog.title("成功")
            dialog.geometry("300x120")
            dialog.transient(self.root)  # 设置为主窗口的子窗口
            dialog.grab_set()  # 模态对话框，阻止主窗口操作

            # 设置图标
            icon_path = os.path.join(self.app_path, "assets", "ipfs_importer_icon.ico")
            if os.path.exists(icon_path):
                dialog.iconbitmap(icon_path)

            # 窗口居中显示
            dialog.update_idletasks()  # 更新窗口尺寸信息
            width = dialog.winfo_width()
            height = dialog.winfo_height()
            x = (self.root.winfo_width() // 2) - (width // 2) + self.root.winfo_x()
            y = (self.root.winfo_height() // 2) - (height // 2) + self.root.winfo_y()
            dialog.geometry(f"+{x}+{y}")

            # 显示信息
            msg = f"成功导出到\n{file_path}"
            tk.Label(dialog, text=msg, wraplength=280, padx=10, pady=10).pack()

            # 按钮框架
            btn_frame = tk.Frame(dialog)
            btn_frame.pack(pady=5)

            # 打开文件夹按钮
            def open_folder():
                folder = os.path.dirname(file_path)
                if os.path.exists(folder):
                    # 跨平台打开文件夹
                    if os.name == 'nt':  # Windows
                        os.startfile(folder)
                    elif os.name == 'posix':  # macOS/Linux
                        subprocess.run(['open' if os.uname().sysname == 'Darwin' else 'xdg-open', folder])
                dialog.destroy()

            tk.Button(btn_frame, text="打开导出文件夹", command=open_folder, width=15).pack(side=tk.LEFT, padx=5)

            # 确定按钮
            tk.Button(btn_frame, text="确定", command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=5)

            # 等待对话框关闭
            self.root.wait_window(dialog)

        except Exception as e:
            messagebox.showerror("错误", f"导出过程中发生错误：{str(e)}")
            self.logger.error(f"Error during JSON export: {str(e)}")


    def start_gc(self):
        """启动垃圾回收"""
        if not messagebox.askyesno("确认", "执行清理会解除所有固定的对象并运行垃圾回收。确定继续吗？"):
            return
        
        self.gc_button.config(state=tk.DISABLED)
        threading.Thread(target=self._execute_gc, daemon=True).start()

    def _execute_gc(self):
        """执行垃圾回收"""
        success = False
        unpinned = freed = 0
        try:
            cleaner = IPFSCleaner(
                ipfs_path=self.kubo.kubo_path,
                repo_path=self.repo_path,
                logger=self.logger
            )

            success, unpinned, freed = cleaner.clean_all(self.update_status_label)
        except Exception as e:
            self.logger.error(f"GC error: {e}")
            self._call_ui(lambda: messagebox.showerror("错误", f"清理失败：{e}"))
            self.update_status_label("清理失败")
            self._call_ui(lambda: self.gc_button.config(state=tk.NORMAL))
            return

        def finish_ui():
            try:
                if success:
                    if unpinned > 0:
                        msg = f"清理完成！\n解固定: {unpinned} 个\n释放空间: {IPFSCleaner.format_size(freed)}"
                    else:
                        msg = f"垃圾回收完成！\n没有固定对象\n释放空间: {IPFSCleaner.format_size(freed)}"
                    messagebox.showinfo("完成", msg)
                else:
                    raise Exception("清理过程发生错误，请检查 IPFS 仓库是否可用。")
            except Exception as e:
                self.logger.error(f"GC error: {e}")
                messagebox.showerror("错误", f"清理失败：{e}")
            finally:
                self.gc_button.config(state=tk.NORMAL)
                self.update_status_label("清理完成")

        self._call_ui(finish_ui)

    def open_plugin_launcher(self):
        """插件启动器：列出 plugins 目录下的脚本并支持后台启动"""
        if self.plugin_window and self.plugin_window.winfo_exists():
            self.plugin_window.deiconify()
            self.plugin_window.lift()
            self.plugin_window.focus_force()
            self.refresh_plugin_list()
            return

        self.plugin_window = tk.Toplevel(self.root)
        self.plugin_window.title("插件启动器")
        self.plugin_window.minsize(480, 360)
        if os.path.exists(self.icon_path):
            try:
                self.plugin_window.iconbitmap(self.icon_path)
            except:
                pass
        self.plugin_window.protocol("WM_DELETE_WINDOW", self._close_plugin_window)

        header_frame = ttk.Frame(self.plugin_window)
        header_frame.pack(fill="x", padx=10, pady=8)
        ttk.Label(header_frame, text="放在 plugins 文件夹下的 .py 文件会出现在下方列表。").pack(anchor="w")
        python_label = self.runtime_pythonw or self.runtime_python or sys.executable
        ttk.Label(header_frame, text=f"使用的 Python: {python_label}", foreground="gray").pack(anchor="w")

        control_frame = ttk.Frame(self.plugin_window)
        control_frame.pack(fill="x", padx=10, pady=(0, 5))
        ttk.Button(control_frame, text="刷新列表", command=self.refresh_plugin_list, width=12).pack(side="left", padx=2)
        ttk.Button(control_frame, text="打开插件目录", command=self._open_plugins_folder, width=15).pack(side="left", padx=2)

        doc_frame = ttk.LabelFrame(self.plugin_window, text="插件说明")
        doc_frame.pack(fill="both", expand=False, padx=10, pady=(0, 8))
        doc_inner = ttk.Frame(doc_frame)
        doc_inner.pack(fill="both", expand=True, padx=5, pady=5)
        y_scroll = ttk.Scrollbar(doc_inner, orient="vertical")
        y_scroll.pack(side="right", fill="y")
        self.plugin_doc_text = tk.Text(doc_inner, height=10, wrap="word", state=tk.DISABLED, yscrollcommand=y_scroll.set)
        self.plugin_doc_text.pack(side="left", fill="both", expand=True)
        y_scroll.config(command=self.plugin_doc_text.yview)

        args_frame = ttk.Frame(self.plugin_window)
        args_frame.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(
            args_frame,
            text="可选参数（留空则不传参数；想看帮助可输入 --help (根据插件的实际设置可能会不同)；示例: --cid <CID> --interval 60 --duration 600）"
        ).pack(anchor="w")
        self.plugin_args_var = tk.StringVar(value="")
        ttk.Entry(args_frame, textvariable=self.plugin_args_var).pack(fill="x", pady=2)

        mode_frame = ttk.Frame(self.plugin_window)
        mode_frame.pack(fill="x", padx=10, pady=(0, 8))
        self.plugin_use_gui = tk.BooleanVar(value=False)
        ttk.Checkbutton(mode_frame, text="使用 GUI 模式 (pythonw，无控制台)", variable=self.plugin_use_gui).pack(anchor="w")

        outer_frame, scrollable_frame = self._create_smart_scrollable_frame(self.plugin_window)
        outer_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.plugin_list_frame = scrollable_frame

        launch_frame = ttk.Frame(self.plugin_window)
        launch_frame.pack(fill="x", padx=10, pady=(0, 10))
        self.launch_selected_button = ttk.Button(
            launch_frame,
            text="启动选中插件",
            command=self.launch_selected_plugin,
            width=18,
            state=tk.DISABLED
        )
        self.launch_selected_button.pack(side="right")

        self.selected_plugin = None

        self.refresh_plugin_list()

    def _close_plugin_window(self):
        """关闭插件窗口时重置引用"""
        if self.plugin_window and self.plugin_window.winfo_exists():
            self.plugin_window.destroy()
        self.plugin_window = None

    def _open_plugins_folder(self):
        """打开插件目录方便用户放置脚本"""
        os.makedirs(self.plugins_dir, exist_ok=True)
        try:
            if os.name == 'nt':
                os.startfile(self.plugins_dir)
            else:
                subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', self.plugins_dir])
        except Exception as e:
            self.logger.error(f"Failed to open plugins folder: {e}")
            messagebox.showerror("错误", f"无法打开插件目录：{e}")

    def refresh_plugin_list(self):
        """刷新插件列表"""
        if not hasattr(self, 'plugin_list_frame') or not self.plugin_list_frame.winfo_exists():
            return

        for child in list(self.plugin_list_frame.winfo_children()):
            child.destroy()

        plugins = self._discover_plugins()
        if not plugins:
            ttk.Label(self.plugin_list_frame, text="当前 plugins 目录为空，点击“打开插件目录”放入脚本。").pack(anchor="w", padx=5, pady=5)
            return

        # 按“标签”式网格排列按钮，点击即启动
        columns = 3
        for col in range(columns):
            self.plugin_list_frame.grid_columnconfigure(col, weight=1)

        for idx, plugin_name in enumerate(plugins):
            row, col = divmod(idx, columns)
            btn = ttk.Button(
                self.plugin_list_frame,
                text=plugin_name,
                width=24,
                command=lambda p=plugin_name: self.select_plugin(p)
            )
            btn.grid(row=row, column=col, padx=4, pady=4, sticky="ew")

    def _discover_plugins(self):
        """返回 plugins 目录下的插件列表"""
        if not os.path.exists(self.plugins_dir):
            return []
        return sorted([
            name for name in os.listdir(self.plugins_dir)
            if name.lower().endswith(".py") and not name.startswith("_")
        ])

    def select_plugin(self, plugin_name):
        """选择插件并展示文档"""
        self.selected_plugin = plugin_name
        self.launch_selected_button.config(state=tk.NORMAL)
        doc = self._load_plugin_doc(plugin_name)
        self._set_plugin_doc(doc)
        self.update_status_label(f"已选择插件：{plugin_name}")

    def _load_plugin_doc(self, plugin_name):
        """读取插件文件的顶层docstring"""
        try:
            import ast
            path = os.path.join(self.plugins_dir, plugin_name)
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
            return ast.get_docstring(tree) or "（无文档）"
        except Exception as exc:
            self.logger.warning(f"读取插件文档失败 {plugin_name}: {exc}")
            return "（无法读取插件文档）"

    def _set_plugin_doc(self, text):
        if not hasattr(self, "plugin_doc_text"):
            return
        self.plugin_doc_text.config(state=tk.NORMAL)
        self.plugin_doc_text.delete("1.0", tk.END)
        self.plugin_doc_text.insert(tk.END, text)
        self.plugin_doc_text.config(state=tk.DISABLED)

    def launch_selected_plugin(self):
        """使用当前参数启动选中的插件"""
        if not self.selected_plugin:
            messagebox.showwarning("提示", "请先选择一个插件")
            return
        self.launch_plugin_script(
            self.selected_plugin,
            self.plugin_args_var.get(),
            self.plugin_use_gui.get()
        )

    def _build_plugin_env(self):
        """构造插件运行所需环境变量，确保可以导入项目模块"""
        env = os.environ.copy()
        extra_paths = [self.app_path, self.plugins_dir, env.get("PYTHONPATH", "")]
        env["PYTHONPATH"] = os.pathsep.join([p for p in extra_paths if p])
        return env

    def launch_plugin_script(self, plugin_name, extra_args="", use_gui=False):
        """使用嵌入式Python后台启动指定插件脚本"""
        # 插件使用 console 或 GUI 解释器
        python_exec = self.runtime_pythonw if use_gui else (self.runtime_python or self.runtime_pythonw)
        if not python_exec or not os.path.exists(python_exec):
            messagebox.showerror("错误", "未找到 runtime\\python.exe 或 runtime\\pythonw.exe，无法启动插件。")
            return

        script_path = os.path.join(self.plugins_dir, plugin_name)
        if not os.path.exists(script_path):
            messagebox.showerror("错误", f"未找到插件：{script_path}")
            self.refresh_plugin_list()
            return

        creationflags = 0
        if os.name == "nt":
            creationflags = 0 if use_gui else subprocess.CREATE_NEW_CONSOLE
        try:
            arg_list = shlex.split(extra_args) if extra_args and extra_args.strip() else []
            if os.name == "nt":
                # 使用 cmd /k 保留窗口，便于查看输出/错误
                if use_gui:
                    cmd = [python_exec, script_path, *arg_list]
                else:
                    cmd = ["cmd.exe", "/k", python_exec, script_path, *arg_list]
            else:
                cmd = [python_exec, script_path, *arg_list]

            subprocess.Popen(cmd, cwd=self.app_path, env=self._build_plugin_env(), creationflags=creationflags)
            self.update_status_label(f"已启动插件：{plugin_name}")
            self.logger.info(f"Plugin launched: {plugin_name}")
        except Exception as e:
            self.logger.error(f"Failed to launch plugin {plugin_name}: {e}")
            messagebox.showerror("错误", f"启动插件失败：{plugin_name}\n{e}")

    def open_crust_pinner(self):
        """打开Crust Pinner """
        # 若窗口已存在则直接聚焦
        if self.crust_window and self.crust_window.winfo_exists():
            self.crust_window.deiconify()
            self.crust_window.lift()
            self.crust_window.focus_force()
            return
            
        # 创建窗口
        window = tk.Toplevel(self.root)
        self.crust_window = window
        window.title("CID Calculator & Crust Pinning")
        if os.path.exists(self.icon_path):
            window.iconbitmap(self.icon_path)
        
        config_file = os.path.join(self.app_path, 'config.json')
        app = IntegratedApp(window, config_file, self.logger, self.repo_path, kubo=self.kubo)
        app.cid_calculator.api_address = self.actual_api_address
        app.cid_calculator.repo_dir = self.repo_path
        app.cid_calculator.app_path = self.app_path
        
        # 定义窗口关闭回调
        def on_close():
            window.destroy()
            self.crust_window = None

        window.protocol("WM_DELETE_WINDOW", on_close)

    def open_aleph_manager(self):
        """打开集成的Aleph分享助手"""
        if self.aleph_window and self.aleph_window.winfo_exists():
            self.aleph_window.deiconify()
            self.aleph_window.lift()
            self.aleph_window.focus_force()
            return

        # 按需加载 Aleph 相关模块，避免程序启动时的副作用（如生成配置目录）
        from utils.aleph_integrated_app import AlephIntegratedApp

        # 创建子窗口
        window = tk.Toplevel(self.root)
        self.aleph_window = window
        window.title("Aleph 分享助手 - 集成版")
        window.minsize(1000, 800)  # 设置最小尺寸
        
        # 设置窗口图标
        if os.path.exists(self.icon_path):
            try:
                window.iconbitmap(self.icon_path)
            except:
                pass  # 忽略图标设置错误
        
        # 初始化Aleph集成应用实例（按 AlephIntegratedApp 签名传参）
        app = AlephIntegratedApp(
            master=window,
            app_path=self.app_path,
            config_file_path=os.path.join(self.app_path, 'config.json'),
            kubo_path=self.kubo.kubo_path if hasattr(self, 'kubo') and self.kubo else None,
            repo_path=getattr(self, "repo_path", None),
            allow_ipfs_init=False,  # 是否初始化 IPFS 由主程序决定
            logger=self.logger
        )
        
        # 定义窗口关闭回调函数
        def on_close():
            """关闭窗口时恢复按钮状态"""
            window.destroy()
            self.aleph_window = None
        
        # 绑定窗口关闭事件
        window.protocol("WM_DELETE_WINDOW", on_close)

    def open_webui(self):
        """打开WebUI"""
        parsed = urllib.parse.urlparse(self.actual_api_address)
        webbrowser.open(f"{parsed.scheme}://{parsed.netloc}/webui")

    def open_gateway(self):
        """打开网关汇总页面"""
        try:
            port = self._get_ipfs_gateway_port()
            ipns_hash = "k51qzi5uqu5djx3hvne57dwcotpc8h76o2ygrxh05kck11j6wnhvse8jrfzf2w"
            # url = f"http://{ipns_hash}.ipns.localhost:{port}" # 使用本地网关
            url = f"https://{ipns_hash}.ipns.dweb.link"       # 使用公共网关
            webbrowser.open(url)
        except Exception as e:
            self.logger.error(f"Failed to open gateway: {e}")

    def _get_ipfs_gateway_port(self):
        """获取网关端口"""
        env = os.environ.copy()
        if getattr(self, "repo_path", None):
            env["IPFS_PATH"] = self.repo_path

        # 优先使用内置 Kubo，其次回退到系统 ipfs
        cmd_candidates = []
        if getattr(self, "kubo", None) and getattr(self.kubo, "kubo_path", None):
            cmd_candidates.append(self.kubo.kubo_path)
        cmd_candidates.append("ipfs")

        for cmd in cmd_candidates:
            try:
                result = subprocess.run(
                    [cmd, "config", "Addresses.Gateway"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env
                )
            except FileNotFoundError:
                self.logger.warning(f"{cmd} not found when reading gateway port")
                continue
            except Exception as e:
                self.logger.error(f"Error running {cmd} for gateway port: {e}")
                continue

            if result.returncode == 0:
                address = result.stdout.strip()
                port = address.split("/")[-1]
                if port.isdigit():
                    return int(port)
                self.logger.warning(f"Unexpected gateway address format: {address}")

        # 解析仓库配置文件作为兜底
        try:
            config_path = os.path.join(self.repo_path, "config")
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            address = config.get("Addresses", {}).get("Gateway", "")
            port = address.split("/")[-1]
            if port.isdigit():
                return int(port)
        except Exception as e:
            self.logger.error(f"Failed to parse gateway port from config: {e}")

        raise Exception("无法获取网关端口")

    def clear_window(self):
        """重置程序"""
        # 清空文本框（安全检查）
        if hasattr(self, 'cid_text_advanced') and self.cid_text_advanced:
            self.cid_text_advanced.delete("1.0", tk.END)
        
        if hasattr(self, 'name_text_advanced') and self.name_text_advanced:
            self.name_text_advanced.delete("1.0", tk.END)
        
        if self.cid_input_text:
            self.cid_input_text.delete("1.0", tk.END)
        
        if self.cid_output_text:
            self.cid_output_text.delete("1.0", tk.END)
        
        if self.links_text:
            self.links_text.delete("1.0", tk.END)
        
        if hasattr(self, 'path_entry_advanced') and self.path_entry_advanced:
            self.path_entry_advanced.delete(0, tk.END)
        
        # 清除测速结果
        if hasattr(self, 'cid_best_gateways'):
            self.cid_best_gateways.clear()
        
        # 重新加载网关
        self.load_gateways()
        
        if hasattr(self, 'progress_bar') and self.progress_bar:
            self.progress_bar['value'] = 0
        
        self.update_status_label("已重置")

    ## ==================== 10.1 拖放处理 ====================
    
    def drop_on_cid(self, event):
        """CID输入框拖放处理"""
        files = self.root.tk.splitlist(event.data)
        for file in files:
            if self.cid_text_advanced.get("1.0", tk.END).strip():
                self.cid_text_advanced.insert(tk.END, "\n")
            self.cid_text_advanced.insert(tk.END, file)
            
            # 自动填充文件名
            if self.name_text.get("1.0", tk.END).strip():
                self.name_text_advanced.insert(tk.END, "\n")
            self.name_text_advanced.insert(tk.END, os.path.basename(file))

    def drop_on_filename(self, event):
        """文件名输入框拖放处理"""
        files = self.root.tk.splitlist(event.data)
        for file in files:
            if self.name_text.get("1.0", tk.END).strip():
                self.name_text_advanced.insert(tk.END, "\n")
            self.name_text_advanced.insert(tk.END, os.path.basename(file))

    def drop_on_path(self, event):
        """路径输入框拖放处理"""
        files = self.root.tk.splitlist(event.data)
        if files and os.path.isdir(files[0]):
            folder_name = '/' + os.path.basename(files[0])
            self.path_entry_advanced.delete("0", tk.END)
            self.path_entry.insert(tk.END, folder_name)

    def drop_on_cid_calculator(self, event):
        """CID计算器拖放处理"""
        files = self.root.tk.splitlist(event.data)
        for file in files:
            if self.cid_input_text.get("1.0", tk.END).strip():
                self.cid_input_text.insert(tk.END, "\n")
            self.cid_input_text.insert(tk.END, file)

    ## ==================== 10.2 系统托盘 ====================
    
    def create_hidden_window(self):
        """创建隐藏窗口用于托盘消息"""
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "IPFSAppTrayWindow"
        wc.lpfnWndProc = self.wndproc
        class_atom = win32gui.RegisterClass(wc)
        return win32gui.CreateWindow(class_atom, "IPFSAppTray", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None)

    def wndproc(self, hwnd, msg, wparam, lparam):
        """窗口消息处理"""
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        elif msg == self.WM_TASKBAR:
            self.on_tray_icon_command(hwnd, msg, wparam, lparam)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def create_tray_icon(self):
        """创建托盘图标"""
        self.tray_icon = None
        if os.path.exists(self.icon_path):
            try:
                self.tray_icon = win32gui.LoadImage(
                    None, self.icon_path, win32con.IMAGE_ICON, 0, 0,
                    win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE
                )
            except pywintypes.error as e:
                self.logger.error(f"Failed to load tray icon: {e}")
        
        # 创建菜单
        self.tray_menu = win32gui.CreatePopupMenu()
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 1, "打开主窗口")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 2, "IPFS WebUI")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 3, "执行 GC")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 4, "打开 Crust Pinner")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 5, "打开 Aleph Pinner")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 6, "打开插件启动器")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_SEPARATOR, 0, "")
        win32gui.AppendMenu(self.tray_menu, win32con.MF_STRING, 99, "退出")
        
        self.show_tray_icon()

    def show_tray_icon(self):
        """显示托盘图标"""
        if self.tray_icon:
            nid = (self.hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
                   self.WM_TASKBAR, self.tray_icon, "IPFS分享助手 v1.2.2-20251130")
            try:
                win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, nid)
            except pywintypes.error as e:
                self.logger.error(f"Failed to show tray icon: {e}")

    def on_tray_icon_command(self, hwnd, msg, wparam, lparam):
        """托盘图标命令处理"""
        if lparam == win32con.WM_LBUTTONUP:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        elif lparam == win32con.WM_RBUTTONUP:
            pos = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(hwnd)
            cmd = win32gui.TrackPopupMenu(
                self.tray_menu,
                win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON | win32con.TPM_RETURNCMD,
                pos[0], pos[1], 0, hwnd, None
            )
            
            if cmd == 1:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
            elif cmd == 2:
                self.open_webui()
            elif cmd == 3:
                self.start_gc()                
            elif cmd == 4:
                self.open_crust_pinner()
            elif cmd == 5:
                self.open_aleph_manager()
            elif cmd == 6:
                self.open_plugin_launcher()                
            elif cmd == 99:
                self.exit_application()
            
            win32gui.PostMessage(hwnd, win32con.WM_NULL, 0, 0)

    def message_loop(self):
        """消息循环"""
        msg = win32gui.GetMessage(None, 0, 0)
        while msg[0] != 0:
            win32gui.TranslateMessage(msg)
            win32gui.DispatchMessage(msg)
            msg = win32gui.GetMessage(None, 0, 0)

    def on_closing(self):
        """窗口关闭处理"""
        if self.minimize_to_tray.get():
            self.root.withdraw()
        else:
            self.exit_application()

    def exit_application(self):
        """退出应用"""
        self.logger.info("Exiting application")
        
        # 安全保存配置
        try:
            self.save_main_config()
        except Exception as e:
            self.logger.error(f"Error saving config on exit: {e}")
        
        # 停止 IPFS
        try:
            if hasattr(self, 'kubo') and self.kubo.process:
                self.kubo.stop_daemon()
        except Exception as e:
            self.logger.error(f"Error stopping IPFS: {e}")
        
        # 删除托盘图标
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        except Exception as e:
            self.logger.error(f"Error removing tray icon: {e}")
        
        # 退出
        try:
            self.root.quit()
            self.root.destroy()
        except:
            pass
        
        os._exit(0)

    ## ==================== 10.3 工具方法 ====================
    
    def _get_text_lines(self, text_widget):
        """从文本框获取非空行"""
        text = text_widget.get("1.0", tk.END).strip()
        return [line.strip() for line in text.split("\n") if line.strip()]

    def _get_subprocess_args(self):
        """获取subprocess参数"""
        if sys.platform.startswith('win'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            return {'startupinfo': startupinfo, 'creationflags': subprocess.CREATE_NO_WINDOW}
        return {'startupinfo': None, 'creationflags': 0}

    def _is_valid_cid(self, cid):
        """验证CID"""
        return (cid.startswith('Qm') and len(cid) == 46) or \
               (cid.startswith('b') and len(cid) > 46) or \
               (cid.startswith('k51') and len(cid) > 50)

    def _is_cid_v0(self, cid):
        """检查是否为CID v0"""
        return cid.startswith('Qm') and len(cid) == 46

    def _is_cid_v1(self, cid):
        """检查是否为CID v1"""
        return cid.startswith('b') and all(c.isalnum() or c == '-' for c in cid)

    def _format_speed(self, speed):
        """格式化速度"""
        if speed == 0:
            return "N/A"
        for unit in ['B', 'KB', 'MB', 'GB']:
            if speed < 1024.0:
                return f"{speed:.2f} {unit}"
            speed /= 1024.0
        return f"{speed:.2f} TB"

    def _parse_progress_line(self, line):
        """解析进度行"""
        match = re.search(r'(\d+\.?\d*)\s*(KiB|MiB|GiB)\s*/\s*(\d+\.?\d*)\s*(KiB|MiB|GiB)\s*(\d+\.?\d*)%', line)
        if match:
            percentage = float(match.group(5))
            return percentage
        return None

    def _call_ui(self, func, *args, **kwargs):
        """确保在UI线程执行回调"""
        if threading.current_thread() is threading.main_thread():
            func(*args, **kwargs)
        else:
            self.root.after(0, lambda: func(*args, **kwargs))

    def update_status_label(self, text):
        """更新状态标签"""
        truncated = self.truncate_path(text)
        def update():
            self.status_label.config(text=truncated)
            self.status_label.full_text = text
        self._call_ui(update)

    def _update_cid_status(self, text):
        """更新CID计算器状态"""
        def update():
            self.cid_status_label.config(text=text)
            self.root.update_idletasks()
        self.root.after(0, update)

    def truncate_path(self, path, max_length=140):
        """截断过长的路径"""
        if len(path) <= max_length:
            return path
        return path[:max_length-3] + "..."

    def show_tooltip(self, event):
        """显示工具提示"""
        if hasattr(self.status_label, 'full_text'):
            self.tooltip_label.config(text=self.status_label.full_text)
            self.update_tooltip_position(event)
            self.tooltip.deiconify()

    def hide_tooltip(self, event):
        """隐藏工具提示"""
        self.tooltip.withdraw()

    def update_tooltip_position(self, event):
        """更新工具提示位置"""
        x = self.root.winfo_pointerx() + 25
        y = self.root.winfo_pointery() + 20
        self.tooltip.geometry(f"+{x}+{y}")


def main():
    """主函数"""
    root = TkinterDnD.Tk()
    root.resizable(True, True)
    app = IPFSApp(root)
    
    # 注册任务栏消息
    message = win32gui.RegisterWindowMessage("TaskbarCreated")
    root.bind(f'<<Message{message}>>', lambda event: app.show_tray_icon())
    
    # 启动消息循环
    threading.Thread(target=app.message_loop, daemon=True).start()
    
    root.mainloop()


if __name__ == "__main__":
    main()
