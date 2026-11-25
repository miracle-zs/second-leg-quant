#!/usr/bin/env python3
"""
编译【突破追多与移动保本系统 · 实战工作台 v3.5 Pro】纯静态 HTML Shell
交付路径: index.html
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from hud_common import ROOT, render_index_shell_html


def atomic_write_text(filepath: Path, content: str) -> None:
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def main() -> None:
    html_content = render_index_shell_html()
    output_path = ROOT / "index.html"
    atomic_write_text(output_path, html_content)
    print(f"Compiled index.html (Static Shell, {len(html_content)} chars)")


if __name__ == "__main__":
    main()
