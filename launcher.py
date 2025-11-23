import ctypes
import os
import subprocess
import sys


def _message_box(title: str, message: str) -> None:
    """Show a Windows message box without requiring tkinter."""
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        # 作为兜底，打印到标准错误（调试用）
        print(f"{title}: {message}", file=sys.stderr)


def main() -> int:
    """Launcher shell that forwards to the embedded runtime."""
    base_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    runtime_dir = os.path.join(base_dir, "runtime")

    python_candidates = [
        os.path.join(runtime_dir, "pythonw.exe"),
        os.path.join(runtime_dir, "python.exe"),
    ]
    python_exec = next((p for p in python_candidates if os.path.exists(p)), None)

    if not python_exec:
        _message_box("启动失败", "未找到 runtime\\pythonw.exe 或 runtime\\python.exe，请确认运行目录完整。")
        return 1

    gui_entry = os.path.join(base_dir, "src", "ipfs_gui.py")
    if not os.path.exists(gui_entry):
        _message_box("启动失败", "未找到 ipfs_gui.py，请确认程序目录是否完整。")
        return 1

    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        subprocess.Popen([python_exec, gui_entry], cwd=base_dir, creationflags=creationflags)
    except Exception as exc:  # pragma: no cover - 仅用于用户态弹窗
        _message_box("启动失败", f"无法启动 GUI：{exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
