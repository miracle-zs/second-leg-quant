#!/usr/bin/env python3
"""
编译【突破追多与移动保本战法手册 v3.5 Pro】纯静态 HTML
交付路径: reports/strategy_second_leg_playbook.html
"""

from __future__ import annotations

from build_index import atomic_write_text
from hud_common import ROOT, render_playbook_html


def main() -> None:
    html_content = render_playbook_html()
    output_path = ROOT / "reports" / "strategy_second_leg_playbook.html"
    atomic_write_text(output_path, html_content)
    print(f"Compiled reports/strategy_second_leg_playbook.html (Static Shell, {len(html_content)} chars)")


if __name__ == "__main__":
    main()
