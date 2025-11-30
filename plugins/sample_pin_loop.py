"""
示例插件：周期性执行 `python -m aleph_client file pin <CID>`。

用法：
    1) 仅指定 CID（简写）：
       runtime\\python.exe plugins\\sample_pin_loop.py <你的CID>
    2) 使用显式参数：
       runtime\\python.exe plugins\\sample_pin_loop.py --cid <你的CID> --interval 60 --duration 3600

说明：
- 如果没有显式传入 --cid，会自动把**第一个位置参数**当作 CID。
- --interval / --duration 如未指定，默认分别为 60 与 3600。
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
    # 不再要求必须显式传 --cid
    parser.add_argument("--cid", help="要 pin 的 CID")
    parser.add_argument("--interval", type=int, default=60, help="每次调用间隔秒数 (默认 60)")
    parser.add_argument("--duration", type=int, default=3600, help="持续时长秒数 (默认 3600)")
    parser.add_argument("--log", type=Path, default=None, help="可选：将输出追加写入指定日志文件")

    # 先解析已知参数，同时保留未知参数（位置参数 CID 会出现在 unknown 中）
    known, unknown = parser.parse_known_args()

    # 如果没有显式 --cid，就尝试从未知参数里拿第一个非选项作为 CID
    if known.cid is None:
        cid_candidate = None
        for item in unknown:
            if not item.startswith("-"):  # 避免把未知的选项名当成 CID
                cid_candidate = item
                break

        if cid_candidate is not None:
            known.cid = cid_candidate
        else:
            parser.error("缺少 CID（可以写成 --cid <CID>，也可以直接写成第一个位置参数 <CID>）")

    return known


def main() -> int:
    args = parse_args()
    end_time = time.time() + args.duration
    call = [sys.executable, "-m", "aleph_client", "file", "pin", args.cid]

    print(
        f"[plugin] start pin loop: cid={args.cid}, "
        f"interval={args.interval}s, duration={args.duration}s"
    )
    while time.time() < end_time:
        try:
            result = subprocess.run(call, capture_output=True, text=True)
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            output = result.stdout.strip()
            err = result.stderr.strip()
            line = (
                f"{ts} rc={result.returncode} "
                f"stdout={output or '-'} stderr={err or '-'}"
            )
            print(line)
            if args.log:
                args.log.parent.mkdir(parents=True, exist_ok=True)
                # 追加写入日志文件
                with args.log.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except KeyboardInterrupt:
            print("[plugin] interrupted, exiting.")
            return 0
        except Exception as exc:  # pragma: no cover - simple plugin logging
            print(f"[plugin] error: {exc}")
        time.sleep(args.interval)

    print("[plugin] completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())  # 安全起见，确保返回码正确传递
