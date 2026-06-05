"""使用 WebUI 同款 React/Recharts 技术栈渲染统计图表。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import asyncio
import json
import logging
import os
import shutil
import subprocess

from playwright.async_api import async_playwright


logger = logging.getLogger(__name__)


class WebUIChartRenderer:
    """把图表规格渲染为 PNG。"""

    def __init__(self, plugin_dir: Path):
        self.plugin_dir = plugin_dir
        self.repo_dir = plugin_dir.parents[1]
        self.dashboard_dir = self.repo_dir / "dashboard"
        self.app_source = plugin_dir / "webui_chart_renderer_app.jsx"
        self.cache_dir = plugin_dir / "data" / "webui_chart_renderer"
        self.bundle_path = self.cache_dir / "statistics_chart.bundle.js"

    async def render(self, spec: Dict[str, Any], output_path: Path) -> Optional[Path]:
        """渲染图表，失败时返回 None。"""

        try:
            self._ensure_bundle()
            browser_path = self._find_browser_executable()
            if browser_path is None:
                logger.warning("统计图表渲染跳过: 未找到 Chrome/Edge 浏览器")
                return None

            html = self._build_html(spec)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=str(browser_path),
                    args=["--disable-gpu", "--disable-dev-shm-usage"],
                    headless=True,
                )
                page = await browser.new_page(
                    viewport={
                        "width": int(spec.get("width", 1200) or 1200),
                        "height": int(spec.get("height", 700) or 700),
                    },
                    device_scale_factor=2,
                )
                await page.set_content(html, wait_until="domcontentloaded")
                await page.wait_for_function("window.__MAIBOT_CHART_READY__ === true", timeout=20000)
                await page.locator("#chart-card").screenshot(path=str(output_path))
                await browser.close()
            return output_path
        except Exception as exc:
            logger.warning("统计图表渲染失败: %s", exc)
            return None

    def _ensure_bundle(self) -> None:
        if self._is_bundle_fresh():
            return

        node_path = shutil.which("node")
        esbuild_path = self.dashboard_dir / "node_modules" / "esbuild" / "bin" / "esbuild"
        if node_path is None:
            raise RuntimeError("未找到 node")
        if not esbuild_path.exists():
            raise RuntimeError(f"未找到 esbuild: {esbuild_path}")
        if not (self.dashboard_dir / "node_modules" / "recharts").exists():
            raise RuntimeError("dashboard/node_modules 中缺少 recharts")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["NODE_PATH"] = str(self.dashboard_dir / "node_modules")
        subprocess.run(
            [
                node_path,
                str(esbuild_path),
                str(self.app_source),
                "--bundle",
                "--format=iife",
                "--global-name=MaiBotStatsChart",
                "--platform=browser",
                "--jsx=automatic",
                f"--outfile={self.bundle_path}",
            ],
            cwd=str(self.repo_dir),
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _is_bundle_fresh(self) -> bool:
        return self.bundle_path.exists() and self.bundle_path.stat().st_mtime >= self.app_source.stat().st_mtime

    def _build_html(self, spec: Dict[str, Any]) -> str:
        bundle = self.bundle_path.read_text(encoding="utf-8")
        spec_json = json.dumps(spec, ensure_ascii=False).replace("</", "<\\/")
        width = int(spec.get("width", 1200) or 1200)
        height = int(spec.get("height", 700) or 700)
        return f"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <style>
    :root {{
      --retro-paper: #f3e3cc;
      --retro-paper-soft: #f6e8d3;
      --retro-ink: #0a4550;
      --retro-ink-soft: rgb(13 70 80 / 0.68);
      --retro-rust: #c24d24;
      --retro-line: rgb(13 70 80 / 0.82);
      --retro-stroke: 2px;
      --retro-recessed: rgb(73 48 22 / 0.055);
      --retro-shadow: rgb(73 48 22 / 0.14);
      --retro-paper-texture: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180' viewBox='0 0 180 180'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='1.05' numOctaves='4' seed='11'/%3E%3CfeColorMatrix type='matrix' values='0 0 0 0 .18 0 0 0 0 .11 0 0 0 0 .04 0 0 0 .22 0'/%3E%3C/filter%3E%3Crect width='180' height='180' filter='url(%23noise)'/%3E%3Cg opacity='.12' fill='%23c24d24'%3E%3Ccircle cx='12' cy='21' r='.7'/%3E%3Ccircle cx='67' cy='43' r='.55'/%3E%3Ccircle cx='139' cy='16' r='.65'/%3E%3Ccircle cx='158' cy='107' r='.6'/%3E%3Ccircle cx='41' cy='151' r='.55'/%3E%3C/g%3E%3C/svg%3E");
    }}
    * {{ box-sizing: border-box; }}
    html, body, #chart-card, #root {{ width: {width}px; height: {height}px; margin: 0; }}
    body {{
      background-color: var(--retro-paper);
      background-image: var(--retro-paper-texture);
      color: var(--retro-ink);
      font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
    }}
    .page {{
      width: 100%;
      height: 100%;
      padding: 24px;
      background-color: var(--retro-paper);
      background-image: var(--retro-paper-texture);
    }}
    .card {{
      position: relative;
      width: 100%;
      height: 100%;
      border: var(--retro-stroke) solid var(--retro-line);
      border-radius: 4px;
      background: var(--retro-recessed);
      box-shadow: 8px 8px 0 var(--retro-shadow);
      color: var(--retro-ink);
      overflow: visible;
      padding: 24px 26px 20px;
    }}
    .card::before {{
      content: "";
      position: absolute;
      top: 10px;
      right: 14px;
      width: 54px;
      height: 6px;
      background: repeating-linear-gradient(90deg, var(--retro-ink) 0 8px, transparent 8px 13px);
      opacity: 0.75;
    }}
    .card::after {{
      content: "";
      position: absolute;
      right: 0;
      bottom: 0;
      width: 0;
      height: 0;
      border-style: solid;
      border-width: 0 0 24px 24px;
      border-color: transparent transparent var(--retro-ink) transparent;
      opacity: 0.9;
    }}
    .header {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }}
    h1 {{
      margin: 0;
      font-size: 28px;
      line-height: 1.05;
      font-weight: 800;
      letter-spacing: 0;
      color: var(--retro-ink);
    }}
    p {{
      display: inline-block;
      margin: 10px 0 0;
      border-left: var(--retro-stroke) solid var(--retro-rust);
      padding-left: 10px;
      color: var(--retro-ink-soft);
      font-size: 14px;
      font-weight: 700;
    }}
    .chart-wrap {{ width: 100%; height: calc(100% - 58px); }}
    .summary-wrap {{
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      align-content: start;
      grid-auto-rows: max-content;
      gap: 24px;
      width: 100%;
      min-height: calc(100% - 58px);
      padding: 12px 6px 6px;
    }}
    .summary-section {{
      min-width: 0;
      min-height: 0;
      border-left: var(--retro-stroke) solid var(--retro-rust);
      padding: 4px 0 0 16px;
      overflow: visible;
    }}
    .summary-section h2 {{
      margin: 0 0 12px;
      color: var(--retro-rust);
      font-size: 17px;
      line-height: 1.2;
      font-weight: 800;
    }}
    .summary-rows {{
      display: grid;
      gap: 8px;
    }}
    .summary-row {{
      display: grid;
      grid-template-columns: minmax(220px, 0.22fr) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
      border-bottom: 1px dashed rgb(13 70 80 / 0.24);
      padding: 0 0 8px;
      color: var(--retro-ink-soft);
      font-size: 15px;
      font-weight: 700;
      line-height: 1.42;
    }}
    .summary-section-commands .summary-row {{
      grid-template-columns: minmax(650px, 0.58fr) minmax(0, 1fr);
    }}
    .summary-row span {{
      min-width: 0;
      color: var(--retro-ink);
      font-weight: 800;
      overflow-wrap: anywhere;
    }}
    .summary-row strong {{
      min-width: 0;
      color: var(--retro-ink);
      font-weight: 800;
      overflow-wrap: anywhere;
    }}
    .pie-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 24px;
      width: 100%;
      height: calc(100% - 58px);
      padding: 8px 18px 16px;
      overflow: visible;
    }}
    .pie-grid.pie-count-1 {{
      grid-template-columns: minmax(0, 1fr);
      place-items: stretch;
      padding: 0 26px 18px;
    }}
    .pie-panel {{
      min-width: 0;
      min-height: 0;
      width: 100%;
      height: 100%;
      border: 0;
      background: transparent;
      padding: 10px 12px 12px;
      display: flex;
      flex-direction: column;
      overflow: visible;
    }}
    .pie-panel h2 {{
      margin: 0 0 6px;
      color: var(--retro-rust);
      font-size: 14px;
      font-weight: 800;
      text-align: left;
    }}
    .pie-chart-wrap {{
      min-width: 0;
      min-height: 0;
      flex: 1;
      overflow: visible;
    }}
    .pie-chart-wrap .recharts-responsive-container,
    .pie-chart-wrap .recharts-wrapper,
    .pie-chart-wrap .recharts-surface {{
      overflow: visible !important;
    }}
    .chart-tooltip {{
      min-width: 150px;
      border: var(--retro-stroke) solid var(--retro-line);
      border-radius: 3px;
      background-color: var(--retro-paper-soft);
      background-image: var(--retro-paper-texture);
      box-shadow: 4px 4px 0 var(--retro-shadow);
      padding: 10px 12px;
      font-size: 12px;
      font-weight: 700;
    }}
    .tooltip-label {{ margin-bottom: 8px; font-weight: 800; color: var(--retro-ink); }}
    .tooltip-row {{ display: grid; grid-template-columns: 10px 1fr auto; align-items: center; gap: 8px; color: var(--retro-ink-soft); }}
    .tooltip-dot {{ width: 8px; height: 8px; border-radius: 1px; }}
    .tooltip-row strong {{ color: var(--retro-ink); font-weight: 800; }}
    .recharts-surface text {{
      font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", system-ui, sans-serif;
    }}
    .recharts-pie-label-text {{
      font-size: 21px;
      font-weight: 800;
    }}
  </style>
</head>
<body>
  <div id="chart-card"><div id="root"></div></div>
  <script>window.__MAIBOT_CHART_SPEC__ = {spec_json};</script>
  <script>{bundle}</script>
</body>
</html>
"""

    @staticmethod
    def _find_browser_executable() -> Optional[Path]:
        env_browser = os.environ.get("MAIBOT_CHART_BROWSER", "").strip()
        candidates = [
            Path(env_browser) if env_browser else None,
            Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
            Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
            Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        ]
        for candidate in candidates:
            if candidate is not None and candidate.exists():
                return candidate
        return None


async def render_webui_chart(plugin_dir: Path, spec: Dict[str, Any], output_path: Path) -> Optional[Path]:
    """便捷函数，避免调用方关心渲染器生命周期。"""

    await asyncio.sleep(0)
    return await WebUIChartRenderer(plugin_dir).render(spec, output_path)
