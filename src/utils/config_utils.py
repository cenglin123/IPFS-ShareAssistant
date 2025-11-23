# src\utils\config_utils.py

import json
import os
from tkinter import messagebox

def save_config_file(config_path, new_config, logger=None):
    try:
        # 读取现有配置（如果存在）
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                existing_config = json.load(f)
        else:
            existing_config = {}

        # 更新配置
        existing_config.update(new_config)

        # 保存更新后的配置
        with open(config_path, 'w') as f:
            json.dump(existing_config, f, indent=4)

        success_message = "Configuration saved successfully"
        if logger:
            logger.info(success_message)
        return True, success_message
    except Exception as e:
        error_message = f"Error saving configuration file: {str(e)}"
        if logger:
            logger.error(error_message)
        messagebox.showerror("错误", "无法保存配置文件")
        return False, error_message