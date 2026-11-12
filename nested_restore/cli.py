#!/usr/bin/env python3
"""
cli.py — 嵌套列还原作业交付入口。

用法：
    python3 cli.py <input.json> <output.json>

* 输入 JSON 含 schema / row_count / columns。
* 还原成功：先写同目录临时文件再 os.replace 原子替换输出。
* 任何校验 / 解码 / 合并失败：输出文件原样保留，进程以非零码退出，
  错误信息（含列路径与令牌下标）写入 stderr。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nested_restore import RestoreError, restore  # noqa: E402


def run(input_path: str, output_path: str) -> int:
    try:
        with open(input_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"读取/解析输入失败: {exc}", file=sys.stderr)
        return 2

    try:
        records = restore(payload)
    except RestoreError as exc:
        # 关键：失败时绝不触碰输出文件，旧结果保留。
        print(f"还原被拒绝: {exc}", file=sys.stderr)
        return 1

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            prefix=".nested_restore.", suffix=".tmp", dir=out_dir
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, output_path)  # 成功才替换
    except OSError as exc:
        print(f"写输出失败（旧结果保留）: {exc}", file=sys.stderr)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return 2
    return 0


def main(argv: List[str]) -> int:
    if len(argv) != 3:
        print("用法: python3 cli.py <input.json> <output.json>", file=sys.stderr)
        return 64
    return run(argv[1], argv[2])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
