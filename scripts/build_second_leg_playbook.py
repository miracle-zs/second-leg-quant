#!/usr/bin/env python3
"""
编译【突破追多与移动保本战法手册 v3.5 Pro】HTML
交付路径: reports/strategy_second_leg_playbook.html
公共渲染、表格行、双引擎 K 线弹窗与实时轮询逻辑统一复用 scripts/hud_common.py
"""

from hud_common import ROOT, compute_pool_stats, load_candidates, render_playbook_body

HEAD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
  <meta name="color-scheme" content="dark">
  <title>突破追多与移动保本战法手册 v3.5</title>
  <script src="https://www.gstatic.com/antigravity/web/dev/tailwindcss.min.js"></script>
  <style>
    :root {
      color-scheme: dark;
    }
    body {
      background-color: #070a12;
      color: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    }
    .custom-scrollbar {
      --scrollbar-thumb: #1e293b;
      --scrollbar-track: transparent;
      scrollbar-width: thin;
      scrollbar-color: var(--scrollbar-thumb) var(--scrollbar-track);
    }
    @supports not (scrollbar-color: auto) {
      .custom-scrollbar::-webkit-scrollbar {
        width: 5px;
        height: 5px;
      }
      .custom-scrollbar::-webkit-scrollbar-thumb {
        background: var(--scrollbar-thumb);
        border-radius: 3px;
      }
    }
    .kpi-card {
      cursor: pointer;
      transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .kpi-card:hover {
      transform: translateY(-2px);
    }
    .kpi-card.active {
      ring-width: 2px;
      box-shadow: 0 8px 24px -4px rgba(0, 0, 0, 0.5);
    }
    .kpi-card.active.card-resonance {
      border-color: #f59e0b;
      background: linear-gradient(180deg, rgba(245, 158, 11, 0.12) 0%, rgba(15, 23, 42, 0.8) 100%);
    }
    .kpi-card.active.card-neg-fr {
      border-color: #f43f5e;
      background: linear-gradient(180deg, rgba(244, 63, 94, 0.12) 0%, rgba(15, 23, 42, 0.8) 100%);
    }
    .kpi-card.active.card-high-tr {
      border-color: #a855f7;
      background: linear-gradient(180deg, rgba(168, 85, 247, 0.12) 0%, rgba(15, 23, 42, 0.8) 100%);
    }
    .kpi-card.active.card-ema-bull {
      border-color: #10b981;
      background: linear-gradient(180deg, rgba(16, 185, 129, 0.12) 0%, rgba(15, 23, 42, 0.8) 100%);
    }
    .kpi-card.active.card-all {
      border-color: #38bdf8;
      background: linear-gradient(180deg, rgba(56, 189, 248, 0.12) 0%, rgba(15, 23, 42, 0.8) 100%);
    }
    th.sortable {
      cursor: pointer;
      user-select: none;
    }
    th.sortable:hover {
      color: #38bdf8;
      background-color: rgba(30, 41, 59, 0.6);
    }
    .sort-icon {
      display: inline-block;
      margin-left: 2px;
      opacity: 0.5;
    }
    th.sortable:hover .sort-icon {
      opacity: 1;
    }
    .tabular-nums {
      font-variant-numeric: tabular-nums;
    }
    .sticky-header th {
      position: sticky;
      top: 0;
      z-index: 10;
      backdrop-filter: blur(12px);
      -webkit-backdrop-filter: blur(12px);
      background-color: rgba(11, 15, 25, 0.95);
      box-shadow: 0 1px 0 rgba(51, 65, 85, 0.8);
    }
  </style>
</head>"""


def main():
    candidates = load_candidates()
    stats = compute_pool_stats(candidates)
    html_content = HEAD_HTML + "\n" + render_playbook_body(candidates, stats)

    output_path = ROOT / "reports" / "strategy_second_leg_playbook.html"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_content, encoding="utf-8")
    print(f"Compiled reports/strategy_second_leg_playbook.html (Compact HUD v3.5 Pro): Total {len(candidates)} candidates")


if __name__ == "__main__":
    main()
