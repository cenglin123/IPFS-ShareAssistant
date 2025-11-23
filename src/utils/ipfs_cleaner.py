# src\utils\ipfs_cleaner.py

import subprocess
import sys
import logging
import re
from typing import Set, List, Tuple, Optional, Callable


class IPFSCleaner:
    """IPFS清理器类 - 负责解除固定和垃圾回收操作"""
    
    def __init__(self, ipfs_path: str, repo_path: str, logger: Optional[logging.Logger] = None):
        """
        初始化IPFS清理器
        
        Args:
            ipfs_path: IPFS可执行文件路径
            repo_path: IPFS仓库路径
            logger: 日志记录器，如果为None则创建新的
        """
        self.ipfs_path = ipfs_path
        self.repo_path = repo_path
        self.logger = logger or logging.getLogger(__name__)
        
        # Windows系统配置
        if sys.platform.startswith('win'):
            self.startupinfo = subprocess.STARTUPINFO()
            self.startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            self.startupinfo.wShowWindow = subprocess.SW_HIDE
            self.creationflags = subprocess.CREATE_NO_WINDOW
        else:
            self.startupinfo = None
            self.creationflags = 0
    
    def run_ipfs_command(self, cmd: List[str], timeout: int = 30) -> str:
        """执行IPFS命令并返回输出"""
        full_cmd = [self.ipfs_path, "--repo-dir", self.repo_path] + cmd
        
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                check=True,
                encoding='utf-8',
                errors='ignore',
                timeout=timeout,
                startupinfo=self.startupinfo,
                creationflags=self.creationflags
            )
            output = result.stdout
            return output.strip() if output else ""
        except subprocess.TimeoutExpired:
            self.logger.error(f"IPFS命令超时 ({timeout}秒): {' '.join(cmd)}")
            raise
        except subprocess.CalledProcessError as e:
            self.logger.error(f"IPFS命令执行失败: {' '.join(cmd)}")
            stderr_msg = e.stderr if e.stderr else "未知错误"
            self.logger.error(f"错误信息: {stderr_msg}")
            raise
    
    def get_all_pinned_objects(self) -> Set[str]:
        """获取所有固定的对象哈希"""
        self.logger.info("获取所有固定的对象...")
        
        try:
            # 获取递归固定的对象
            recursive_output = self.run_ipfs_command(['pin', 'ls', '--type=recursive', '--quiet'])
            
            # 获取直接固定的对象
            direct_output = self.run_ipfs_command(['pin', 'ls', '--type=direct', '--quiet'])
            
            pinned = set()
            
            # 处理递归固定的对象
            if recursive_output:
                for line in recursive_output.split('\n'):
                    line = line.strip()
                    if line:
                        hash_value = line.split()[0] if line.split() else ""
                        if hash_value and (hash_value.startswith('Qm') or hash_value.startswith('bafy') or len(hash_value) >= 40):
                            pinned.add(hash_value)
            
            # 处理直接固定的对象
            if direct_output:
                for line in direct_output.split('\n'):
                    line = line.strip()
                    if line:
                        hash_value = line.split()[0] if line.split() else ""
                        if hash_value and (hash_value.startswith('Qm') or hash_value.startswith('bafy') or len(hash_value) >= 40):
                            pinned.add(hash_value)
            
            self.logger.info(f"找到 {len(pinned)} 个固定的对象")
            return pinned
            
        except Exception as e:
            self.logger.error(f"获取固定对象列表失败: {e}")
            return set()
    
    def unpin_all_objects(self, pinned_objects: Set[str], progress_callback: Optional[Callable[[int, int, str], None]] = None) -> int:
        """
        解除所有对象的固定状态
        
        Args:
            pinned_objects: 要解固定的对象集合
            progress_callback: 进度回调函数 (current, total, message)
            
        Returns:
            成功解固定的对象数量
        """
        if not pinned_objects:
            self.logger.info("没有找到固定的对象")
            return 0
        
        success_count = 0
        error_count = 0
        total = len(pinned_objects)
        
        self.logger.info(f"开始解固定 {total} 个对象...")
        
        for i, obj_hash in enumerate(pinned_objects, 1):
            try:
                # 更新进度
                if progress_callback:
                    progress_callback(i, total, f"解固定: {obj_hash[:12]}...")
                
                # 先尝试递归解固定
                try:
                    self.run_ipfs_command(['pin', 'rm', obj_hash])
                    self.logger.debug(f"[{i}/{total}] 成功解固定: {obj_hash}")
                    success_count += 1
                except subprocess.CalledProcessError:
                    # 如果递归解固定失败，尝试直接解固定
                    try:
                        self.run_ipfs_command(['pin', 'rm', '--type=direct', obj_hash])
                        self.logger.debug(f"[{i}/{total}] 成功解固定(直接): {obj_hash}")
                        success_count += 1
                    except subprocess.CalledProcessError as e:
                        self.logger.warning(f"[{i}/{total}] 解固定失败 {obj_hash}: {e.stderr}")
                        error_count += 1
                
            except Exception as e:
                self.logger.error(f"[{i}/{total}] 解固定时发生意外错误 {obj_hash}: {e}")
                error_count += 1
        
        self.logger.info(f"解固定完成: 成功 {success_count}, 失败 {error_count}")
        return success_count
    
    def get_repo_size(self) -> int:
        """获取仓库大小（字节）"""
        try:
            output = self.run_ipfs_command(['repo', 'stat'])
            match = re.search(r'RepoSize:\s+(\d+)', output)
            if match:
                return int(match.group(1))
            return 0
        except Exception as e:
            self.logger.error(f"获取仓库大小失败: {e}")
            return 0
    
    def run_garbage_collection(self, progress_callback: Optional[Callable[[str], None]] = None) -> Tuple[bool, int]:
        """
        运行垃圾回收
        
        Args:
            progress_callback: 进度回调函数 (message)
            
        Returns:
            (成功标志, 释放的字节数)
        """
        try:
            # 获取GC前的大小
            if progress_callback:
                progress_callback("获取仓库大小...")
            size_before = self.get_repo_size()
            
            # 运行垃圾回收
            if progress_callback:
                progress_callback("正在运行垃圾回收...")
            self.logger.info("开始垃圾回收...")
            
            output = self.run_ipfs_command(['repo', 'gc'], timeout=300)  # 5分钟超时
            
            # 获取GC后的大小
            if progress_callback:
                progress_callback("计算释放的空间...")
            size_after = self.get_repo_size()
            
            space_freed = size_before - size_after
            self.logger.info(f"垃圾回收完成，释放空间: {self.format_size(space_freed)}")
            
            return True, space_freed
            
        except subprocess.TimeoutExpired:
            self.logger.warning("垃圾回收超时")
            return False, 0
        except Exception as e:
            self.logger.error(f"垃圾回收执行失败: {e}")
            return False, 0
    
    def clean_all(self, progress_callback: Optional[Callable[[str], None]] = None) -> Tuple[bool, int, int]:
        """
        执行完整的清理流程：解除所有固定 + 垃圾回收
        
        Args:
            progress_callback: 进度回调函数 (message)
            
        Returns:
            (成功标志, 解固定的对象数量, 释放的字节数)
        """
        try:
            unpinned_count = 0
            
            # 1. 获取所有固定对象
            if progress_callback:
                progress_callback("扫描固定的对象...")
            pinned_objects = self.get_all_pinned_objects()
            
            # 2. 如果有固定对象，则解除固定
            if pinned_objects:
                self.logger.info(f"找到 {len(pinned_objects)} 个固定的对象，正在解除固定...")
                
                def unpin_progress(current, total, message):
                    if progress_callback:
                        progress_callback(f"解固定进度: {current}/{total} - {message}")
                
                unpinned_count = self.unpin_all_objects(pinned_objects, unpin_progress)
                self.logger.info(f"成功解固定 {unpinned_count} 个对象")
            else:
                self.logger.info("没有找到固定的对象，跳过解固定步骤")
                if progress_callback:
                    progress_callback("没有找到固定的对象")
            
            # 3. 无论是否有固定对象，都运行垃圾回收
            self.logger.info("开始运行垃圾回收...")
            if progress_callback:
                progress_callback("准备运行垃圾回收...")
            
            def gc_progress(message):
                if progress_callback:
                    progress_callback(message)
            
            gc_success, space_freed = self.run_garbage_collection(gc_progress)
            
            if gc_success:
                self.logger.info(f"清理完成: 解固定 {unpinned_count} 个对象, 释放空间 {self.format_size(space_freed)}")
            else:
                self.logger.warning("垃圾回收执行失败")
            
            return gc_success, unpinned_count, space_freed
            
        except Exception as e:
            self.logger.error(f"清理过程发生错误: {e}")
            return False, 0, 0
    
    def gc_only(self, progress_callback: Optional[Callable[[str], None]] = None) -> Tuple[bool, int]:
        """
        仅运行垃圾回收，不解除固定
        
        Args:
            progress_callback: 进度回调函数 (message)
            
        Returns:
            (成功标志, 释放的字节数)
        """
        self.logger.info("开始仅垃圾回收模式...")
        
        def gc_progress(message):
            if progress_callback:
                progress_callback(message)
        
        return self.run_garbage_collection(gc_progress)
        """格式化字节大小为人类可读格式"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_in_bytes < 1024.0:
                return f"{size_in_bytes:.2f} {unit}"
            size_in_bytes /= 1024.0
        return f"{size_in_bytes:.2f} PB"
    
    @staticmethod
    def format_size(size_in_bytes: int) -> str:
        """格式化字节大小为人类可读格式"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_in_bytes < 1024.0:
                return f"{size_in_bytes:.2f} {unit}"
            size_in_bytes /= 1024.0
        return f"{size_in_bytes:.2f} PB"