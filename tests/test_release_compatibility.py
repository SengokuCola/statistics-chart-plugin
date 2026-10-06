from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio
import pytest
import threading


def test_packaged_renderer_needs_no_dashboard(plugin_module, tmp_path):
    renderer_module = import_module("statistics_chart_release.webui_chart_renderer")
    # 模拟只解压插件的安装环境，不创建 dashboard 或 node_modules。
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "statistics_chart.bundle.js").write_text("window.__MAIBOT_CHART_READY__ = true;", encoding="utf-8")
    renderer = renderer_module.WebUIChartRenderer(tmp_path)
    assert "window.__MAIBOT_CHART_READY__ = true" in renderer._build_html({"title": "统计"})


def test_missing_bundle_reports_original_error(plugin_module, tmp_path):
    renderer_module = import_module("statistics_chart_release.webui_chart_renderer")
    with pytest.raises(RuntimeError, match="缺少预编译图表脚本"):
        asyncio.run(renderer_module.WebUIChartRenderer(tmp_path).render({}, tmp_path / "chart.png"))


def test_message_query_runs_outside_event_loop(plugin_module, monkeypatch):
    loop_thread = threading.get_ident()

    def fetch_message_trend(**kwargs):
        assert threading.get_ident() != loop_thread
        return plugin_module.TimeSeriesResult([], {}, {}, 0, 0)

    plugin = plugin_module.create_plugin()
    monkeypatch.setattr(plugin, "_get_service", lambda: SimpleNamespace(fetch_message_trend=fetch_message_trend))
    send_chart = AsyncMock(return_value=(True, "ok", True))
    monkeypatch.setattr(plugin, "_send_time_series_chart", send_chart)
    result = asyncio.run(plugin.handle_message_stats(stream_id="session-1"))
    assert result[0]
    send_chart.assert_awaited_once()
