# src\utils\ipfs_embedded_kubo.py
# IPFS Kubo 嵌入式管理器

import subprocess
import os
import sys
import re
import tarfile
import requests
from requests.exceptions import RequestException
import zipfile
import platform
import time
import urllib.parse
import psutil
import shutil  # ★ 新增：用于删除临时目录等操作


class EmbeddedKubo:
    """IPFS Kubo 嵌入式管理器"""
    
    def __init__(self, app_path, logger=None, repo_path=None, auto_update=False):
        self.logger = logger
        self.app_path = app_path
        self.auto_update = auto_update
        # 如果传进来的路径本身就是 kubo 目录，就不要再拼一层 kubo
        last_component = os.path.basename(app_path.rstrip(os.sep)).lower()
        if last_component == "kubo":
            # app_path 设成上一级目录，kubo_dir 用传进来的这个
            self.app_path = os.path.dirname(app_path)
            self.kubo_dir = app_path
        else:
            self.app_path = app_path
            self.kubo_dir = os.path.join(self.app_path, 'kubo')
        self.process = None  # 守护进程标识
        self.api_url = None
        self.stopping = False

        # 清理历史遗留的 kubo\kubo 目录（如果有的话）
        self._cleanup_legacy_nested_kubo()
        
        # 初始化 Kubo
        self.kubo_version = self._get_latest_kubo_version()
        self.kubo_path = self._setup_kubo()
        self.repo_path = self._find_ipfs_repo(repo_path)

    # ==================== 版本管理 ====================
    
    def _get_latest_kubo_version(self):
        """获取最新的 Kubo 版本"""
        try:
            response = requests.get("https://dist.ipfs.tech/kubo/versions", timeout=10)
            versions = response.text.strip().split('\n')
            latest = versions[-1]
            self._log_info(f"Latest Kubo version: {latest}")
            return latest
        except Exception as e:
            self._log_error(f"Error fetching latest Kubo version: {e}")
            return "v0.18.1"  # fallback

    def _get_current_kubo_version(self, kubo_path):
        """获取当前安装的 Kubo 版本"""
        try:
            result = subprocess.run(
                [kubo_path, "version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            match = re.search(r"ipfs version (.+)", result.stdout)
            if match:
                version = f"v{match.group(1)}"
                self._log_info(f"Current Kubo version: {version}")
                return version
        except Exception as e:
            self._log_error(f"Error getting current Kubo version: {e}")
        return None

    def check_and_migrate_repo(self):
        """检查并迁移仓库版本"""
        try:
            # 获取仓库版本
            result = subprocess.run(
                [self.kubo_path, "repo", "version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            repo_match = re.search(r'fs-repo@(\d+)', result.stdout)
            if not repo_match:
                self._log_warning(f"Unable to parse repository version: {result.stdout}")
                return
            
            repo_version = int(repo_match.group(1))
            
            # 获取 Kubo 版本
            kubo_result = subprocess.run(
                [self.kubo_path, "version", "--repo"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            kubo_match = re.search(r'fs-repo@(\d+)', kubo_result.stdout)
            if not kubo_match:
                self._log_warning(f"Unable to parse Kubo version: {kubo_result.stdout}")
                return
            
            kubo_version = int(kubo_match.group(1))
            
            self._log_info(f"Repository version: {repo_version}, Kubo version: {kubo_version}")
            
            # 版本比较和迁移
            if repo_version > kubo_version:
                self._log_info(f"Migrating repository from v{repo_version} to v{kubo_version}")
                subprocess.run([self.kubo_path, "repo", "migrate"], check=True, timeout=300)
            elif repo_version < kubo_version:
                self._log_warning(f"Repository version ({repo_version}) is lower than Kubo version ({kubo_version})")
            else:
                self._log_info("Repository and Kubo versions match")
                
        except subprocess.TimeoutExpired:
            self._log_error("Repository version check timed out")
        except subprocess.CalledProcessError as e:
            self._log_error(f"Error checking or migrating repository: {e}")
        except Exception as e:
            self._log_error(f"Unexpected error during repository check: {e}")

    # ==================== Kubo 安装管理 ====================
    
    def _setup_kubo(self):
        """设置 Kubo 二进制文件"""
        binary_info = self._get_binary_info()
        kubo_path = os.path.join(self.kubo_dir, binary_info['name'])
        
        # 检查是否需要更新
        if os.path.exists(kubo_path):
            current_version = self._get_current_kubo_version(kubo_path)
            if current_version == self.kubo_version:
                self._log_info(f"Kubo is up to date (version: {current_version})")
                return kubo_path
            elif self.auto_update:
                self._log_info(f"Updating Kubo from {current_version} to {self.kubo_version}")
                try:
                    os.remove(kubo_path)
                except OSError as e:
                    self._log_error(f"Failed to remove old Kubo binary: {e}")
            else:
                self._log_info(f"Kubo update available ({current_version} -> {self.kubo_version}), auto-update disabled")
                return kubo_path
        
        # 下载并安装 Kubo
        self._download_and_install_kubo(binary_info)
        
        if not os.path.exists(kubo_path):
            raise RuntimeError("Kubo binary not found after installation")
        
        return kubo_path

    def _get_binary_info(self):
        """获取当前平台的二进制文件信息"""
        system = platform.system().lower()
        machine = platform.machine().lower()
        
        if system == 'windows':
            return {
                'name': 'ipfs.exe',
                'url': f'https://dist.ipfs.tech/kubo/{self.kubo_version}/kubo_{self.kubo_version}_windows-{machine}.zip',
                'archive_type': 'zip'
            }
        elif system == 'darwin':
            return {
                'name': 'ipfs',
                'url': f'https://dist.ipfs.tech/kubo/{self.kubo_version}/kubo_{self.kubo_version}_darwin-{machine}.tar.gz',
                'archive_type': 'tar.gz'
            }
        elif system == 'linux':
            return {
                'name': 'ipfs',
                'url': f'https://dist.ipfs.tech/kubo/{self.kubo_version}/kubo_{self.kubo_version}_linux-{machine}.tar.gz',
                'archive_type': 'tar.gz'
            }
        else:
            raise RuntimeError(f"Unsupported platform: {system}")

    def _download_and_install_kubo(self, binary_info):
        """下载并安装 Kubo（解压到临时目录，避免 kubo\\kubo 嵌套）"""
        os.makedirs(self.kubo_dir, exist_ok=True)

        # 临时解压目录，避免出现 kubo\kubo 这种嵌套
        tmp_dir = os.path.join(self.kubo_dir, "_tmp")
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)
        
        # 下载
        self._log_info(f"Downloading Kubo {self.kubo_version}...")
        try:
            response = requests.get(binary_info['url'], timeout=300)
            response.raise_for_status()
        except RequestException as e:
            raise RuntimeError(f"Failed to download Kubo: {e}")
        
        # 保存压缩包到临时目录
        archive_ext = 'zip' if binary_info['archive_type'] == 'zip' else 'tar.gz'
        archive_path = os.path.join(tmp_dir, f'kubo.{archive_ext}')
        
        with open(archive_path, 'wb') as f:
            f.write(response.content)
        
        # 解压到临时目录
        try:
            if binary_info['archive_type'] == 'zip':
                with zipfile.ZipFile(archive_path, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
            else:
                with tarfile.open(archive_path, 'r:gz') as tar_ref:
                    tar_ref.extractall(tmp_dir)
        except Exception as e:
            raise RuntimeError(f"Failed to extract Kubo: {e}")
        finally:
            if os.path.exists(archive_path):
                os.remove(archive_path)
        
        # 从临时目录中找到二进制文件并移动到 kubo_dir 顶层
        self._move_binary_to_target(binary_info['name'], search_root=tmp_dir)

        # 清理临时目录
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def _move_binary_to_target(self, binary_name, search_root=None):
        """将二进制文件移动到目标位置
        
        search_root: 指定搜索根目录，默认从 self.kubo_dir 开始搜索
        """
        if search_root is None:
            search_root = self.kubo_dir
        
        target_path = os.path.join(self.kubo_dir, binary_name)
        
        for root, _, files in os.walk(search_root):
            if binary_name in files:
                src = os.path.join(root, binary_name)
                if src != target_path:
                    # 如果目标位置已有旧文件，先删掉
                    if os.path.exists(target_path):
                        try:
                            os.remove(target_path)
                        except OSError as e:
                            self._log_error(f"Failed to remove old binary at target: {e}")
                    os.rename(src, target_path)
                break

    # ==================== 仓库管理 ====================
    
    def _find_ipfs_repo(self, provided_repo_path):
        """查找 IPFS 仓库路径"""
        # 优先使用提供的路径
        if provided_repo_path and os.path.exists(os.path.join(provided_repo_path, "config")):
            self._log_info(f"Using provided IPFS repository: {provided_repo_path}")
            return provided_repo_path
        
        # 可能的仓库位置
        possible_locations = []
        
        # 环境变量
        if "IPFS_PATH" in os.environ:
            possible_locations.append(os.environ["IPFS_PATH"])
        
        # 默认位置
        possible_locations.extend([
            os.path.join(os.getenv("APPDATA", ""), "IPFS") if os.name == 'nt' else None,
            os.path.join(os.path.expanduser("~"), ".ipfs"),
            os.path.join(self.app_path, ".ipfs"),
        ])
        
        # 过滤 None 值
        possible_locations = [loc for loc in possible_locations if loc]
        
        # 查找存在的仓库
        for location in possible_locations:
            if os.path.exists(os.path.join(location, "config")):
                self._log_info(f"Found existing IPFS repository: {location}")
                return location
        
        # 使用默认路径
        default_path = provided_repo_path if provided_repo_path else os.path.join(self.app_path, ".ipfs")
        self._log_info(f"No existing repository found, using: {default_path}")
        return default_path

    def initialize_ipfs(self):
        """初始化 IPFS 仓库"""
        config_path = os.path.join(self.repo_path, "config")
        
        if os.path.exists(config_path):
            self._log_info("IPFS repository already initialized")
            return
        
        self._log_info(f"Initializing IPFS repository at {self.repo_path}")
        
        try:
            env = os.environ.copy()
            env['IPFS_PATH'] = self.repo_path
            
            result = subprocess.run(
                [self.kubo_path, "init"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            self._log_info(f"IPFS initialized: {result.stdout}")
            
        except subprocess.TimeoutExpired:
            self._log_error("IPFS initialization timed out")
            raise
        except subprocess.CalledProcessError as e:
            self._log_error(f"Failed to initialize IPFS: {e.stderr}")
            raise

    def get_api_address(self):
        """获取 API 地址"""
        api_file = os.path.join(self.repo_path, 'api')
        
        if os.path.exists(api_file):
            with open(api_file, 'r') as f:
                api_address = f.read().strip()
            
            standardized = self._standardize_api_address(api_address)
            self._log_info(f"API address: {standardized}")
            return standardized
        
        default_address = "http://127.0.0.1:5001"
        self._log_warning(f"API file not found, using default: {default_address}")
        return default_address

    def _standardize_api_address(self, api_address):
        """标准化 API 地址格式"""
        # /ip4/127.0.0.1/tcp/5001 格式
        if api_address.startswith('/ip4/'):
            match = re.match(r'/ip4/([^/]+)/tcp/(\d+)', api_address)
            if match:
                host, port = match.groups()
                return f'http://{host}:{port}'
        
        # http://127.0.0.1:5001 格式
        elif api_address.startswith(('http://', 'https://')):
            parsed = urllib.parse.urlparse(api_address)
            return f'{parsed.scheme}://{parsed.netloc}'
        
        return api_address

    # ==================== 守护进程管理 ====================
    
    def start_daemon(self):
        """启动 IPFS 守护进程"""
        if self.is_ipfs_running():
            self._log_info("IPFS daemon already running")
            self.api_url = self.get_api_address()
            return
        
        self._log_info("Starting IPFS daemon...")
        self.initialize_ipfs()
        
        # 尝试不同端口
        for port in range(5001, 5101):
            if self._is_port_in_use(port):
                self._log_info(f"Port {port} in use, trying next")
                continue
            
            self._log_info(f"Attempting to start daemon on port {port}")
            self.process = self._run_daemon(port)
            
            if self.process:
                self._log_info(f"Daemon started with PID: {self.process.pid}")
                time.sleep(2)  # 等待 API 文件生成
                
                self.api_url = self.get_api_address()
                if self.api_url:
                    self._log_info(f"Daemon ready, API: {self.api_url}")
                    return
                else:
                    self._log_error("Failed to get API address")
                    self.stop_daemon()
            else:
                self._log_error(f"Failed to start on port {port}")
        
        self._log_error("Failed to start daemon on any port")

    def _run_daemon(self, port=5001):
        """运行守护进程"""
        env = os.environ.copy()
        env['IPFS_PATH'] = self.repo_path
        
        subprocess_args = self._get_subprocess_args()
        
        # 尝试使用系统 IPFS
        try:
            self._log_info(f"Trying system IPFS on port {port}")
            process = subprocess.Popen(
                ['ipfs', 'daemon', '--api', f'/ip4/127.0.0.1/tcp/{port}'],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_args
            )
            self._log_info(f"System IPFS started, PID: {process.pid}")
            return process
        except FileNotFoundError:
            self._log_warning("System IPFS not found, using Kubo")
        except Exception as e:
            self._log_error(f"Error starting system IPFS: {e}")
        
        # 使用 Kubo
        try:
            self._log_info(f"Trying Kubo on port {port}")
            process = subprocess.Popen(
                [self.kubo_path, 'daemon', '--api', f'/ip4/127.0.0.1/tcp/{port}'],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **subprocess_args
            )
            self._log_info(f"Kubo started, PID: {process.pid}")
            return process
        except Exception as e:
            self._log_error(f"Failed to start Kubo: {e}")
            return None

    def stop_daemon(self):
        """停止 IPFS 守护进程"""
        if not self.process:
            self._log_info("No daemon to stop")
            return
        
        if self.stopping:
            return
        
        self.stopping = True
        
        try:
            if psutil.pid_exists(self.process.pid):
                self._log_info("Stopping IPFS daemon...")
                process = psutil.Process(self.process.pid)
                
                # 正常终止
                process.terminate()
                
                # 等待进程结束
                try:
                    process.wait(timeout=10)
                    self._log_info("Daemon stopped gracefully")
                except psutil.TimeoutExpired:
                    self._log_warning("Daemon not responding, force killing")
                    process.kill()
                    self._log_info("Daemon killed")
            else:
                self._log_info("Daemon process no longer exists")
                
        except psutil.NoSuchProcess:
            self._log_info("Daemon process not found")
        except Exception as e:
            self._log_error(f"Error stopping daemon: {e}")
        
        # 清理文件
        self._cleanup_daemon_files()
        
        self.process = None
        self.stopping = False

    def _cleanup_daemon_files(self):
        """清理守护进程文件"""
        files_to_remove = ['api', 'gateway']
        
        for filename in files_to_remove:
            filepath = os.path.join(self.repo_path, filename)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                    self._log_info(f"Removed {filename} file")
                except OSError as e:
                    self._log_error(f"Error removing {filename} file: {e}")

    def is_ipfs_running(self):
        """检查 IPFS 是否运行"""
        api_file = os.path.join(self.repo_path, 'api')
        
        if not os.path.exists(api_file):
            self._log_info("API file not found, IPFS not running")
            return False
        
        with open(api_file, 'r') as f:
            api_address = f.read().strip()
        
        # 解析地址
        host, port = self._parse_api_address(api_address)
        if not host or not port:
            self._log_warning(f"Invalid API address: {api_address}")
            return False
        
        # 检查端口
        if not self._is_port_in_use(port):
            self._log_info(f"Port {port} not in use, IPFS not running")
            return False
        
        # 验证 API
        api_url = f"http://{host}:{port}/api/v0/id"
        try:
            response = requests.post(
                api_url,
                timeout=5,
                proxies={'http': None, 'https': None}
            )
            
            if response.status_code == 200:
                self._log_info(f"IPFS running at {host}:{port}")
                self.api_url = f"http://{host}:{port}"
                return True
            else:
                self._log_warning(f"Unexpected status code: {response.status_code}")
        except RequestException as e:
            self._log_warning(f"Failed to connect to API: {e}")
        
        return False

    def _parse_api_address(self, api_address):
        """解析 API 地址"""
        # HTTP 格式
        parsed_url = urllib.parse.urlparse(api_address)
        if parsed_url.scheme == 'http':
            return parsed_url.hostname, parsed_url.port or 5001
        
        # /ip4/ 格式
        if api_address.startswith('/ip4/'):
            parts = api_address.split('/')
            if len(parts) >= 5:
                return parts[2], int(parts[4])
        
        return None, None

    # ==================== 工具方法 ====================
    
    def _is_port_in_use(self, port):
        """检查端口是否被占用"""
        if os.name == 'nt':  # Windows
            try:
                result = subprocess.run(
                    ['netstat', '-an'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                pattern = r'TCP\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):' + str(port) + r'\s+(\S+)\s+(LISTENING|ESTABLISHED)'
                return bool(re.search(pattern, result.stdout))
            except Exception:
                return False
        else:  # Unix-like
            try:
                result = subprocess.run(
                    ['lsof', '-i', f':{port}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return bool(result.stdout.strip())
            except Exception:
                return False

    def _get_subprocess_args(self):
        """获取 subprocess 参数"""
        if sys.platform.startswith('win'):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            return {
                'startupinfo': startupinfo,
                'creationflags': subprocess.CREATE_NO_WINDOW
            }
        return {'startupinfo': None, 'creationflags': 0}

    # ==================== 日志方法 ====================
    
    def _log_info(self, message):
        """记录信息日志"""
        if self.logger:
            self.logger.info(f"Kubo: {message}")

    def _log_warning(self, message):
        """记录警告日志"""
        if self.logger:
            self.logger.warning(f"Kubo: {message}")

    def _log_error(self, message):
        """记录错误日志"""
        if self.logger:
            self.logger.error(f"Kubo: {message}")

    # ==================== 额外清理 ====================

    def _cleanup_legacy_nested_kubo(self):
        """清理历史上遗留的 kubo\\kubo 嵌套目录"""
        nested = os.path.join(self.kubo_dir, "kubo")
        if os.path.isdir(nested):
            self._log_info("Removing legacy nested kubo directory")
            shutil.rmtree(nested, ignore_errors=True)

    # ==================== 清理 ====================
    
    def __del__(self):
        """析构函数"""
        try:
            if self.process:
                self.stop_daemon()
        except Exception as e:
            print(f"Error stopping daemon in destructor: {e}")
