"""统计绘图插件 SDK 入口。"""

from asyncio import to_thread
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image
from maibot_sdk import Command, MaiBotPlugin
from maibot_sdk.config import Field, PluginConfigBase

import base64
import logging
import re
import time

from .chart_specs import STATS_CHART_PALETTE, line_chart_spec, pie_grid_spec, simple_time_series_spec
from .data_service import BUCKET_ALIASES, PieChartResult, StatisticsDataService, TimeSeriesResult
from .webui_chart_renderer import render_webui_chart


logger = logging.getLogger(__name__)


HELP_TEXT = """统计绘图插件

只读取本机数据，不同步、不上传、不请求任何遥测接口。

可用命令：
/statschart 或 /统计绘图：显示摘要和命令帮助
/msgstats [天数] [hour|day] [topN]：绘制聊天流消息量趋势
/tokenstats [天数] [hour|day] [model|module|provider|type] [topN]：绘制 token 使用折线图
/tokenpie [天数] [model|module|provider|type] [topN]：绘制 token 使用分布饼图
/modelstats [天数] [hour|day] [token|request|cost|latency] [module=模块名] [topN]：绘制模型调用趋势
/toolstats [天数] [hour|day] [topN]：绘制工具调用趋势
/toolchat [天数] [topN] [tools=N] [min=N]：绘制工具调用在聊天流和工具名上的分布
/onlinestats [天数] [hour|day]：绘制在线时长趋势
/interactionstats [天数] [hour|day]：绘制交互消息趋势
/cachestats [天数] [hour|day]：绘制 Prompt Cache 命中趋势

所有绘图命令可追加 show_time 查看耗时拆分。"""


TOKEN_GROUP_ALIASES = {
    "model": "model",
    "models": "model",
    "模型": "model",
    "module": "module",
    "modules": "module",
    "模块": "module",
    "provider": "provider",
    "providers": "provider",
    "服务商": "provider",
    "type": "type",
    "types": "type",
    "request_type": "type",
    "请求类型": "type",
    "类型": "type",
}


class PluginSection(PluginConfigBase):
    """插件基本信息配置。"""

    __ui_label__ = "插件"
    __ui_order__ = 10

    name: str = Field(default="statistics_chart_plugin", title="插件名称")
    version: str = Field(default="0.1.3", title="插件版本")
    enabled: bool = Field(default=True, title="启用插件")
    config_version: str = Field(default="0.1.2", title="配置版本")


class DataSection(PluginConfigBase):
    """数据路径配置。"""

    __ui_label__ = "数据来源"
    __ui_order__ = 20

    db_path: str = Field(default="data/MaiBot.db", title="MaiBot 数据库路径")


class DrawSection(PluginConfigBase):
    """绘图输出配置。"""

    __ui_label__ = "绘图"
    __ui_order__ = 30

    pic_dir: str = Field(default="data/pic", title="图片保存目录")


class CacheSection(PluginConfigBase):
    """缓存配置。"""

    __ui_label__ = "缓存"
    __ui_order__ = 40

    summary_cache_seconds: int = Field(default=120, ge=0, le=3600, title="摘要图片缓存秒数")


class StatisticsChartPluginConfig(PluginConfigBase):
    """统计绘图插件配置模型。"""

    plugin: PluginSection = Field(default_factory=PluginSection, title="插件")
    data: DataSection = Field(default_factory=DataSection, title="数据来源")
    draw: DrawSection = Field(default_factory=DrawSection, title="绘图")
    cache: CacheSection = Field(default_factory=CacheSection, title="缓存")


class StatisticsChartPlugin(MaiBotPlugin):
    """只读本机数据的统计绘图插件。"""

    config_model = StatisticsChartPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._chart_render_seconds: Dict[Path, float] = {}
        self._chart_render_started_at: Dict[Path, float] = {}
        self._chart_render_finished_at: Dict[Path, float] = {}
        self._chart_command_started_at: Dict[Path, float] = {}
        self._chart_show_time_paths: set[Path] = set()
        self._summary_chart_cache: Dict[str, Tuple[float, Path]] = {}

    @property
    def plugin_dir(self) -> Path:
        """返回插件目录。"""

        return Path(__file__).resolve().parent

    @property
    def repo_dir(self) -> Path:
        """返回 MaiBot 仓库目录。"""

        return self.plugin_dir.parents[1]

    @property
    def pic_dir(self) -> Path:
        """返回绘图输出目录。"""

        relative_path = Path(self.config.draw.pic_dir)
        path = relative_path if relative_path.is_absolute() else self.plugin_dir / relative_path
        return path

    async def on_load(self) -> None:
        """处理插件加载。"""

        await to_thread(self.pic_dir.mkdir, parents=True, exist_ok=True)
        self.ctx.logger.info("统计绘图插件已加载")

    async def on_unload(self) -> None:
        """处理插件卸载。"""

        self.ctx.logger.info("统计绘图插件已卸载")

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """处理配置更新。"""

        del config_data
        self.ctx.logger.info(f"收到统计绘图插件配置更新: scope={scope}, version={version}")

    def _get_service(self) -> StatisticsDataService:
        """创建本机只读数据服务。"""

        return StatisticsDataService(
            repo_dir=self.repo_dir,
            db_path=self.config.data.db_path,
        )

    @staticmethod
    def _parse_args(matched_groups: Optional[Dict[str, Any]]) -> List[str]:
        """从命令捕获组中提取参数列表。"""

        if not matched_groups:
            return []
        raw_args = str(matched_groups.get("args") or "").strip()
        if not raw_args:
            return []
        return [arg for arg in raw_args.split() if arg]

    @staticmethod
    def _is_show_time_arg(arg: str) -> bool:
        """判断是否为显示耗时统计的内部参数。"""

        return arg.strip().lower() == "show_time"

    @classmethod
    def _effective_args(cls, matched_groups: Optional[Dict[str, Any]]) -> List[str]:
        """返回过滤掉内部调试参数后的命令参数。"""

        return [arg for arg in cls._parse_args(matched_groups) if not cls._is_show_time_arg(arg)]

    @classmethod
    def _should_show_time(cls, matched_groups: Optional[Dict[str, Any]]) -> bool:
        """只有命令显式带 show_time 时才输出耗时统计。"""

        return any(cls._is_show_time_arg(arg) for arg in cls._parse_args(matched_groups))

    @staticmethod
    def _parse_days(args: List[str], default: int) -> int:
        """解析天数参数。"""

        for arg in args:
            lowered = arg.lower().strip()
            for prefix in ("days=", "day=", "d=", "天数="):
                if lowered.startswith(prefix):
                    lowered = lowered.split("=", 1)[1].strip()
                    break
            if re.fullmatch(r"\d+(?:\.\d+)?d", lowered):
                return max(1, int(float(lowered[:-1])))
            if re.fullmatch(r"\d+(?:\.\d+)?w", lowered):
                return max(1, int(float(lowered[:-1]) * 7))
            if re.fullmatch(r"\d+", lowered):
                return max(1, int(lowered))
        return default

    @staticmethod
    def _parse_bucket(args: List[str], default: str) -> str:
        """解析时间粒度参数。"""

        for arg in args:
            lowered = arg.lower().strip()
            for prefix in ("bucket=", "粒度="):
                if lowered.startswith(prefix):
                    lowered = lowered.split("=", 1)[1].strip()
                    break
            if lowered in BUCKET_ALIASES:
                return StatisticsDataService.normalize_bucket(lowered)
        return default

    @staticmethod
    def _parse_top(args: List[str], default: int, *, prefix_names: Tuple[str, ...] = ("top", "rank")) -> int:
        """解析 topN/top=N/rankN 参数。"""

        for arg in args:
            lowered = arg.lower().strip()
            for prefix in prefix_names:
                if lowered.startswith(f"{prefix}="):
                    value = lowered.split("=", 1)[1].strip()
                    if value.isdigit():
                        return max(1, min(12, int(value)))
                match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", lowered)
                if match:
                    return max(1, min(12, int(match.group(1))))
        return default

    @staticmethod
    def _parse_named_int(args: List[str], name: str, default: int, *, upper: int = 24) -> int:
        """解析 name=N 形式的整数参数。"""

        for arg in args:
            lowered = arg.lower().strip()
            if lowered.startswith(f"{name.lower()}="):
                value = lowered.split("=", 1)[1].strip()
                if value.isdigit():
                    return max(1, min(upper, int(value)))
        return default

    @staticmethod
    def _parse_named_option(args: List[str], name: str) -> Optional[str]:
        """解析 name=value 形式的字符串参数。"""

        for arg in args:
            if arg.lower().startswith(f"{name.lower()}="):
                value = arg.split("=", 1)[1].strip()
                return value or None
        return None

    @staticmethod
    def _parse_model_metric(args: List[str]) -> str:
        """解析模型趋势统计口径。"""

        aliases = {
            "token": "token",
            "tokens": "token",
            "request": "request",
            "requests": "request",
            "count": "request",
            "cost": "cost",
            "latency": "latency",
            "time": "latency",
            "耗时": "latency",
            "延迟": "latency",
            "费用": "cost",
            "花费": "cost",
            "次数": "request",
        }
        for arg in args:
            lowered = arg.lower().strip()
            if lowered.startswith("metric="):
                lowered = lowered.split("=", 1)[1].strip()
            if lowered in aliases:
                return aliases[lowered]
        return "token"

    @staticmethod
    def _parse_module_name(args: List[str]) -> Optional[str]:
        """解析模型趋势模块过滤参数。"""

        named = StatisticsChartPlugin._parse_named_option(args, "module")
        if named:
            return named

        ignored_values = set(BUCKET_ALIASES) | {
            "token",
            "tokens",
            "request",
            "requests",
            "count",
            "cost",
            "latency",
            "time",
            "耗时",
            "延迟",
            "费用",
            "花费",
            "次数",
        }
        for arg in args:
            lowered = arg.lower().strip()
            if lowered in ignored_values:
                continue
            if re.fullmatch(r"\d+|\d+d|\d+w|top\d+|rank\d+", lowered):
                continue
            if any(lowered.startswith(prefix) for prefix in ("top=", "rank=", "days=", "day=", "d=", "bucket=", "粒度=")):
                continue
            return arg
        return None

    @staticmethod
    def _parse_token_group(args: List[str], default: Optional[str]) -> Optional[str]:
        """解析 token 统计的分组参数。"""

        named = StatisticsChartPlugin._parse_named_option(args, "group")
        if named:
            return TOKEN_GROUP_ALIASES.get(named.lower().strip(), default)

        for arg in args:
            lowered = arg.lower().strip()
            if lowered in TOKEN_GROUP_ALIASES:
                return TOKEN_GROUP_ALIASES[lowered]
        return default

    @staticmethod
    def _token_group_label(group_by: Optional[str]) -> str:
        """返回 token 分组展示名。"""

        labels = {
            None: "总量",
            "model": "模型",
            "module": "模块",
            "provider": "服务商",
            "type": "请求类型",
        }
        return labels.get(group_by, "模型")

    @staticmethod
    def _metric_label(metric: str) -> str:
        """返回模型统计口径展示名。"""

        labels = {
            "token": "总 token",
            "request": "请求次数",
            "cost": "费用",
            "latency": "平均耗时(秒)",
        }
        return labels.get(metric, "总 token")

    @staticmethod
    def _bucket_label(bucket: str) -> str:
        """返回时间粒度展示名。"""

        return "小时" if bucket == "hour" else "天"

    @staticmethod
    def _date_format(bucket: str) -> str:
        """返回图表时间格式。"""

        return "%m-%d %H:%M" if bucket == "hour" else "%m-%d"

    @staticmethod
    def _time_series_chart_data(result: TimeSeriesResult, *, date_format: str) -> List[Dict[str, Any]]:
        """把 TimeSeriesResult 转成组合图数据。"""

        data: List[Dict[str, Any]] = []
        for index, timestamp in enumerate(result.timestamps):
            item: Dict[str, Any] = {"label": timestamp.strftime(date_format)}
            for key, values in result.values_by_key.items():
                item[key] = values[index] if index < len(values) else 0
            data.append(item)
        return data

    async def _send_text(self, stream_id: str, text: str) -> None:
        """发送文本消息。"""

        await self.ctx.send.text(text, stream_id)

    async def _send_image(self, stream_id: str, image_path: Path) -> None:
        """发送图片消息。"""

        render_seconds = self._chart_render_seconds.pop(image_path, None)
        render_started_at = self._chart_render_started_at.pop(image_path, None)
        render_finished_at = self._chart_render_finished_at.pop(image_path, None)
        command_started_at = self._chart_command_started_at.pop(image_path, None)
        show_time = image_path in self._chart_show_time_paths
        self._chart_show_time_paths.discard(image_path)

        image_encode_started_at = time.perf_counter()
        image_base64, image_format, image_size = await to_thread(self._image_path_to_send_base64, image_path)
        image_encode_seconds = time.perf_counter() - image_encode_started_at
        image_send_started_at = time.perf_counter()
        await self.ctx.send.image(image_base64, stream_id)
        image_upload_seconds = time.perf_counter() - image_send_started_at
        if not show_time:
            return

        total_seconds = time.perf_counter() - command_started_at if command_started_at is not None else None
        duration_parts = ["耗时统计："]
        if total_seconds is not None:
            duration_parts.append(f"命令总耗时：{self._format_render_duration(total_seconds)}")
        if command_started_at is not None and render_started_at is not None:
            prepare_seconds = max(0, render_started_at - command_started_at)
            duration_parts.append(f"数据准备/统计计算：{self._format_render_duration(prepare_seconds)}")
        if render_seconds is not None:
            duration_parts.append(f"图表渲染：{self._format_render_duration(render_seconds)}")
        if render_finished_at is not None:
            before_image_send_seconds = max(0, image_encode_started_at - render_finished_at)
            duration_parts.append(f"发图前处理：{self._format_render_duration(before_image_send_seconds)}")
        duration_parts.append(
            f"图片编码：{self._format_render_duration(image_encode_seconds)} ({image_format}, {self._format_file_size(image_size)})"
        )
        duration_parts.append(f"图片发送等待：{self._format_render_duration(image_upload_seconds)}")
        await self._send_text(stream_id, "\n".join(duration_parts))

    @staticmethod
    def _image_path_to_send_base64(image_path: Path) -> Tuple[str, str, int]:
        """把图表转成适合聊天发送的 JPEG base64。"""

        with Image.open(image_path) as image:
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")

            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=90, optimize=True, subsampling=0)
            image_bytes = buffer.getvalue()

        return base64.b64encode(image_bytes).decode("utf-8"), "JPEG", len(image_bytes)

    @staticmethod
    def _format_file_size(size_bytes: int) -> str:
        """格式化图片发送体积。"""

        if size_bytes < 1024:
            return f"{size_bytes} B"
        if size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        return f"{size_bytes / 1024 / 1024:.2f} MB"

    @staticmethod
    def _format_render_duration(seconds: float) -> str:
        """格式化单张图表渲染耗时。"""

        if seconds < 1:
            return f"{seconds * 1000:.0f} ms"
        if seconds < 60:
            return f"{seconds:.2f} 秒"

        minutes = int(seconds // 60)
        remaining_seconds = seconds % 60
        return f"{minutes} 分 {remaining_seconds:.2f} 秒"

    def _record_chart_total_seconds(self, image_path: Path, started_at: float, *, show_time: bool = False) -> None:
        """记录绘图命令开始时间，用于发图后输出阶段耗时。"""

        if not show_time:
            return
        self._chart_command_started_at[image_path] = started_at
        self._chart_show_time_paths.add(image_path)

    async def _render_webui_chart(self, prefix: str, spec: Dict[str, Any]) -> Path:
        """使用 WebUI/Recharts 渲染图表。"""

        image_path = self.pic_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        started_at = time.perf_counter()
        rendered_path = await render_webui_chart(self.plugin_dir, spec, image_path)
        render_seconds = time.perf_counter() - started_at
        self._chart_render_seconds[rendered_path] = render_seconds
        self._chart_render_started_at[rendered_path] = started_at
        self._chart_render_finished_at[rendered_path] = started_at + render_seconds
        return rendered_path

    @staticmethod
    def _series_summary(result: TimeSeriesResult) -> str:
        """格式化时间序列摘要。"""

        if not result.timestamps:
            return "没有可用数据"
        labels = "、".join(result.labels_by_key.values())
        return (
            f"时间范围：{result.timestamps[0].strftime('%Y-%m-%d %H:%M')} 至 "
            f"{result.timestamps[-1].strftime('%Y-%m-%d %H:%M')}\n"
            f"数据点：{len(result.timestamps)}，序列：{labels}"
        )

    async def _send_time_series_chart(
        self,
        *,
        stream_id: str,
        command_started_at: float,
        matched_groups: Optional[Dict[str, Any]],
        result: TimeSeriesResult,
        prefix: str,
        title: str,
        description: str,
        date_format: str,
    ) -> Tuple[bool, str, bool]:
        """发送时间序列图表。"""

        if not result.timestamps or not result.values_by_key:
            error_msg = "没有可绘制的统计数据"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

        image_path = await self._render_webui_chart(
            prefix,
            simple_time_series_spec(
                title=title,
                description=description,
                timestamps=result.timestamps,
                values_by_key=result.values_by_key,
                labels_by_key=result.labels_by_key,
                width=1300,
                height=720,
                date_format=date_format,
            ),
        )
        await self._send_text(stream_id, f"{title}\n{self._series_summary(result)}")
        self._record_chart_total_seconds(image_path, command_started_at, show_time=self._should_show_time(matched_groups))
        await self._send_image(stream_id, image_path)
        return True, f"已生成{title}", True

    async def _send_custom_time_series_chart(
        self,
        *,
        stream_id: str,
        command_started_at: float,
        matched_groups: Optional[Dict[str, Any]],
        result: TimeSeriesResult,
        prefix: str,
        title: str,
        description: str,
        date_format: str,
        series: List[Dict[str, Any]],
    ) -> Tuple[bool, str, bool]:
        """发送自定义 series 的时间序列图表。"""

        if not result.timestamps or not result.values_by_key:
            error_msg = "没有可绘制的统计数据"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

        image_path = await self._render_webui_chart(
            prefix,
            line_chart_spec(
                title=title,
                description=description,
                data=self._time_series_chart_data(result, date_format=date_format),
                series=series,
                width=1300,
                height=720,
                x_tick_angle=-35 if len(result.timestamps) > 10 else 0,
            ),
        )
        await self._send_text(stream_id, f"{title}\n{self._series_summary(result)}")
        self._record_chart_total_seconds(image_path, command_started_at, show_time=self._should_show_time(matched_groups))
        await self._send_image(stream_id, image_path)
        return True, f"已生成{title}", True

    def _build_summary_card_spec(self, *, days: int, summary_text: str) -> Dict[str, Any]:
        """构建统计摘要卡片规格。"""

        summary_rows: List[Dict[str, str]] = []
        for line in summary_text.splitlines()[1:]:
            if "：" in line:
                label, value = line.split("：", 1)
                summary_rows.append({"label": label, "value": value})
            elif line.strip():
                summary_rows.append({"label": line.strip(), "value": ""})

        command_rows = [
            {"label": "/msgstats [天数] [hour|day] [topN]", "value": "聊天流消息量趋势"},
            {
                "label": "/tokenstats [天数] [hour|day] [model|module|provider|type] [topN]",
                "value": "token 使用折线图",
            },
            {
                "label": "/tokenpie [天数] [model|module|provider|type] [topN]",
                "value": "token 使用分布饼图",
            },
            {
                "label": "/modelstats [天数] [hour|day] [token|request|cost|latency] [module=模块名] [topN]",
                "value": "模型调用趋势",
            },
            {"label": "/toolstats [天数] [hour|day] [topN]", "value": "工具调用趋势"},
            {"label": "/toolchat [天数] [topN] [tools=N] [min=N]", "value": "工具调用分布"},
            {"label": "/onlinestats [天数] [hour|day]", "value": "在线时长趋势"},
            {"label": "/interactionstats [天数] [hour|day]", "value": "交互消息趋势"},
            {"label": "/cachestats [天数] [hour|day]", "value": "Prompt Cache 命中趋势"},
        ]
        parameter_rows = [
            {"label": "基础用法", "value": "/statschart [天数] [show_time]"},
            {"label": "天数写法", "value": "可写 7、days=7、7d、1w；不填默认 7 天"},
            {"label": "耗时调试", "value": "追加 show_time 后，会在发图后输出生成与发送耗时"},
            {"label": "示例", "value": "/statschart 7；/statschart days=30 show_time"},
        ]
        return {
            "kind": "summary-card",
            "title": "统计绘图摘要",
            "description": f"最近 {days} 天，只读取本机数据",
            "width": 1400,
            "height": 1250,
            "sections": [
                {"title": "核心统计", "rows": summary_rows},
                {"title": "参数说明", "rows": parameter_rows},
                {"title": "可用命令", "rows": command_rows, "layout": "commands"},
            ],
        }

    async def _render_statistics_summary_chart(self, *, days: int) -> Path:
        """渲染或复用统计摘要图片。"""

        cache_key = f"summary:{days}"
        cache_seconds = int(self.config.cache.summary_cache_seconds)
        cached_item = self._summary_chart_cache.get(cache_key)
        now = time.time()
        if cache_seconds > 0 and cached_item is not None:
            cached_at, cached_path = cached_item
            if now - cached_at <= cache_seconds and cached_path.exists():
                return cached_path

        summary_text = await to_thread(self._get_service().build_summary, days=days)
        image_path = await self._render_webui_chart(
            "statistics_summary",
            self._build_summary_card_spec(days=days, summary_text=summary_text),
        )
        if cache_seconds > 0:
            self._summary_chart_cache[cache_key] = (now, image_path)
        return image_path

    @Command(
        "statistics_chart",
        description="显示统计绘图插件帮助和摘要",
        pattern=r"^(?:/|#)?(?:statschart|chartstats|统计绘图|绘图统计)(?:\s+(?P<args>.+))?$",
    )
    async def handle_statistics_chart(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理统计绘图帮助命令。"""

        del kwargs
        if not self.config.plugin.enabled:
            return False, "统计绘图插件已禁用", True

        command_started_at = time.perf_counter()
        args = self._effective_args(matched_groups)
        days = self._parse_days(args, 7)
        try:
            image_path = await self._render_statistics_summary_chart(days=days)
            self._record_chart_total_seconds(image_path, command_started_at, show_time=self._should_show_time(matched_groups))
            await self._send_image(stream_id, image_path)
            return True, "已发送统计绘图摘要", True
        except Exception as exc:
            logger.error("生成统计摘要失败", exc_info=True)
            await self._send_text(stream_id, f"生成统计摘要图片失败：{exc}")
            return False, str(exc), True

    @Command(
        "message_stats",
        description="绘制聊天流消息量趋势",
        pattern=r"^(?:/|#)?(?:msgstats|messagestats|message_stats|消息统计)(?:\s+(?P<args>.+))?$",
    )
    async def handle_message_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理消息统计命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            bucket = self._parse_bucket(args, "day")
            top_chats = self._parse_top(args, 6)
            result = await to_thread(self._get_service().fetch_message_trend, days=days, bucket=bucket, top_chats=top_chats)
            return await self._send_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="message_stats",
                title="消息量趋势",
                description=f"最近 {days} 天，按{self._bucket_label(bucket)}，前 {top_chats} 个聊天流",
                date_format=self._date_format(bucket),
            )
        except Exception as exc:
            logger.error("处理消息统计命令失败", exc_info=True)
            error_msg = f"生成消息量趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "token_stats",
        description="绘制 token 使用折线图",
        pattern=r"^(?:/|#)?(?:tokenstats|token_stats|token统计|token趋势)(?:\s+(?P<args>.+))?$",
    )
    async def handle_token_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理 token 使用趋势命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            bucket = self._parse_bucket(args, "day")
            top_items = self._parse_top(args, 6)
            group_by = self._parse_token_group(args, None)
            result = await to_thread(
                self._get_service().fetch_token_trend,
                days=days,
                bucket=bucket,
                group_by=group_by,
                top_items=top_items,
            )
            if group_by is not None:
                return await self._send_time_series_chart(
                    stream_id=stream_id,
                    command_started_at=command_started_at,
                    matched_groups=matched_groups,
                    result=result,
                    prefix="token_stats",
                    title="Token 使用趋势",
                    description=(
                        f"最近 {days} 天，按{self._bucket_label(bucket)}，"
                        f"按{self._token_group_label(group_by)}分组，前 {top_items} 项"
                    ),
                    date_format=self._date_format(bucket),
                )

            return await self._send_custom_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="token_stats",
                title="Token 使用趋势",
                description=f"最近 {days} 天，按{self._bucket_label(bucket)}",
                date_format=self._date_format(bucket),
                series=[
                    {"key": "total_tokens", "label": "总 token", "type": "line", "color": STATS_CHART_PALETTE[0]},
                    {"key": "prompt_tokens", "label": "输入 token", "type": "line", "color": STATS_CHART_PALETTE[1]},
                    {"key": "completion_tokens", "label": "输出 token", "type": "line", "color": STATS_CHART_PALETTE[2]},
                    {
                        "key": "request_count",
                        "label": "请求次数",
                        "type": "bar",
                        "color": STATS_CHART_PALETTE[3],
                        "yAxisId": "right",
                        "opacity": 0.5,
                    },
                ],
            )
        except Exception as exc:
            logger.error("处理 token 使用趋势命令失败", exc_info=True)
            error_msg = f"生成 token 使用趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "token_distribution",
        description="绘制 token 使用分布饼图",
        pattern=r"^(?:/|#)?(?:tokenpie|tokendist|token_distribution|token分布|token饼图)(?:\s+(?P<args>.+))?$",
    )
    async def handle_token_distribution(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理 token 使用分布命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            top_items = self._parse_top(args, 10)
            group_by = self._parse_token_group(args, "model") or "model"
            result = await to_thread(self._get_service().fetch_token_distribution, days=days, group_by=group_by, top_items=top_items)
            return await self._send_pie_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="token_distribution",
                title="Token 使用分布",
                description=f"最近 {days} 天，按{self._token_group_label(group_by)}分组，前 {top_items} 项",
                total_label="总 token",
            )
        except Exception as exc:
            logger.error("处理 token 使用分布命令失败", exc_info=True)
            error_msg = f"生成 token 使用分布图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "model_stats",
        description="绘制模型调用趋势",
        pattern=r"^(?:/|#)?(?:modelstats|model_stats|模型统计)(?:\s+(?P<args>.+))?$",
    )
    async def handle_model_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理模型统计命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            bucket = self._parse_bucket(args, "day")
            top_models = self._parse_top(args, 6)
            metric = self._parse_model_metric(args)
            module_name = self._parse_module_name(args)
            result = await to_thread(
                self._get_service().fetch_model_trend,
                days=days,
                bucket=bucket,
                top_models=top_models,
                metric=metric,
                module_name=module_name,
            )
            module_label = f"，模块 {module_name}" if module_name else ""
            return await self._send_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="model_stats",
                title="模型调用趋势",
                description=(
                    f"最近 {days} 天，按{self._bucket_label(bucket)}，{self._metric_label(metric)}"
                    f"{module_label}，前 {top_models} 个模型"
                ),
                date_format=self._date_format(bucket),
            )
        except Exception as exc:
            logger.error("处理模型统计命令失败", exc_info=True)
            error_msg = f"生成模型调用趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "tool_stats",
        description="绘制工具调用趋势",
        pattern=r"^(?:/|#)?(?:toolstats|tool_stats|工具统计)(?:\s+(?P<args>.+))?$",
    )
    async def handle_tool_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理工具统计命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            bucket = self._parse_bucket(args, "day")
            top_tools = self._parse_top(args, 6)
            result = await to_thread(self._get_service().fetch_tool_trend, days=days, bucket=bucket, top_tools=top_tools)
            return await self._send_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="tool_stats",
                title="工具调用趋势",
                description=f"最近 {days} 天，按{self._bucket_label(bucket)}，前 {top_tools} 个工具",
                date_format=self._date_format(bucket),
            )
        except Exception as exc:
            logger.error("处理工具统计命令失败", exc_info=True)
            error_msg = f"生成工具调用趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "tool_chat_distribution",
        description="绘制工具调用在聊天流和工具名上的分布",
        pattern=r"^(?:/|#)?(?:toolchat|toolchatstats|tool_chat_stats|工具分布|工具聊天统计)(?:\s+(?P<args>.+))?$",
    )
    async def handle_tool_chat_distribution(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理工具聊天分布命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            top_chats = self._parse_top(args, 8)
            top_tools = self._parse_named_int(args, "tools", 10, upper=16)
            min_chat_total = self._parse_named_int(args, "min", 1, upper=100000)
            result = await to_thread(
                self._get_service().fetch_tool_chat_distribution,
                days=days,
                top_chats=top_chats,
                top_tools=top_tools,
                min_chat_total=min_chat_total,
            )
            return await self._send_pie_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="tool_chat_distribution",
                title="工具调用分布",
                description=f"最近 {days} 天，聊天流前 {top_chats}，工具前 {top_tools}",
                total_label="总工具调用",
            )
        except Exception as exc:
            logger.error("处理工具分布命令失败", exc_info=True)
            error_msg = f"生成工具分布图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "online_stats",
        description="绘制在线时长趋势",
        pattern=r"^(?:/|#)?(?:onlinestats|online_stats|在线统计|在线时长)(?:\s+(?P<args>.+))?$",
    )
    async def handle_online_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理在线时长统计命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 30)
            bucket = self._parse_bucket(args, "day")
            result = await to_thread(self._get_service().fetch_online_time_trend, days=days, bucket=bucket)
            return await self._send_custom_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="online_stats",
                title="在线时长趋势",
                description=f"最近 {days} 天，按{self._bucket_label(bucket)}",
                date_format=self._date_format(bucket),
                series=[
                    {
                        "key": "online_hours",
                        "label": "在线时长(小时)",
                        "type": "bar",
                        "color": STATS_CHART_PALETTE[0],
                        "opacity": 0.72,
                    },
                    {
                        "key": "record_count",
                        "label": "记录数",
                        "type": "line",
                        "color": STATS_CHART_PALETTE[1],
                        "yAxisId": "right",
                    },
                ],
            )
        except Exception as exc:
            logger.error("处理在线时长统计命令失败", exc_info=True)
            error_msg = f"生成在线时长趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "interaction_stats",
        description="绘制交互消息趋势",
        pattern=r"^(?:/|#)?(?:interactionstats|interaction_stats|交互统计|互动统计|交互消息)(?:\s+(?P<args>.+))?$",
    )
    async def handle_interaction_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理交互消息统计命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            bucket = self._parse_bucket(args, "day")
            result = await to_thread(self._get_service().fetch_interaction_trend, days=days, bucket=bucket)
            return await self._send_custom_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="interaction_stats",
                title="交互消息趋势",
                description=f"最近 {days} 天，按{self._bucket_label(bucket)}",
                date_format=self._date_format(bucket),
                series=[
                    {
                        "key": "total_messages",
                        "label": "全部消息",
                        "type": "bar",
                        "color": STATS_CHART_PALETTE[0],
                        "opacity": 0.5,
                    },
                    {"key": "direct_interactions", "label": "直接互动", "type": "line", "color": STATS_CHART_PALETTE[1]},
                    {"key": "mentioned_messages", "label": "提及麦麦", "type": "line", "color": STATS_CHART_PALETTE[2]},
                    {"key": "reply_messages", "label": "回复消息", "type": "line", "color": STATS_CHART_PALETTE[3]},
                    {"key": "picture_messages", "label": "图片消息", "type": "line", "color": STATS_CHART_PALETTE[4]},
                    {"key": "emoji_messages", "label": "表情消息", "type": "line", "color": STATS_CHART_PALETTE[5]},
                    {"key": "command_messages", "label": "命令消息", "type": "line", "color": STATS_CHART_PALETTE[6]},
                ],
            )
        except Exception as exc:
            logger.error("处理交互消息统计命令失败", exc_info=True)
            error_msg = f"生成交互消息趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    @Command(
        "cache_stats",
        description="绘制 Prompt Cache 命中趋势",
        pattern=r"^(?:/|#)?(?:cachestats|cache_stats|缓存统计|缓存命中)(?:\s+(?P<args>.+))?$",
    )
    async def handle_cache_stats(
        self,
        stream_id: str = "",
        matched_groups: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        """处理 Prompt Cache 命中统计命令。"""

        del kwargs
        command_started_at = time.perf_counter()
        try:
            args = self._effective_args(matched_groups)
            days = self._parse_days(args, 7)
            bucket = self._parse_bucket(args, "day")
            result = await to_thread(self._get_service().fetch_cache_trend, days=days, bucket=bucket)
            return await self._send_custom_time_series_chart(
                stream_id=stream_id,
                command_started_at=command_started_at,
                matched_groups=matched_groups,
                result=result,
                prefix="cache_stats",
                title="Prompt Cache 命中趋势",
                description=f"最近 {days} 天，按{self._bucket_label(bucket)}",
                date_format=self._date_format(bucket),
                series=[
                    {"key": "hit_tokens", "label": "命中 token", "type": "line", "color": STATS_CHART_PALETTE[0]},
                    {"key": "miss_tokens", "label": "未命中 token", "type": "line", "color": STATS_CHART_PALETTE[1]},
                    {
                        "key": "hit_rate",
                        "label": "命中率(%)",
                        "type": "line",
                        "color": STATS_CHART_PALETTE[2],
                        "yAxisId": "right",
                    },
                    {
                        "key": "request_count",
                        "label": "请求数",
                        "type": "bar",
                        "color": STATS_CHART_PALETTE[3],
                        "yAxisId": "right",
                        "opacity": 0.36,
                    },
                    {
                        "key": "cache_enabled_requests",
                        "label": "启用缓存请求数",
                        "type": "line",
                        "color": STATS_CHART_PALETTE[4],
                        "yAxisId": "right",
                    },
                ],
            )
        except Exception as exc:
            logger.error("处理 Prompt Cache 命中统计命令失败", exc_info=True)
            error_msg = f"生成 Prompt Cache 命中趋势图失败：{exc}"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

    async def _send_pie_chart(
        self,
        *,
        stream_id: str,
        command_started_at: float,
        matched_groups: Optional[Dict[str, Any]],
        result: PieChartResult,
        prefix: str,
        title: str,
        description: str,
        total_label: str,
    ) -> Tuple[bool, str, bool]:
        """发送饼图。"""

        if not result.pies or not any(pie.get("data") for pie in result.pies):
            error_msg = "没有可绘制的分布数据"
            await self._send_text(stream_id, error_msg)
            return False, error_msg, True

        image_path = await self._render_webui_chart(
            prefix,
            pie_grid_spec(
                title=title,
                description=description,
                pies=result.pies,
                width=1500,
                height=900,
            ),
        )
        await self._send_text(stream_id, f"{title}\n{total_label}：{result.total}，聚合项：{result.source_count}")
        self._record_chart_total_seconds(image_path, command_started_at, show_time=self._should_show_time(matched_groups))
        await self._send_image(stream_id, image_path)
        return True, f"已生成{title}", True

def create_plugin() -> StatisticsChartPlugin:
    """创建插件实例。"""

    return StatisticsChartPlugin()
