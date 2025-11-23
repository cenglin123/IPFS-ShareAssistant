# src\utils\ipfs_crust_pinner.py

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinterdnd2 import DND_FILES, TkinterDnD
import subprocess
import threading
import logging
from datetime import datetime
import base64
import json
import time
import webbrowser
import os
import sys

from utils import EmbeddedKubo

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

try:
    from utils.config_utils import save_config_file
except ImportError:
    from config_utils import save_config_file


# ==================== 常量定义 ====================
class Constants:
    """应用常量"""
    # CID相关
    CID_V0_PREFIX = 'Qm'
    CID_V0_LENGTH = 46
    CID_V1_PREFIX = 'b'
    
    # Chunker配置
    CHUNKER_DEFAULT = "size-262144"  # 256KB
    CHUNKER_FILECOIN = "size-1048576"  # 1MB
    
    # 窗口配置
    WINDOW_WIDTH_RATIO = 0.70
    WINDOW_HEIGHT_RATIO = 0.74
    MIN_WINDOW_WIDTH = 1000
    MIN_WINDOW_HEIGHT = 800
    MIN_LEFT_FRAME_WIDTH = 600
    
    # 字体配置
    FONT_SIZE_NORMAL = 10
    FONT_SIZE_TITLE = 14
    
    # Crust公共账户
    CRUST_PUBLIC_USERNAME = 'IPFSShareAssistant'
    CRUST_PUBLIC_AUTH = 'c3ViLWNUR01xVHdjck5FZnJ1VVV2SG1pS012MnVnSzJiemM1d2p0TmI3SkNxM0R0UG04c3Y6MHgzODU1ZDNkY2Y0OTYwYjhhMzY3MmEzYTRmNGRiYjNhMWFjMDkyNDNhODdmNDgxOTdmZmUxNTRkNDExZmUzZjQ2MDhhZDY4ODUzOGNmMTk3MWE2Zjg5NTVhZjJmOWFjYzkwYjdiYzZlZGVkMzBjNmIyZGM2NGZhZGI4ZGQ1MmM4Ng=='
    
    # Crust API
    CRUST_API_BASE = 'https://pin.crustcode.com'
    CRUST_API_PSA = f'{CRUST_API_BASE}/psa'
    CRUST_API_PINS = f'{CRUST_API_BASE}/psa/pins'
    
    # 超时和重试
    DEFAULT_TIMEOUT = 300
    MAX_RETRIES = 10
    RETRY_DELAY = 5
    
    # 并发控制
    MAX_CONCURRENT_OPERATIONS = 2


# ==================== 工具类 ====================
class SubprocessHelper:
    """subprocess调用辅助类"""
    
    @staticmethod
    def get_creation_flags():
        """获取subprocess创建标志（跨平台）"""
        if sys.platform.startswith('win'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            return {'startupinfo': startupinfo, 'creationflags': subprocess.CREATE_NO_WINDOW}
        return {}
    
    @staticmethod
    def run_command(command, timeout=None, **kwargs):
        """统一执行命令"""
        flags = SubprocessHelper.get_creation_flags()
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            **flags,
            **kwargs
        )
    
    @staticmethod
    def popen_command(command, **kwargs):
        """统一创建进程"""
        flags = SubprocessHelper.get_creation_flags()
        return subprocess.Popen(command, **flags, **kwargs)


class CIDValidator:
    """CID验证和转换工具类"""
    
    @staticmethod
    def is_cid_v0(cid_string):
        """检查是否为CID v0格式"""
        return (cid_string.startswith(Constants.CID_V0_PREFIX) and 
                len(cid_string) == Constants.CID_V0_LENGTH)
    
    @staticmethod
    def is_cid_v1(cid_string):
        """检查是否为CID v1格式"""
        return (cid_string.startswith(Constants.CID_V1_PREFIX) and 
                all(c.isalnum() or c == '-' for c in cid_string))
    
    @staticmethod
    def is_valid_cid(cid):
        """检查是否为有效的CID"""
        return CIDValidator.is_cid_v0(cid) or CIDValidator.is_cid_v1(cid)


class FilenameValidator:
    """文件名验证工具类"""
    
    INVALID_CHARS = set('<>:"/\\|?*')
    MAX_LENGTH = 255
    
    @staticmethod
    def validate(filename):
        """验证文件名"""
        if not filename or not filename.strip():
            return False
        if any(char in FilenameValidator.INVALID_CHARS for char in filename):
            return False
        if len(filename) > FilenameValidator.MAX_LENGTH:
            return False
        return True


class UIHelper:
    """UI辅助工具类"""
    
    @staticmethod
    def get_system_font():
        """获取系统字体"""
        if sys.platform.startswith('win'):
            return 'Microsoft YaHei'
        elif sys.platform.startswith('darwin'):
            return 'PingFang SC'
        else:
            return 'Noto Sans CJK SC'
    
    @staticmethod
    def create_scrolled_text(parent, height=10, width=50):
        """创建带滚动条的文本框"""
        frame = ttk.Frame(parent)
        
        v_scrollbar = ttk.Scrollbar(frame, orient='vertical')
        v_scrollbar.pack(side='right', fill='y')
        
        h_scrollbar = ttk.Scrollbar(frame, orient='horizontal')
        h_scrollbar.pack(side='bottom', fill='x')
        
        text = tk.Text(frame, height=height, width=width, wrap='none',
                      yscrollcommand=v_scrollbar.set,
                      xscrollcommand=h_scrollbar.set)
        text.pack(side='left', fill='both', expand=True)
        
        v_scrollbar.config(command=text.yview)
        h_scrollbar.config(command=text.xview)
        
        return frame, text
    
    @staticmethod
    def get_text_widget(widget):
        """从frame中获取Text组件"""
        if isinstance(widget, tk.Text):
            return widget
        
        # 遍历子组件，返回实际的 Text 组件，避免误拿到滚动条
        for child in widget.winfo_children():
            if isinstance(child, tk.Text):
                return child
        
        raise TypeError("No tk.Text widget found in the provided container.")


# ==================== 配置管理 ====================
class ConfigManager:
    """配置管理类"""
    
    def __init__(self, config_file_path, logger):
        self.config_file_path = config_file_path
        self.logger = logger
        self.config = {}
        self.load_config()
    
    def load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_file_path, 'r') as f:
                self.config = json.load(f)
        except FileNotFoundError:
            self.logger.warning(f"Config file not found: {self.config_file_path}")
            self.config = {}
        except json.JSONDecodeError:
            self.logger.error(f"Error decoding config file: {self.config_file_path}")
            self.config = {}
    
    def save_config(self, new_config):
        """保存配置"""
        return save_config_file(self.config_file_path, new_config, self.logger)
    
    def get(self, key, default=None):
        """获取配置项"""
        return self.config.get(key, default)
    
    def get_crust_config(self):
        """获取Crust配置"""
        return {
            'crust_username': self.get('crust_username', ''),
            'crust_user_address': self.get('crust_user_address', ''),
            'crust_user_signature': self.get('crust_user_signature', ''),
            'crust_b64auth_encoded_data': self.get('crust_b64auth_encoded_data', ''),
            'use_public_account': self.get('use_public_account', True)
        }


# ==================== IPFS仓库管理 ====================
class IPFSRepoFinder:
    """IPFS仓库查找器"""
    
    def __init__(self, app_path, logger):
        self.app_path = app_path
        self.logger = logger
    
    def find_repo(self):
        """查找IPFS仓库"""
        possible_locations = self._get_possible_locations()
        
        for location in possible_locations:
            if os.path.exists(os.path.join(location, "config")):
                self.logger.info(f"Found existing IPFS repository at: {location}")
                return location
        
        default_path = os.path.join(self.app_path, ".ipfs")
        self.logger.info(f"No existing IPFS repository found. Using: {default_path}")
        return default_path
    
    def _get_possible_locations(self):
        """获取可能的IPFS仓库位置"""
        locations = []
        
        # 从config.json读取
        config_path = os.path.join(self.app_path, "config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    if "repo_path" in config:
                        locations.append(config["repo_path"])
            except Exception as e:
                self.logger.warning(f"Error reading config.json: {e}")
        
        # 环境变量
        if "IPFS_PATH" in os.environ:
            locations.append(os.environ["IPFS_PATH"])
        
        # 默认位置
        locations.extend([
            os.path.join(os.path.expanduser("~"), ".ipfs"),
            os.path.join(os.getenv("APPDATA", ""), "IPFS") if os.getenv("APPDATA") else None,
            os.path.join(self.app_path, ".ipfs"),
        ])
        
        return [loc for loc in locations if loc]


# ==================== 主应用类 ====================
class IntegratedApp:
    """集成应用主类"""
    
    def __init__(self, master, config_file_path=None, logger=None, repo_dir=None, kubo=None):
        self.master = master
        self.config_file_path = config_file_path or 'config.json'
        self.logger = logger or self._setup_logger()
        
        # 初始化Kubo
        self.kubo = self._initialize_kubo(kubo)
        
        # 设置窗口
        self._setup_window()
        self._setup_style()
        
        # 创建主界面
        self._create_ui(repo_dir)
    
    def _initialize_kubo(self, kubo):
        """初始化Kubo实例"""
        if kubo is None:
            default_kubo_path = os.path.join(parent_dir, "kubo")
            return EmbeddedKubo(default_kubo_path)
        elif isinstance(kubo, str):
            return EmbeddedKubo(kubo)
        elif isinstance(kubo, EmbeddedKubo):
            return kubo
        else:
            raise ValueError("Invalid kubo parameter")
    
    def _setup_window(self):
        """设置窗口大小和位置"""
        screen_width = self.master.winfo_screenwidth()
        screen_height = self.master.winfo_screenheight()
        
        window_width = int(screen_width * Constants.WINDOW_WIDTH_RATIO)
        window_height = int(screen_height * Constants.WINDOW_HEIGHT_RATIO)
        center_x = int(screen_width / 2 - window_width / 2)
        center_y = int(screen_height / 2 - window_height / 2)
        
        self.master.geometry(f'{window_width}x{window_height}+{center_x}+{center_y}')
        self.master.minsize(Constants.MIN_WINDOW_WIDTH, Constants.MIN_WINDOW_HEIGHT)
        self.master.title("CID Calculator & Crust Pinning")
    
    def _setup_style(self):
        """设置样式"""
        style = ttk.Style()
        font_family = UIHelper.get_system_font()
        
        # 配置基础样式
        for widget_type in ['TEntry', 'TButton', 'TCheckbutton', 'TRadiobutton', 
                           'TCombobox', 'TFrame', 'TLabelframe', 'TNotebook']:
            style.configure(widget_type, font=(font_family, Constants.FONT_SIZE_NORMAL))
        
        style.configure('TNotebook.Tab', font=(font_family, Constants.FONT_SIZE_NORMAL))
        
        # 大标题样式
        style.configure('BigTitle.TLabelframe', 
                       font=(font_family, Constants.FONT_SIZE_TITLE, 'bold'))
        style.configure('BigTitle.TLabelframe.Label', 
                       font=(font_family, Constants.FONT_SIZE_TITLE, 'bold'))
        
        # Text widget样式
        self.master.option_add('*Text*highlightThickness', 0)
        style.layout('Sash', [('Sash.hsash', {'sticky': 'ns'})])
        style.configure('Sash', gripcount=0, thickness=1)
    
    def _setup_logger(self):
        """设置日志"""
        log_dir = os.path.join(os.path.dirname(self.config_file_path), 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, 
                               f'integrated_app_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
        
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(log_file, encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        logger = logging.getLogger(__name__)
        logger.info("IntegratedApp logger setup complete")
        return logger
    
    def _create_ui(self, repo_dir):
        """创建UI界面"""
        # 创建PanedWindow
        paned_window = tk.PanedWindow(self.master, orient=tk.HORIZONTAL)
        paned_window.pack(fill=tk.BOTH, expand=1)
        
        # 左侧框架
        left_frame = ttk.Frame(paned_window, width=Constants.MIN_LEFT_FRAME_WIDTH)
        paned_window.add(left_frame, minsize=Constants.MIN_LEFT_FRAME_WIDTH)
        
        # 右侧框架
        right_frame = ttk.Frame(paned_window)
        paned_window.add(right_frame)
        
        # 初始化组件
        try:
            self.cid_calculator = CIDCalculator(
                left_frame, 
                kubo=self.kubo,
                logger=self.logger, 
                integrated_app=self, 
                repo_dir=repo_dir
            )
            self.logger.info("CIDCalculator initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize CIDCalculator: {e}")
            raise
        
        try:
            self.crust_pinning = CrustPinning(
                right_frame, 
                self, 
                self.config_file_path, 
                kubo_path=self.kubo.kubo_path
            )
            self.logger.info("CrustPinning initialized successfully")
        except Exception as e:
            self.logger.error(f"Failed to initialize CrustPinning: {e}")
            raise
    
    def clear_all_inputs(self):
        """清空所有输入"""
        self.cid_calculator.clear_window()
        self.crust_pinning.clear_inputs()
    
    def fill_crust_pin_input(self):
        """填充CID到Crust Pinning"""
        # 获取并验证CID
        all_cids = self.cid_calculator.output_text.get("1.0", tk.END).strip().split("\n")
        valid_cids = [cid.strip() for cid in all_cids 
                     if cid.strip() and CIDValidator.is_valid_cid(cid.strip())]
        
        if not valid_cids:
            self.logger.warning("No valid CIDs to fill")
            messagebox.showwarning("无有效 CID", "没有有效的 CID 可填充。")
            return
        
        # 获取文件名
        file_paths = [path.strip() for path in 
                     self.cid_calculator.input_text.get("1.0", tk.END).strip().split("\n") 
                     if path.strip()]
        filenames = [os.path.basename(path) for path in file_paths]
        
        # 补齐文件名
        while len(filenames) < len(valid_cids):
            filenames.append(valid_cids[len(filenames)])
        
        filenames = filenames[:len(valid_cids)]
        
        # 填充
        self._fill_crust_inputs(valid_cids, filenames)
        
        # 更新状态
        self.cid_calculator.status_label.config(
            text=f"已填充 {len(valid_cids)} 个有效 CID 和文件名到 Crust Pinning"
        )
        
        # 警告信息
        invalid_count = len(all_cids) - len(valid_cids)
        if invalid_count > 0:
            messagebox.showwarning("无效 CID", f"有 {invalid_count} 个无效的 CID 被忽略。")
        
        cids_as_filenames = len(valid_cids) - len(file_paths)
        if cids_as_filenames > 0:
            messagebox.showinfo("使用 CID 作为文件名", 
                              f"有 {cids_as_filenames} 个 CID 使用自身作为文件名。")
    
    def _fill_crust_inputs(self, cids, filenames):
        """填充Crust输入框"""
        cid_text = UIHelper.get_text_widget(self.crust_pinning.cid_input)
        cid_text.delete("1.0", tk.END)
        cid_text.insert("1.0", "\n".join(cids))
        
        filename_text = UIHelper.get_text_widget(self.crust_pinning.filename_input)
        filename_text.delete("1.0", tk.END)
        filename_text.insert("1.0", "\n".join(filenames))


# ==================== CID计算器 ====================
class CIDCalculator:
    """CID计算器类"""
    
    def __init__(self, master, kubo=None, api_address=None, repo_dir=None, 
                 logger=None, app_path=None, integrated_app=None):
        self.master = master
        self.app = integrated_app
        self.logger = logger or self._setup_default_logger()
        
        self.kubo = kubo
        self.api_address = api_address or "http://localhost:5001"
        self.app_path = app_path or os.path.dirname(os.path.abspath(__file__))
        
        # 查找IPFS仓库
        repo_finder = IPFSRepoFinder(self.app_path, self.logger)
        self.repo_dir = repo_dir or repo_finder.find_repo()
        
        # 设置IPFS路径
        self.ipfs_path = self._get_ipfs_path()
        
        # 创建UI
        self.create_widgets()
    
    def _setup_default_logger(self):
        """设置默认日志"""
        logging.basicConfig(level=logging.INFO)
        return logging.getLogger(__name__)
    
    def _get_ipfs_path(self):
        """获取IPFS可执行文件路径"""
        if self.kubo is None:
            self.logger.info("使用环境中的ipfs")
            return "ipfs"
        else:
            self.logger.info(f"使用kubo路径: {self.kubo.kubo_path}")
            return self.kubo.kubo_path
    
    def create_widgets(self):
        """创建UI组件"""
        self._create_input_section()
        self._create_options_section()
        self._create_buttons_section()
        self._create_output_section()
        self._create_status_section()
    
    def _create_input_section(self):
        """创建输入区域"""
        input_frame = ttk.LabelFrame(self.master, text="INPUT 输入", 
                                     style='BigTitle.TLabelframe')
        input_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.input_label = ttk.Label(input_frame, 
                                     text="Drag & drop files/folders or input v0 CIDs:")
        self.input_label.pack(anchor=tk.W, padx=5, pady=(5, 0))
        
        input_text_frame, self.input_text = UIHelper.create_scrolled_text(input_frame, height=10)
        input_text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # 绑定拖放
        self.input_text.drop_target_register(DND_FILES)
        self.input_text.dnd_bind('<<Drop>>', self._on_drop)
    
    def _create_options_section(self):
        """创建选项区域"""
        # 创建统一的选项框架
        options_frame = ttk.Frame(self.master.winfo_children()[0])
        options_frame.pack(anchor=tk.W, padx=5, pady=(5, 0))
        
        # CID版本选择（左侧）
        self.cid_version = tk.IntVar(value=1)  # 默认值1对应"CID v1"
        ttk.Label(options_frame, text="CID版本:").pack(side=tk.LEFT, padx=(0, 5))
        self.cid_version_dropdown = ttk.Combobox(
            options_frame,
            values=["CID v0", "CID v1", "Files > CID v0 > CID v1", "Files > CID v1 > CID v0"],
            state="readonly",
            width=25
        )
        self.cid_version_dropdown.pack(side=tk.LEFT)
        self.cid_version_dropdown.current(1)  # 默认选择"CID v1"
        
        # 绑定选择事件，将索引同步到IntVar
        def on_version_change(event):
            self.cid_version.set(self.cid_version_dropdown.current())
        self.cid_version_dropdown.bind('<<ComboboxSelected>>', on_version_change)
        
        # Filecoin选项（右侧）
        self.use_filecoin = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            options_frame, 
            text=f"使用Filecoin参数)",
            variable=self.use_filecoin
        ).pack(side=tk.LEFT, padx=(20, 0))
    
    def _create_buttons_section(self):
        """创建按钮区域"""
        button_frame = ttk.Frame(self.master)
        button_frame.pack(pady=10)
        
        buttons = [
            ("计算CID", 7, self.start_calculate_cid),
            ("复制CID", 7, self.copy_to_clipboard),
            ("填写CID到Crust Pinning", 19, self.app.fill_crust_pin_input),
            ("读取JSON文件", 12, self.import_json_file),
            ("导出JSON文件", 12, self.export_json_file),
            ("清空", 5, self.app.clear_all_inputs),
        ]
        
        for text, width, command in buttons:
            btn = ttk.Button(button_frame, text=text, width=width, command=command)
            btn.pack(side=tk.LEFT, padx=2)
            if text == "计算CID":
                self.calculate_button = btn
            elif text == "清空":
                self.clear_button = btn
    
    def _create_output_section(self):
        """创建输出区域"""
        output_frame = ttk.LabelFrame(self.master, text="OUTPUT 输出",
                                      style='BigTitle.TLabelframe')
        output_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        output_text_frame, self.output_text = UIHelper.create_scrolled_text(
            output_frame, height=10
        )
        output_text_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    
    def _create_status_section(self):
        """创建状态区域"""
        # 进度条
        self.progress = ttk.Progressbar(self.master, mode='indeterminate', length=300)
        self.progress.pack(pady=10)
        
        # 状态标签
        self.status_frame = ttk.Frame(self.master, height=20)
        self.status_frame.pack(fill=tk.X, pady=10)
        self.status_frame.pack_propagate(False)
        
        self.status_label = ttk.Label(self.status_frame, text="", anchor='w', justify='left')
        self.status_label.pack(fill=tk.BOTH, expand=True)
    
    def _on_drop(self, event):
        """处理文件拖放"""
        files = self.master.tk.splitlist(event.data)
        text_widget = UIHelper.get_text_widget(self.input_text)
        
        current_content = text_widget.get("1.0", tk.END).strip()
        
        for file in files:
            if current_content:
                text_widget.insert(tk.END, "\n")
            text_widget.insert(tk.END, file)
            current_content += "\n" + file
        
        self.logger.info(f"Files dropped: {', '.join(files)}")
    
    def start_calculate_cid(self):
        """开始计算CID"""
        items = self.input_text.get("1.0", tk.END).strip().split("\n")
        items = [item.strip() for item in items if item.strip()]
        
        if not items:
            messagebox.showwarning("Warning", "Please input files/folders or CIDs.")
            return
        
        self._set_calculating_state(True)
        self.status_label.config(text="Calculating...")
        self.logger.info("Starting CID calculation")
        threading.Thread(target=self.calculate_cid, args=(items,), daemon=True).start()
    
    def calculate_cid(self, items):
        """计算CID主逻辑"""
        results = []
        total_items = len(items)
        
        for idx, item in enumerate(items, 1):
            if not item.strip():
                continue
            
            self.master.after(0, self._update_status, 
                            f"处理 ({idx}/{total_items}): {item}")
            
            try:
                cid = self._process_item(item)
                if cid:
                    results.append(cid)
                else:
                    results.append(f"处理失败: {item}")
            except ValueError as e:
                # 处理内联CID无法转换的情况
                if "inline" in str(e).lower() or "cannot" in str(e).lower():
                    error_msg = f"{item}: 此文件为内联CID，无法转换为v0格式"
                    results.append(error_msg)
                    self.logger.warning(error_msg)
                else:
                    self.logger.error(f"Error processing {item}: {e}")
                    results.append(f"错误: {str(e)}")
            except Exception as e:
                self.logger.error(f"Error processing {item}: {e}")
                results.append(f"错误: {str(e)}")
        
        self.master.after(0, self._calculation_completed, results)
    
    def _process_item(self, item):
        """处理单个项目（文件/目录/CID）"""
        cid_version_option = self.cid_version.get()
        
        # 文件或目录
        if os.path.exists(item):
            cid = self._calculate_file_cid(item)
        # CID v0
        elif CIDValidator.is_cid_v0(item):
            cid = item
        # CID v1
        elif CIDValidator.is_cid_v1(item):
            cid = item
        else:
            self.logger.warning(f"Invalid input: {item}")
            return None
        
        # 转换 CID 版本
        if cid:
            # value=2: v0 -> v1 (Files > CID v0 > CID v1)
            if cid_version_option == 2 and CIDValidator.is_cid_v0(cid):
                cid = self._convert_to_v1(cid)
            # value=3: v1 -> v0 (Files > CID v1 > CID v0)
            elif cid_version_option == 3 and CIDValidator.is_cid_v1(cid):
                cid = self._convert_to_v0(cid)
            # value=0: v1 -> v0 (CID v0)
            elif cid_version_option == 0 and CIDValidator.is_cid_v1(cid):
                cid = self._convert_to_v0(cid)
            # value=1: v0 -> v1 (CID v1)
            elif cid_version_option == 1 and CIDValidator.is_cid_v0(cid):
                cid = self._convert_to_v1(cid)
        
        return cid
    
    def _calculate_file_cid(self, file_path):
        """计算文件或目录的CID"""
        cid_version_option = self.cid_version.get()
        
        # 根据选项决定初始计算的版本
        # value=2: file -> v0 -> v1，先算 v0
        # value=3: file -> v1 -> v0，先算 v1
        # value=0: 直接算 v0
        # value=1: 直接算 v1
        if cid_version_option == 2:
            target_version = 0  # 先算v0，后面会转v1
        elif cid_version_option == 3:
            target_version = 1  # 先算v1，后面会转v0
        else:
            target_version = cid_version_option
        
        command = [
            self.ipfs_path, "add", "--only-hash", "-Q", 
            "--repo-dir", self.repo_dir,
            f"--cid-version={target_version}"
        ]
        
        if os.path.isdir(file_path):
            command.append("-r")
        
        # Chunker
        chunker = (Constants.CHUNKER_FILECOIN if self.use_filecoin.get() 
                  else Constants.CHUNKER_DEFAULT)
        command.extend(["--chunker", chunker])
        command.append(file_path)
        
        self.logger.info(f"CID计算参数: filecoin={self.use_filecoin.get()}, "
                        f"chunker={chunker}, version={target_version}")
        
        result = SubprocessHelper.run_command(command, timeout=Constants.DEFAULT_TIMEOUT)
        
        if result.returncode != 0:
            self.logger.error(f"CID calculation failed: {result.stderr}")
            return None
        
        return result.stdout.strip()
    
    
    def _convert_to_v1(self, cid):
        """转换CID到v1"""
        command = [self.ipfs_path, "cid", "format", "-v", "1", "-b", "base32", cid]
        result = SubprocessHelper.run_command(command)
        
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            self.logger.error(f"Failed to convert to v1: {error_msg}")
            raise ValueError(error_msg)
        
        return result.stdout.strip()
    
    def _convert_to_v0(self, cid):
        """转换CID到v0"""
        command = [self.ipfs_path, "cid", "format", "-v", "0", cid]
        result = SubprocessHelper.run_command(command)
        
        if result.returncode != 0:
            error_msg = result.stderr.strip()
            self.logger.error(f"Failed to convert to v0: {error_msg}")
            # 抛出包含错误信息的异常，以便上层处理内联CID的情况
            raise ValueError(error_msg)
        
        return result.stdout.strip()
    
    def _update_status(self, text):
        """更新状态"""
        self.status_label.config(text=text)
        self.master.update_idletasks()
    
    def _calculation_completed(self, results):
        """计算完成"""
        self.progress.stop()
        self.status_label.config(text="Calculation completed")
        self._set_calculating_state(False)
        
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert(tk.END, "\n".join(results))
        self.logger.info("CID calculation completed")
    
    def _set_calculating_state(self, is_calculating):
        """设置计算状态"""
        state = tk.DISABLED if is_calculating else tk.NORMAL
        self.calculate_button.config(state=state)
        self.clear_button.config(state=state)
        if is_calculating:
            self.progress.start(10)
    
    def clear_window(self):
        """清空窗口"""
        self.input_text.delete("1.0", tk.END)
        self.output_text.delete("1.0", tk.END)
        self.cid_version.set(1)
        self.cid_version_dropdown.current(1)  # 重置下拉框显示
        self.progress['value'] = 0
        self.status_label.config(text="")
        self.logger.info("CID Calculator window cleared")
    
    def copy_to_clipboard(self):
        """复制CID到剪贴板"""
        cids = self.output_text.get("1.0", tk.END).strip()
        if cids:
            self.master.clipboard_clear()
            self.master.clipboard_append(cids)
            self.logger.info("CIDs copied to clipboard")
            self.status_label.config(text="CIDs copied to clipboard")
        else:
            self.logger.warning("No CIDs to copy")
            messagebox.showwarning("Warning", "No CID to copy")
    
    def import_json_file(self):
        """导入JSON文件"""
        file_path = filedialog.askopenfilename(
            title="选择 IPFS JSON 种子文件",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            cids, filenames = self._parse_json_data(data)
            
            if not cids:
                messagebox.showwarning("警告", "未在JSON文件中找到有效的CID")
                return
            
            self._populate_from_json(cids, filenames)
            self.status_label.config(text=f"成功导入 {len(cids)} 个CID")
            self.logger.info(f"Successfully imported {len(cids)} CIDs")
        except Exception as e:
            messagebox.showerror("错误", f"导入过程中发生错误：{str(e)}")
            self.logger.error(f"Error during JSON import: {e}")
    
    def _parse_json_data(self, data):
        """解析JSON数据"""
        cids = []
        filenames = []
        
        def extract_item(item):
            cid = item.get('fileCid') or item.get('Hash')
            filename = item.get('fileName') or item.get('Name') or cid
            return cid, filename
        
        if isinstance(data, list):
            for item in data:
                cid, filename = extract_item(item)
                if cid:
                    cids.append(cid)
                    filenames.append(filename)
        elif isinstance(data, dict):
            if 'files' in data:
                for item in data['files']:
                    cid, filename = extract_item(item)
                    if cid:
                        cids.append(cid)
                        filenames.append(filename)
            else:
                cid, filename = extract_item(data)
                if cid:
                    cids.append(cid)
                    filenames.append(filename)
        
        return cids, filenames
    
    def _populate_from_json(self, cids, filenames):
        """从JSON填充数据"""
        self.input_text.delete("1.0", tk.END)
        self.output_text.delete("1.0", tk.END)
        self.input_text.insert(tk.END, "\n".join(filenames))
        self.output_text.insert(tk.END, "\n".join(cids))
    
    def export_json_file(self):
        """导出JSON文件"""
        filenames = [f.strip() for f in 
                    self.input_text.get("1.0", tk.END).strip().split("\n") if f.strip()]
        cids = [c.strip() for c in 
               self.output_text.get("1.0", tk.END).strip().split("\n") if c.strip()]
        
        if not filenames or not cids:
            messagebox.showwarning("警告", "请至少输入一个文件与CID")
            return
        
        if len(filenames) != len(cids):
            messagebox.showwarning("警告", "文件名和CID数量不一致")
            return
        
        json_data = self._build_export_data(filenames, cids)
        
        file_path = filedialog.asksaveasfilename(
            title="保存 JSON 文件",
            defaultextension=".json",
            initialfile="output.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if not file_path:
            return
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=4, ensure_ascii=False)
            
            self.logger.info(f"Successfully exported JSON to {file_path}")
            self._show_export_success_dialog(file_path)
        except Exception as e:
            messagebox.showerror("错误", f"导出过程中发生错误：{str(e)}")
            self.logger.error(f"Error during JSON export: {e}")
    
    def _build_export_data(self, filenames, cids):
        """构建导出数据"""
        total_size = 0
        file_data = []
        
        for filename, cid in zip(filenames, cids):
            basename = os.path.basename(filename)
            file_size = os.path.getsize(filename) if os.path.exists(filename) else 0
            total_size += file_size
            
            file_data.append({
                "fileCid": cid,
                "fileName": basename,
                "fileSize": file_size
            })
        
        return {
            "meta": {
                "generatedBy": "IPFSShareAssistant",
                "version": "1.0",
                "created": datetime.now().isoformat(),
                "totalFiles": len(filenames),
                "totalSize": total_size
            },
            "files": file_data
        }
    
    def _show_export_success_dialog(self, file_path):
        """显示导出成功对话框"""
        dialog = tk.Toplevel(self.master)
        dialog.title("成功")
        dialog.geometry("300x120")
        dialog.transient(self.master)
        dialog.grab_set()
        
        # 居中显示
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        x = (self.master.winfo_width() // 2) - (width // 2) + self.master.winfo_x()
        y = (self.master.winfo_height() // 2) - (height // 2) + self.master.winfo_y()
        dialog.geometry(f"+{x}+{y}")
        
        tk.Label(dialog, text=f"成功导出到\n{file_path}", 
                wraplength=280, padx=10, pady=10).pack()
        
        btn_frame = tk.Frame(dialog)
        btn_frame.pack(pady=5)
        
        def open_folder():
            folder = os.path.dirname(file_path)
            if os.path.exists(folder):
                if os.name == 'nt':
                    os.startfile(folder)
                elif os.name == 'posix':
                    subprocess.run(['open' if os.uname().sysname == 'Darwin' 
                                  else 'xdg-open', folder])
            dialog.destroy()
        
        tk.Button(btn_frame, text="打开导出文件夹", 
                 command=open_folder, width=15).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="确定", 
                 command=dialog.destroy, width=10).pack(side=tk.LEFT, padx=5)


# ==================== Crust固定服务 ====================
class CrustPinning:
    """Crust固定服务类"""
    
    def __init__(self, master, integrated_app, config_file_path, kubo_path=None):
        self.master = master
        self.app = integrated_app
        self.logger = integrated_app.logger
        self.ipfs_path = integrated_app.cid_calculator.ipfs_path
        self.repo_dir = integrated_app.cid_calculator.repo_dir
        self.use_direct_api = True  # 使用直接的 HTTP API 方式进行固定，绕过 ipfs pin remote
        
        # 配置管理
        self.config_manager = ConfigManager(config_file_path, self.logger)
        
        # 状态管理
        self.pin_info = None
        self.pinning_queue = []
        self.is_pinning = False
        self.active_operations = 0
        self.completion_message_printed = False
        self.buttons_to_disable = []
        
        # 并发控制
        self.semaphore = threading.Semaphore(Constants.MAX_CONCURRENT_OPERATIONS)
        self.operations_lock = threading.Lock()
        
        # UI状态
        self.use_public_account = tk.BooleanVar(
            value=self.config_manager.get('use_public_account', True)
        )
        
        # 创建UI
        self.create_widgets()
    
    @staticmethod
    def _format_command(command):
        """格式化命令用于日志显示"""
        if isinstance(command, (list, tuple)):
            return ' '.join(str(part) for part in command)
        return str(command)
    
    @property
    def progress(self):
        """延迟获取progress控件"""
        return self.app.cid_calculator.progress
    
    @property
    def status_label(self):
        """延迟获取status_label控件"""
        return self.app.cid_calculator.status_label
    
    def create_widgets(self):
        """创建UI组件"""
        try:
            self.logger.info("Creating CrustPinning widgets...")
            self.master.grid_columnconfigure(0, weight=1)
            self.master.grid_rowconfigure(2, weight=1)
            
            self._create_settings_section()
            self._create_pinning_section()
            self._create_log_section()
            
            self.logger.info("CrustPinning widgets created successfully")
        except Exception as e:
            self.logger.error(f"Error creating widgets: {e}")
            raise
    
    def _create_settings_section(self):
        """创建设置区域"""
        frame = self._create_labeled_frame("Crust Settings 账户信息", 0)
        frame.grid_columnconfigure(1, weight=1)
        
        # 公共账户选项
        self.public_account_checkbox = ttk.Checkbutton(
            frame,
            text="使用公共账户固定（无法导出固定信息）",
            variable=self.use_public_account,
            command=self._on_public_account_toggle
        )
        self.public_account_checkbox.grid(row=0, column=0, columnspan=2, 
                                         sticky="w", padx=5, pady=5)
        
        # 账户信息输入
        fields = [
            ("Username 用户名昵称:", 1),
            ("User Address 账户地址:", 2),
            ("User Signature 账户签名:", 3)
        ]
        
        self.account_inputs = {}
        for label_text, row in fields:
            ttk.Label(frame, text=label_text).grid(row=row, column=0, 
                                                  sticky="e", padx=5, pady=5)
            entry = ttk.Entry(frame)
            entry.grid(row=row, column=1, sticky="ew", padx=5, pady=5)
            self.account_inputs[label_text] = entry
        
        # 映射到旧的变量名以保持兼容性
        self.username_input = self.account_inputs["Username 用户名昵称:"]
        self.address_input = self.account_inputs["User Address 账户地址:"]
        self.signature_input = self.account_inputs["User Signature 账户签名:"]
        
        # 保存按钮
        self.save_crust_config_button = ttk.Button(
            frame,
            text="保存账户信息到配置文件",
            command=self._save_crust_config
        )
        self.save_crust_config_button.grid(row=4, column=0, columnspan=2, pady=10)
        
        # 加载配置
        self._load_account_config()
        self._toggle_account_inputs()
    
    def _create_pinning_section(self):
        """创建固定区域"""
        frame = self._create_labeled_frame("Crust Pinning 固定", 1)
        frame.grid_columnconfigure(1, weight=1)
        frame.grid_rowconfigure(1, weight=1)
        
        # CID输入
        ttk.Label(frame, text="CID列表:").grid(row=0, column=0, 
                                              sticky="nw", padx=5, pady=5)
        self.cid_input = UIHelper.create_scrolled_text(frame, height=5)[0]
        self.cid_input.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        
        # 文件名输入
        ttk.Label(frame, text="文件名列表:").grid(row=1, column=0, 
                                                sticky="nw", padx=5, pady=5)
        self.filename_input = UIHelper.create_scrolled_text(frame, height=5)[0]
        self.filename_input.grid(row=1, column=1, sticky="nsew", padx=5, pady=5)
        
        # 文件名拖放
        filename_text = UIHelper.get_text_widget(self.filename_input)
        filename_text.drop_target_register(DND_FILES)
        filename_text.dnd_bind('<<Drop>>', self._on_drop_filename)
        
        # 按钮
        self._create_pinning_buttons(frame)
    
    def _create_pinning_buttons(self, parent):
        """创建固定操作按钮"""
        button_frame = ttk.Frame(parent)
        button_frame.grid(row=2, column=0, columnspan=2, pady=10, sticky="ew")
        
        buttons = [
            ("批量固定到 Crust", 15, self.pin_to_crust),
            ("检查 CID 固定状态", 15, self.check_cid_status),
            ("在 IPFS Scan 中查看 CID", 25, self._open_ipfs_scan),
            ("查看账户固定信息", 15, self.check_pin_status),
            ("导出固定信息", 10, self._export_pin_info),
        ]
        
        for idx, (text, width, command) in enumerate(buttons):
            btn = ttk.Button(button_frame, text=text, width=width, command=command)
            btn.grid(row=0, column=idx, padx=2, sticky="ew")
            
            if text == "批量固定到 Crust":
                self.pin_button = btn
            elif text == "检查 CID 固定状态":
                self.check_recent_cid_button = btn
            elif text == "查看账户固定信息":
                self.check_status_button = btn
            elif text == "导出固定信息":
                self.export_button = btn
                btn.config(state=tk.DISABLED)
    
    def _create_log_section(self):
        """创建日志区域"""
        frame = self._create_labeled_frame("Crust Pinning Logs 日志", 2)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)
        
        self.log_output = UIHelper.create_scrolled_text(frame, height=10)[0]
        self.log_output.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
    
    def _create_labeled_frame(self, text, row):
        """创建带标签的框架"""
        frame = ttk.LabelFrame(self.master, text=text, style='BigTitle.TLabelframe')
        frame.grid(row=row, column=0, sticky="nsew", padx=10, pady=5)
        return frame
    
    def _load_account_config(self):
        """加载账户配置"""
        config = self.config_manager.get_crust_config()
        self.username_input.insert(0, config['crust_username'])
        self.address_input.insert(0, config['crust_user_address'])
        self.signature_input.insert(0, config['crust_user_signature'])
    
    def _on_public_account_toggle(self):
        """公共账户切换"""
        self._toggle_account_inputs()
        self._save_crust_config()
    
    def _toggle_account_inputs(self):
        """切换账户输入框状态"""
        state = tk.DISABLED if self.use_public_account.get() else tk.NORMAL
        for entry in self.account_inputs.values():
            entry.config(state=state)
        self.save_crust_config_button.config(state=state)
        
        # 检查按钮是否已创建（在初始化时可能还未创建）
        if hasattr(self, 'check_status_button'):
            self.check_status_button.config(state=state)
        if hasattr(self, 'export_button'):
            self.export_button.config(state=tk.DISABLED)
    
    def _save_crust_config(self):
        """保存Crust配置"""
        username = self.username_input.get()
        address = self.address_input.get()
        signature = self.signature_input.get()
        use_public = self.use_public_account.get()
        
        sign_data = f"sub-{address}:{signature}"
        b64auth = base64.b64encode(sign_data.encode("utf-8")).decode("utf-8")
        
        config = {
            'crust_username': username,
            'crust_user_address': address,
            'crust_user_signature': signature,
            'crust_b64auth_encoded_data': b64auth,
            'use_public_account': use_public
        }
        
        if self.config_manager.save_config(config):
            self.status_label.config(text="Crust configuration saved successfully.")
        else:
            self._log_message("Failed to save Crust configuration.")
    
    def pin_to_crust(self):
        """固定到Crust"""
        if self.is_pinning:
            self._log_message("已有固定操作在进行中，请等待。")
            return
        
        # 获取配置
        config = self._get_active_config()
        if not config:
            return
        
        # 获取输入
        cids = self._get_cids()
        filenames = self._get_filenames()
        
        if not cids:
            messagebox.showwarning("输入错误", "请输入至少一个 CID。")
            return
        
        # 补齐文件名
        filenames = self._align_filenames(filenames, cids)
        
        # 准备固定
        service_name = self._get_service_name(config)
        if not self.use_direct_api:
            self._prepare_pinning_service(service_name, config)
        
        # 加入队列
        for cid, filename in zip(cids, filenames):
            if CIDValidator.is_valid_cid(cid):
                self._queue_pin_operation(cid, filename, service_name, config)
            else:
                self._log_message(f"无效的 CID: {cid}")
        
        self._log_message("批量固定操作已加入队列。")
        self._start_pinning()
    
    def _get_active_config(self):
        """获取当前活动配置"""
        if self.use_public_account.get():
            return {
                'crust_username': Constants.CRUST_PUBLIC_USERNAME,
                'crust_b64auth_encoded_data': Constants.CRUST_PUBLIC_AUTH
            }
        
        config = self.config_manager.get_crust_config()
        if not self._validate_config(config):
            return None
        return config
    
    def _validate_config(self, config):
        """验证配置"""
        required = ['crust_username', 'crust_user_address', 'crust_user_signature']
        for key in required:
            if not config.get(key):
                messagebox.showerror("配置错误", f"缺少{key}")
                return False
        return True
    
    def _get_cids(self):
        """获取CID列表"""
        text = UIHelper.get_text_widget(self.cid_input)
        return [cid.strip() for cid in text.get("1.0", tk.END).strip().split("\n") 
                if cid.strip()]
    
    def _get_filenames(self):
        """获取文件名列表"""
        text = UIHelper.get_text_widget(self.filename_input)
        return [fn.strip() for fn in text.get("1.0", tk.END).strip().split("\n") 
                if fn.strip() and FilenameValidator.validate(fn.strip())]
    
    def _align_filenames(self, filenames, cids):
        """对齐文件名和CID"""
        if len(filenames) < len(cids):
            self._log_message("部分CID将使用自身作为文件名。")
            while len(filenames) < len(cids):
                filenames.append(cids[len(filenames)])
        return filenames[:len(cids)]
    
    def _get_service_name(self, config):
        """获取服务名称"""
        return (Constants.CRUST_PUBLIC_USERNAME if self.use_public_account.get() 
                else config['crust_username'])
    
    def _prepare_pinning_service(self, service_name, config):
        """准备固定服务"""
        cmd_check = [self.ipfs_path, 'pin', 'remote', 'service', 'ls']
        existing = self._check_existing_services(cmd_check)
        
        if service_name not in existing:
            cmd_add = [
                self.ipfs_path, 'pin', 'remote', 'service', 'add',
                service_name, Constants.CRUST_API_PSA,
                config['crust_b64auth_encoded_data']
            ]
            self._log_message("添加远程服务...")
            self._run_command(cmd_add, "添加服务中...")
    
    def _check_existing_services(self, command):
        """检查已存在的服务"""
        process = SubprocessHelper.popen_command(
            command, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE,
            text=True, 
            encoding='utf-8', 
            errors='replace'
        )
        output, _ = process.communicate()
        
        services = set()
        for line in output.splitlines():
            parts = line.split()
            if len(parts) > 1:
                services.add(parts[0])
        
        return services
    
    def _queue_pin_operation(self, cid, filename, service_name, config):
        """加入固定队列"""
        if self.use_direct_api:
            self._queue_pin_operation_http(cid, filename, config)
            return
        
        encoded_filename = filename.encode('utf-8').decode('utf-8')
        escaped_filename = encoded_filename.replace('"', '\\"')
        quoted_filename = f'"{escaped_filename}"'
        
        cmd = [
            self.ipfs_path, 'pin', 'remote', 'add',
            f'--service={service_name}',
            f'--name={quoted_filename}',
            '--background',
            cid
        ]
        self.pinning_queue.append((cmd, f"固定 CID {cid} 为 {filename}"))
    
    def _queue_pin_operation_http(self, cid, filename, config):
        """通过直接 HTTP API 加入固定队列"""
        payload = {"cid": cid}
        if filename:
            payload["name"] = filename
        
        cmd = [
            "curl", "--silent", "--show-error", "--fail", "--location",
            "-X", "POST", Constants.CRUST_API_PINS,
            "--header", f"Authorization: Bearer {config['crust_b64auth_encoded_data']}",
            "--header", "Content-Type: application/json",
            "--data", json.dumps(payload)
        ]
        self.pinning_queue.append((cmd, f"固定 CID {cid} 为 {filename} (HTTP API)"))
    
    def _start_pinning(self):
        """开始固定处理"""
        self._disable_buttons()
        self.is_pinning = True
        self.completion_message_printed = False
        self._process_pinning_queue()
    
    def _process_pinning_queue(self):
        """处理固定队列"""
        if self.pinning_queue:
            cmd, message = self.pinning_queue.pop(0)
            self._run_command(cmd, message)
        else:
            if self.active_operations == 0 and not self.completion_message_printed:
                self.is_pinning = False
                self._enable_buttons()
                self.progress.stop()
                self._log_message("====所有操作已完成====")
                self.completion_message_printed = True
    
    def _run_command(self, command, message):
        """执行命令"""
        self._log_message(message)
        self._log_message(f"执行命令: {self._format_command(command)}")
        self.progress.start(10)
        self.active_operations += 1
        threading.Thread(
            target=self._run_command_with_retry,
            args=(command, message),
            daemon=True
        ).start()
    
    def _run_command_with_retry(self, command, message):
        """重试执行命令"""
        for attempt in range(Constants.MAX_RETRIES):
            try:
                if isinstance(command, (list, tuple)):
                    result = SubprocessHelper.run_command(
                        command,
                        timeout=30
                    )
                else:
                    result = SubprocessHelper.run_command(
                        command,
                        timeout=30,
                        shell=True
                    )
                
                if result.returncode == 0:
                    self._log_message(f"成功: {message}")
                    if result.stdout:
                        output = result.stdout.strip()
                        if len(output) > 500:
                            output = output[:500] + "..."
                        self._log_message(f"输出: {output}")
                    break
                elif "lock" in result.stderr.lower():
                    self._log_message(f"IPFS锁定，尝试释放... (尝试 {attempt + 1})")
                    self._release_ipfs_lock()
                else:
                    self._log_message(f"错误: {result.stderr}")
            except subprocess.TimeoutExpired:
                self._log_message(f"超时，重试中... (尝试 {attempt + 1})")
                time.sleep(Constants.RETRY_DELAY)
            except Exception as e:
                self._log_message(f"异常: {str(e)}")
        
        self.active_operations -= 1
        self.master.after(0, self._process_pinning_queue)
    
    def check_cid_status(self):
        """检查CID状态"""
        if self.is_pinning:
            self._log_message("已有操作在进行中。")
            return
        
        cids = self._get_cids()
        if not cids:
            messagebox.showwarning("输入错误", "请输入至少一个CID。")
            return
        
        config = self._get_active_config()
        if not config:
            return
        
        self._disable_buttons()
        self.is_pinning = True
        
        threading.Thread(
            target=self._check_cids_status,
            args=(cids, config),
            daemon=True
        ).start()
    
    def _check_cids_status(self, cids, config):
        """检查多个CID状态"""
        self.pin_info = []
        self._log_message("CID\t\t\t\t\t\t状态\t\t名称")
        self._log_message("-" * 80)
        
        for cid in set(cids):
            if CIDValidator.is_valid_cid(cid):
                self._check_single_cid_status(cid, config)
        
        self._log_message("-" * 80)
        self._log_message(f"总计检查了 {len(self.pin_info)} 个 CID。\n")
        
        self.is_pinning = False
        self._enable_buttons()
        
        if self.pin_info:
            self.master.after(0, self._enable_export_button)
    
    def _check_single_cid_status(self, cid, config):
        """检查单个CID状态"""
        url = f"{Constants.CRUST_API_PINS}?cid={cid}"
        cmd = [
            "curl", "--location", "--request", "GET", url,
            "--header", f"Authorization: Bearer {config['crust_b64auth_encoded_data']}"
        ]
        
        for attempt in range(Constants.MAX_RETRIES):
            try:
                process = SubprocessHelper.popen_command(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                stdout, _ = process.communicate(timeout=10)
                
                data = json.loads(stdout.decode('utf-8'))
                if 'results' in data and data['results']:
                    info = data['results'][0]
                    status = info['status']
                    name = info['pin']['name']
                    
                    self._log_message(f"{cid}\t{status}\t{name}")
                    self.pin_info.append({'cid': cid, 'status': status, 'name': name})
                    break
            except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
                self._log_message(f"检查 {cid} 失败，重试中...")
                time.sleep(1)
    
    def check_pin_status(self):
        """检查账户固定状态"""
        if self.use_public_account.get():
            messagebox.showinfo("公共账户", "使用公共账户时无法查看账户固定信息。")
            return
        
        if self.is_pinning:
            self._log_message("已有操作在进行中。")
            return
        
        config = self._get_active_config()
        if not config or not self._validate_config(config):
            return
        
        self._disable_buttons()
        self.is_pinning = True
        self.pin_info = None
        self.export_button.config(state=tk.DISABLED)
        
        threading.Thread(
            target=self._fetch_account_pins,
            args=(config,),
            daemon=True
        ).start()
    
    def _fetch_account_pins(self, config):
        """获取账户固定信息"""
        url = f"{Constants.CRUST_API_PINS}?limit=1000"
        cmd = [
            "curl", "--location", "--request", "GET", url,
            "--header", f"Authorization: Bearer {config['crust_b64auth_encoded_data']}"
        ]
        
        for attempt in range(Constants.MAX_RETRIES):
            try:
                process = SubprocessHelper.popen_command(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                stdout, _ = process.communicate(timeout=30)
                
                data = json.loads(stdout.decode('utf-8'))
                self._display_pin_info(data)
                break
            except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
                self._log_message(f"请求失败，重试中... (尝试 {attempt + 1})")
                time.sleep(Constants.RETRY_DELAY)
        
        self.is_pinning = False
        self._enable_buttons()
    
    def _display_pin_info(self, data):
        """显示固定信息"""
        if 'results' not in data or not data['results']:
            self._log_message("未找到任何固定信息。")
            return
        
        self.pin_info = []
        self._log_message("CID\t\t\t\t\t\t状态\t\t名称")
        self._log_message("-" * 80)
        
        for item in data['results']:
            cid = item['pin']['cid']
            status = item['status']
            name = item['pin']['name']
            
            self._log_message(f"{cid}\t{status}\t{name}")
            self.pin_info.append({'cid': cid, 'status': status, 'name': name})
        
        self._log_message("-" * 80)
        self._log_message(f"总计 {len(self.pin_info)} 条固定信息。")
        
        self.master.after(0, self._enable_export_button)
    
    def _export_pin_info(self):
        """导出固定信息"""
        if not self.pin_info:
            self._log_message("没有可用的固定信息。")
            return
        
        formatted_data = [
            {
                "Hash": item['cid'],
                "Name": item['name'],
                "Size": "0",
                "UpEndpoint": "",
                "PinEndpoint": Constants.CRUST_API_BASE
            }
            for item in self.pin_info
        ]
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json")],
            initialfile=f"pins_{timestamp}.json"
        )
        
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(formatted_data, f, ensure_ascii=False, indent=4)
                self._log_message(f"已导出到 {file_path}")
            except Exception as e:
                self._log_message(f"导出错误: {str(e)}")
    
    def _open_ipfs_scan(self):
        """在IPFS Scan中打开"""
        cids = self._get_cids()
        if cids:
            url = f"https://ipfs-scan.io/?cid={cids[0]}"
            webbrowser.open(url)
        else:
            messagebox.showwarning("输入错误", "请输入至少一个CID。")
    
    def _on_drop_filename(self, event):
        """处理文件名拖放"""
        files = self.master.tk.splitlist(event.data)
        text_widget = UIHelper.get_text_widget(self.filename_input)
        
        current = text_widget.get("1.0", tk.END).strip()
        
        for file in files:
            filename = os.path.basename(file)
            if current:
                text_widget.insert(tk.END, "\n")
            text_widget.insert(tk.END, filename)
            current += "\n" + filename
    
    def _disable_buttons(self):
        """禁用按钮"""
        self.buttons_to_disable = [
            self.pin_button,
            self.check_recent_cid_button,
            self.check_status_button
        ]
        for btn in self.buttons_to_disable:
            btn.config(state=tk.DISABLED)
    
    def _enable_buttons(self):
        """启用按钮"""
        for btn in self.buttons_to_disable:
            btn.config(state=tk.NORMAL)
    
    def _enable_export_button(self):
        """启用导出按钮"""
        self.export_button.config(state=tk.NORMAL)
    
    def _release_ipfs_lock(self):
        """释放IPFS锁"""
        try:
            lock_file = os.path.join(self.repo_dir, "repo.lock")
            if os.path.exists(lock_file):
                os.remove(lock_file)
                self._log_message("IPFS锁已释放。")
        except Exception as e:
            self._log_message(f"释放锁失败: {str(e)}")
    
    def _log_message(self, message):
        """记录日志"""
        self.master.after(0, self._log_message_gui, message)
    
    def _log_message_gui(self, message):
        """GUI日志记录"""
        log_widget = UIHelper.get_text_widget(self.log_output)
        log_widget.config(state=tk.NORMAL)
        log_widget.insert(tk.END, message + "\n")
        log_widget.see(tk.END)
        log_widget.config(state=tk.DISABLED)
        log_widget.update_idletasks()
    
    def clear_inputs(self):
        """清空输入"""
        cid_text = UIHelper.get_text_widget(self.cid_input)
        cid_text.delete("1.0", tk.END)
        
        filename_text = UIHelper.get_text_widget(self.filename_input)
        filename_text.delete("1.0", tk.END)
        
        self.logger.info("Crust Pinning inputs cleared")
