"""
示例插件：周期性执行 `python -m aleph_client file pin <CID>`。

用法：
    runtime\\python.exe plugins\\sample_pin_loop.py --cid <你的CID> --interval 60 --duration 3600

说明：
- 默认每 60 秒执行一次，持续 3600 秒（1 小时）。
- 使用当前运行的解释器调用内置的 aleph_client 模块。
- 运行时请在程序根目录下执行，或确保 PYTHONPATH 已包含程序根目录。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="周期性执行 aleph_client file pin")
    parser.add_argument("--cid", required=True, help="要 pin 的 CID")
    parser.add_argument("--interval", type=int, default=60, help="每次调用间隔秒数 (默认 60)")
    parser.add_argument("--duration", type=int, default=3600, help="持续时长秒数 (默认 3600)")
    parser.add_argument("--log", type=Path, default=None, help="可选：将输出追加写入指定日志文件")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    end_time = time.time() + args.duration
    call = [sys.executable, "-m", "aleph_client", "file", "pin", args.cid]

    print(f"[plugin] start pin loop: cid={args.cid}, interval={args.interval}s, duration={args.duration}s")
    while time.time() < end_time:
        try:
            result = subprocess.run(call, capture_output=True, text=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            output = result.stdout.strip()
            err = result.stderr.strip()
            line = f"{ts} rc={result.returncode} stdout={output or '-'} stderr={err or '-'}"
            print(line)
            if args.log:
                args.log.parent.mkdir(parents=True, exist_ok=True)
                args.log.write_text(args.log.read_text() + line + "\n" if args.log.exists() else line + "\n", encoding="utf-8")
        except KeyboardInterrupt:
            print("[plugin] interrupted, exiting.")
            return 0
        except Exception as exc:  # pragma: no cover - simple plugin logging
            print(f"[plugin] error: {exc}")
        time.sleep(args.interval)

    print("[plugin] completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main()) # 安全起见，确保返回码正确传递
