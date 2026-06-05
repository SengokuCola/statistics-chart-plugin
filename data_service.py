"""只读本机统计数据服务。"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Counter as CounterType, DefaultDict, Dict, Iterable, List, Optional, Sequence, Tuple

import json
import re
import sqlite3


BUCKET_ALIASES = {
    "h": "hour",
    "hour": "hour",
    "hours": "hour",
    "1h": "hour",
    "小时": "hour",
    "按小时": "hour",
    "d": "day",
    "day": "day",
    "days": "day",
    "1d": "day",
    "天": "day",
    "按天": "day",
}

TOKEN_GROUP_COLUMNS = {
    "model": ("model_name", "模型"),
    "module": ("module_name", "模块"),
    "provider": ("provider_name", "服务商"),
    "type": ("request_type", "请求类型"),
}


@dataclass(frozen=True)
class TimeSeriesResult:
    """统计时间序列结果。"""

    timestamps: List[datetime]
    values_by_key: Dict[str, List[float]]
    labels_by_key: Dict[str, str]
    total: float
    source_count: int


@dataclass(frozen=True)
class CategoryChartResult:
    """分类统计结果。"""

    data: List[Dict[str, Any]]
    total: float
    source_count: int


@dataclass(frozen=True)
class PieChartResult:
    """饼图统计结果。"""

    pies: List[Dict[str, Any]]
    total: int
    source_count: int


class StatisticsDataError(RuntimeError):
    """本机数据读取失败。"""


class StatisticsDataService:
    """从本机 SQLite 和可选日志文件中读取统计数据，不访问任何远端服务。"""

    def __init__(
        self,
        *,
        repo_dir: Path,
        db_path: str,
        logs_dir: str = "logs",
        max_log_files: int = 80,
        max_prompt_files: int = 8000,
        max_memory_points: int = 320,
    ) -> None:
        self.repo_dir = repo_dir
        self.db_path = self._resolve_repo_path(db_path)
        self.logs_dir = self._resolve_repo_path(logs_dir)
        self.max_log_files = max(1, max_log_files)
        self.max_prompt_files = max(1, max_prompt_files)
        self.max_memory_points = max(10, max_memory_points)

    def _resolve_repo_path(self, path_text: str) -> Path:
        path = Path(path_text)
        return path if path.is_absolute() else self.repo_dir / path

    def _connect_readonly(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise StatisticsDataError(f"数据库文件不存在: {self.db_path}")

        database_uri = f"file:{self.db_path.as_posix()}?mode=ro&cache=shared"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _table_exists(self, table_name: str) -> bool:
        with self._connect_readonly() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table_name,),
            ).fetchone()
        return row is not None

    @staticmethod
    def normalize_bucket(bucket: str) -> str:
        """规范化时间粒度。"""

        return BUCKET_ALIASES.get(bucket.strip().lower(), "day")

    @staticmethod
    def bucket_label_sql(column_name: str, bucket: str) -> str:
        """返回 SQLite 时间桶表达式。"""

        if bucket == "hour":
            return f"strftime('%Y-%m-%d %H:00:00', {column_name})"
        return f"strftime('%Y-%m-%d 00:00:00', {column_name})"

    @staticmethod
    def parse_datetime(value: Any) -> Optional[datetime]:
        """解析数据库和日志中常见的时间格式。"""

        if value is None:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(float(value))
            except (OSError, ValueError):
                return None

        text = str(value).strip()
        if not text:
            return None
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _start_time(days: int) -> datetime:
        return datetime.now() - timedelta(days=max(1, days))

    @staticmethod
    def _label_key(label: str, used_keys: set[str]) -> str:
        base_key = re.sub(r"\s+", " ", label.strip()) or "未命名"
        key = base_key
        index = 2
        while key in used_keys:
            key = f"{base_key} #{index}"
            index += 1
        used_keys.add(key)
        return key

    @staticmethod
    def _format_chat_name(row: sqlite3.Row, name_mapping: Dict[str, str]) -> str:
        group_id = str(row["group_id"] or "")
        user_id = str(row["user_id"] or "")
        session_id = str(row["session_id"] or "")
        group_name = str(row["group_name"] or "").strip()
        user_cardname = str(row["user_cardname"] or "").strip()
        user_nickname = str(row["user_nickname"] or "").strip()

        if group_name:
            return group_name
        if group_id and name_mapping.get(f"g{group_id}"):
            return name_mapping[f"g{group_id}"]
        if user_cardname:
            return f"{user_cardname} 的私聊"
        if user_nickname:
            return f"{user_nickname} 的私聊"
        if user_id:
            return f"{user_id} 的私聊"
        return session_id or "未知聊天"

    def _load_chat_name_mapping(self, connection: sqlite3.Connection) -> Dict[str, str]:
        if not self._table_exists("statistics_message_hourly"):
            return {}

        rows = connection.execute(
            """
            SELECT chat_id, chat_name, MAX(latest_timestamp) AS latest_timestamp
            FROM statistics_message_hourly
            WHERE COALESCE(chat_id, '') != '' AND COALESCE(chat_name, '') != ''
            GROUP BY chat_id
            """
        ).fetchall()
        return {str(row["chat_id"]): str(row["chat_name"]) for row in rows}

    def fetch_message_trend(self, *, days: int, bucket: str, top_chats: int) -> TimeSeriesResult:
        """读取各聊天流消息量趋势。"""

        if not self._table_exists("statistics_message_hourly"):
            raise StatisticsDataError("缺少 statistics_message_hourly 表，无法绘制消息趋势")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time = self._start_time(days)
        bucket_sql = self.bucket_label_sql("bucket_time", normalized_bucket)
        with self._connect_readonly() as connection:
            top_rows = connection.execute(
                """
                SELECT COALESCE(NULLIF(chat_name, ''), chat_id) AS chat_label,
                       SUM(message_count) AS total_count
                FROM statistics_message_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF(chat_name, ''), chat_id)
                ORDER BY total_count DESC, chat_label ASC
                LIMIT ?
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"), max(1, top_chats)),
            ).fetchall()
            labels = [str(row["chat_label"]) for row in top_rows]
            if not labels:
                return TimeSeriesResult([], {}, {}, 0, 0)

            placeholders = ",".join("?" for _ in labels)
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       COALESCE(NULLIF(chat_name, ''), chat_id) AS chat_label,
                       SUM(message_count) AS total_count
                FROM statistics_message_hourly
                WHERE bucket_time >= ?
                  AND COALESCE(NULLIF(chat_name, ''), chat_id) IN ({placeholders})
                GROUP BY bucket_label, chat_label
                ORDER BY bucket_label ASC
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"), *labels),
            ).fetchall()

        return self._build_time_series(
            rows=rows,
            label_column="chat_label",
            value_column="total_count",
            ordered_labels=labels,
        )

    def fetch_model_trend(
        self,
        *,
        days: int,
        bucket: str,
        top_models: int,
        metric: str,
        module_name: Optional[str],
    ) -> TimeSeriesResult:
        """读取模型调用趋势。"""

        if not self._table_exists("statistics_model_hourly"):
            raise StatisticsDataError("缺少 statistics_model_hourly 表，无法绘制模型趋势")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time = self._start_time(days)
        metric_sql, value_column, top_metric_sql = self._model_metric_sql(metric)
        where_sql = "WHERE bucket_time >= ?"
        params: List[Any] = [start_time.strftime("%Y-%m-%d %H:%M:%S")]
        if module_name:
            where_sql += " AND module_name = ?"
            params.append(module_name)

        with self._connect_readonly() as connection:
            top_rows = connection.execute(
                f"""
                SELECT COALESCE(NULLIF(model_name, ''), 'Unknown') AS model_label,
                       {top_metric_sql} AS total_value
                FROM statistics_model_hourly
                {where_sql}
                GROUP BY COALESCE(NULLIF(model_name, ''), 'Unknown')
                ORDER BY total_value DESC, model_label ASC
                LIMIT ?
                """,
                (*params, max(1, top_models)),
            ).fetchall()
            labels = [str(row["model_label"]) for row in top_rows]
            if not labels:
                return TimeSeriesResult([], {}, {}, 0, 0)

            bucket_sql = self.bucket_label_sql("bucket_time", normalized_bucket)
            placeholders = ",".join("?" for _ in labels)
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       COALESCE(NULLIF(model_name, ''), 'Unknown') AS model_label,
                       {metric_sql} AS {value_column}
                FROM statistics_model_hourly
                {where_sql}
                  AND COALESCE(NULLIF(model_name, ''), 'Unknown') IN ({placeholders})
                GROUP BY bucket_label, model_label
                ORDER BY bucket_label ASC
                """,
                (*params, *labels),
            ).fetchall()

        return self._build_time_series(
            rows=rows,
            label_column="model_label",
            value_column=value_column,
            ordered_labels=labels,
        )

    def fetch_token_trend(
        self,
        *,
        days: int,
        bucket: str,
        group_by: Optional[str],
        top_items: int,
    ) -> TimeSeriesResult:
        """读取 token 使用趋势。"""

        if not self._table_exists("statistics_model_hourly"):
            raise StatisticsDataError("缺少 statistics_model_hourly 表，无法绘制 token 趋势")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time_text = self._start_time(days).strftime("%Y-%m-%d %H:%M:%S")
        bucket_sql = self.bucket_label_sql("bucket_time", normalized_bucket)
        if group_by is None:
            with self._connect_readonly() as connection:
                rows = connection.execute(
                    f"""
                    SELECT {bucket_sql} AS bucket_label,
                           SUM(prompt_tokens) AS prompt_tokens,
                           SUM(completion_tokens) AS completion_tokens,
                           SUM(total_tokens) AS total_tokens,
                           SUM(request_count) AS request_count
                    FROM statistics_model_hourly
                    WHERE bucket_time >= ?
                    GROUP BY bucket_label
                    ORDER BY bucket_label ASC
                    """,
                    (start_time_text,),
                ).fetchall()

            timestamps: List[datetime] = []
            values_by_key: Dict[str, List[float]] = {
                "total_tokens": [],
                "prompt_tokens": [],
                "completion_tokens": [],
                "request_count": [],
            }
            for row in rows:
                bucket_time = self.parse_datetime(row["bucket_label"])
                if bucket_time is None:
                    continue
                timestamps.append(bucket_time)
                for key in values_by_key:
                    values_by_key[key].append(float(row[key] or 0))
            return TimeSeriesResult(
                timestamps=timestamps,
                values_by_key=values_by_key,
                labels_by_key={
                    "total_tokens": "总 token",
                    "prompt_tokens": "输入 token",
                    "completion_tokens": "输出 token",
                    "request_count": "请求次数",
                },
                total=sum(values_by_key["total_tokens"]),
                source_count=len(rows),
            )

        group_column, _group_label = self._token_group_column(group_by)
        with self._connect_readonly() as connection:
            top_rows = connection.execute(
                f"""
                SELECT COALESCE(NULLIF({group_column}, ''), 'Unknown') AS group_label,
                       SUM(total_tokens) AS total_tokens
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF({group_column}, ''), 'Unknown')
                ORDER BY total_tokens DESC, group_label ASC
                LIMIT ?
                """,
                (start_time_text, max(1, top_items)),
            ).fetchall()
            labels = [str(row["group_label"]) for row in top_rows]
            if not labels:
                return TimeSeriesResult([], {}, {}, 0, 0)

            placeholders = ",".join("?" for _ in labels)
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       COALESCE(NULLIF({group_column}, ''), 'Unknown') AS group_label,
                       SUM(total_tokens) AS total_tokens
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                  AND COALESCE(NULLIF({group_column}, ''), 'Unknown') IN ({placeholders})
                GROUP BY bucket_label, group_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text, *labels),
            ).fetchall()

        return self._build_time_series(
            rows=rows,
            label_column="group_label",
            value_column="total_tokens",
            ordered_labels=labels,
        )

    def fetch_token_distribution(self, *, days: int, group_by: str, top_items: int) -> PieChartResult:
        """读取 token 使用分布。"""

        if not self._table_exists("statistics_model_hourly"):
            raise StatisticsDataError("缺少 statistics_model_hourly 表，无法绘制 token 分布")

        group_column, group_label_name = self._token_group_column(group_by)
        start_time_text = self._start_time(days).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect_readonly() as connection:
            rows = connection.execute(
                f"""
                SELECT COALESCE(NULLIF({group_column}, ''), 'Unknown') AS group_label,
                       SUM(total_tokens) AS total_tokens,
                       SUM(request_count) AS request_count
                FROM statistics_model_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF({group_column}, ''), 'Unknown')
                ORDER BY total_tokens DESC, group_label ASC
                """,
                (start_time_text,),
            ).fetchall()

        token_counts = Counter({str(row["group_label"]): int(row["total_tokens"] or 0) for row in rows})
        request_counts = Counter({str(row["group_label"]): int(row["request_count"] or 0) for row in rows})
        pies = [
            {"title": f"Token 按{group_label_name}分布", "data": self._pie_items_from_counter(token_counts, limit=top_items)},
            {"title": f"请求次数按{group_label_name}分布", "data": self._pie_items_from_counter(request_counts, limit=top_items)},
        ]
        return PieChartResult(pies=pies, total=sum(token_counts.values()), source_count=len(rows))

    @staticmethod
    def _model_metric_sql(metric: str) -> Tuple[str, str, str]:
        normalized = metric.lower().strip()
        if normalized in {"request", "requests", "count", "次数"}:
            return "SUM(request_count)", "metric_value", "SUM(request_count)"
        if normalized in {"cost", "费用", "花费"}:
            return "SUM(cost)", "metric_value", "SUM(cost)"
        if normalized in {"latency", "time", "耗时", "延迟"}:
            expression = "SUM(time_cost_sum) / NULLIF(SUM(request_count), 0)"
            return expression, "metric_value", "SUM(request_count)"
        return "SUM(total_tokens)", "metric_value", "SUM(total_tokens)"

    @staticmethod
    def _token_group_column(group_by: str) -> Tuple[str, str]:
        group_data = TOKEN_GROUP_COLUMNS.get(group_by.lower().strip())
        if group_data is None:
            return TOKEN_GROUP_COLUMNS["model"]
        return group_data

    def fetch_tool_trend(self, *, days: int, bucket: str, top_tools: int) -> TimeSeriesResult:
        """读取工具调用趋势。"""

        if not self._table_exists("statistics_tool_hourly"):
            raise StatisticsDataError("缺少 statistics_tool_hourly 表，无法绘制工具趋势")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time = self._start_time(days)
        bucket_sql = self.bucket_label_sql("bucket_time", normalized_bucket)
        with self._connect_readonly() as connection:
            top_rows = connection.execute(
                """
                SELECT COALESCE(NULLIF(tool_name, ''), 'Unknown') AS tool_label,
                       SUM(call_count) AS total_count
                FROM statistics_tool_hourly
                WHERE bucket_time >= ?
                GROUP BY COALESCE(NULLIF(tool_name, ''), 'Unknown')
                ORDER BY total_count DESC, tool_label ASC
                LIMIT ?
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"), max(1, top_tools)),
            ).fetchall()
            labels = [str(row["tool_label"]) for row in top_rows]
            if not labels:
                return TimeSeriesResult([], {}, {}, 0, 0)

            placeholders = ",".join("?" for _ in labels)
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       COALESCE(NULLIF(tool_name, ''), 'Unknown') AS tool_label,
                       SUM(call_count) AS total_count
                FROM statistics_tool_hourly
                WHERE bucket_time >= ?
                  AND COALESCE(NULLIF(tool_name, ''), 'Unknown') IN ({placeholders})
                GROUP BY bucket_label, tool_label
                ORDER BY bucket_label ASC
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"), *labels),
            ).fetchall()

        return self._build_time_series(
            rows=rows,
            label_column="tool_label",
            value_column="total_count",
            ordered_labels=labels,
        )

    def fetch_online_time_trend(self, *, days: int, bucket: str) -> TimeSeriesResult:
        """读取在线时长趋势。"""

        if not self._table_exists("online_time"):
            raise StatisticsDataError("缺少 online_time 表，无法绘制在线时长")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time_text = self._start_time(days).strftime("%Y-%m-%d %H:%M:%S")
        bucket_sql = self.bucket_label_sql("COALESCE(start_timestamp, timestamp)", normalized_bucket)
        with self._connect_readonly() as connection:
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       SUM(duration_minutes) AS online_minutes,
                       COUNT(*) AS record_count
                FROM online_time
                WHERE COALESCE(start_timestamp, timestamp) >= ?
                GROUP BY bucket_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text,),
            ).fetchall()

        timestamps: List[datetime] = []
        values_by_key: Dict[str, List[float]] = {"online_hours": [], "record_count": []}
        for row in rows:
            bucket_time = self.parse_datetime(row["bucket_label"])
            if bucket_time is None:
                continue
            timestamps.append(bucket_time)
            values_by_key["online_hours"].append(round(float(row["online_minutes"] or 0) / 60, 2))
            values_by_key["record_count"].append(float(row["record_count"] or 0))
        return TimeSeriesResult(
            timestamps=timestamps,
            values_by_key=values_by_key,
            labels_by_key={"online_hours": "在线时长(小时)", "record_count": "记录数"},
            total=sum(values_by_key["online_hours"]),
            source_count=len(rows),
        )

    def fetch_interaction_trend(self, *, days: int, bucket: str) -> TimeSeriesResult:
        """读取交互消息趋势。"""

        if not self._table_exists("mai_messages"):
            raise StatisticsDataError("缺少 mai_messages 表，无法绘制交互消息统计")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time_text = self._start_time(days).strftime("%Y-%m-%d %H:%M:%S")
        bucket_sql = self.bucket_label_sql("timestamp", normalized_bucket)
        with self._connect_readonly() as connection:
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       COUNT(*) AS total_messages,
                       SUM(CASE WHEN is_mentioned OR is_at OR COALESCE(reply_to, '') != '' THEN 1 ELSE 0 END) AS direct_interactions,
                       SUM(CASE WHEN is_mentioned THEN 1 ELSE 0 END) AS mentioned_messages,
                       SUM(CASE WHEN COALESCE(reply_to, '') != '' THEN 1 ELSE 0 END) AS reply_messages,
                       SUM(CASE WHEN is_picture THEN 1 ELSE 0 END) AS picture_messages,
                       SUM(CASE WHEN is_emoji THEN 1 ELSE 0 END) AS emoji_messages,
                       SUM(CASE WHEN is_command THEN 1 ELSE 0 END) AS command_messages
                FROM mai_messages
                WHERE timestamp >= ?
                GROUP BY bucket_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text,),
            ).fetchall()

        timestamps: List[datetime] = []
        keys = [
            "total_messages",
            "direct_interactions",
            "mentioned_messages",
            "reply_messages",
            "picture_messages",
            "emoji_messages",
            "command_messages",
        ]
        values_by_key: Dict[str, List[float]] = {key: [] for key in keys}
        for row in rows:
            bucket_time = self.parse_datetime(row["bucket_label"])
            if bucket_time is None:
                continue
            timestamps.append(bucket_time)
            for key in keys:
                values_by_key[key].append(float(row[key] or 0))
        return TimeSeriesResult(
            timestamps=timestamps,
            values_by_key=values_by_key,
            labels_by_key={
                "total_messages": "全部消息",
                "direct_interactions": "直接互动",
                "mentioned_messages": "提及麦麦",
                "reply_messages": "回复消息",
                "picture_messages": "图片消息",
                "emoji_messages": "表情消息",
                "command_messages": "命令消息",
            },
            total=sum(values_by_key["total_messages"]),
            source_count=len(rows),
        )

    def fetch_cache_trend(self, *, days: int, bucket: str) -> TimeSeriesResult:
        """读取 Prompt Cache 命中趋势。"""

        if not self._table_exists("llm_usage"):
            raise StatisticsDataError("缺少 llm_usage 表，无法绘制缓存命中统计")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time_text = self._start_time(days).strftime("%Y-%m-%d %H:%M:%S")
        bucket_sql = self.bucket_label_sql("timestamp", normalized_bucket)
        with self._connect_readonly() as connection:
            rows = connection.execute(
                f"""
                SELECT {bucket_sql} AS bucket_label,
                       SUM(prompt_cache_hit_tokens) AS hit_tokens,
                       SUM(prompt_cache_miss_tokens) AS miss_tokens,
                       COUNT(*) AS request_count,
                       SUM(CASE WHEN prompt_cache_enabled THEN 1 ELSE 0 END) AS cache_enabled_requests
                FROM llm_usage
                WHERE timestamp >= ?
                GROUP BY bucket_label
                ORDER BY bucket_label ASC
                """,
                (start_time_text,),
            ).fetchall()

        timestamps: List[datetime] = []
        keys = ["hit_tokens", "miss_tokens", "hit_rate", "cache_enabled_requests", "request_count"]
        values_by_key: Dict[str, List[float]] = {key: [] for key in keys}
        for row in rows:
            bucket_time = self.parse_datetime(row["bucket_label"])
            if bucket_time is None:
                continue
            hit_tokens = float(row["hit_tokens"] or 0)
            miss_tokens = float(row["miss_tokens"] or 0)
            total_cache_tokens = hit_tokens + miss_tokens
            timestamps.append(bucket_time)
            values_by_key["hit_tokens"].append(hit_tokens)
            values_by_key["miss_tokens"].append(miss_tokens)
            values_by_key["hit_rate"].append(round(hit_tokens / total_cache_tokens * 100, 2) if total_cache_tokens else 0)
            values_by_key["cache_enabled_requests"].append(float(row["cache_enabled_requests"] or 0))
            values_by_key["request_count"].append(float(row["request_count"] or 0))
        return TimeSeriesResult(
            timestamps=timestamps,
            values_by_key=values_by_key,
            labels_by_key={
                "hit_tokens": "缓存命中 token",
                "miss_tokens": "缓存未命中 token",
                "hit_rate": "命中率(%)",
                "cache_enabled_requests": "启用缓存请求数",
                "request_count": "请求数",
            },
            total=sum(values_by_key["hit_tokens"]) + sum(values_by_key["miss_tokens"]),
            source_count=len(rows),
        )

    def fetch_tool_chat_distribution(
        self,
        *,
        days: int,
        top_chats: int,
        top_tools: int,
        min_chat_total: int,
    ) -> PieChartResult:
        """读取工具调用在聊天流和工具名上的分布。"""

        if not self._table_exists("tool_records"):
            raise StatisticsDataError("缺少 tool_records 表，无法绘制工具分布")

        start_time = self._start_time(days)
        with self._connect_readonly() as connection:
            name_mapping = self._load_chat_name_mapping(connection)
            rows = connection.execute(
                """
                SELECT r.session_id,
                       COALESCE(NULLIF(r.tool_name, ''), 'Unknown') AS tool_name,
                       COUNT(*) AS usage_count,
                       s.group_id,
                       s.group_name,
                       s.user_id,
                       s.user_cardname,
                       s.user_nickname
                FROM tool_records AS r
                LEFT JOIN chat_sessions AS s ON s.session_id = r.session_id
                WHERE r.timestamp >= ?
                  AND COALESCE(r.session_id, '') != ''
                  AND COALESCE(r.tool_name, '') != ''
                GROUP BY r.session_id, tool_name
                """,
                (start_time.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchall()

        chat_counts: CounterType[str] = Counter()
        tool_counts: CounterType[str] = Counter()
        for row in rows:
            chat_label = self._format_chat_name(row, name_mapping)
            usage_count = int(row["usage_count"] or 0)
            chat_counts[chat_label] += usage_count
            tool_counts[str(row["tool_name"])] += usage_count

        if min_chat_total > 1:
            chat_counts = Counter({label: count for label, count in chat_counts.items() if count >= min_chat_total})

        total = sum(tool_counts.values())
        pies = [
            {"title": "工具调用总分布", "data": self._pie_items_from_counter(tool_counts, limit=top_tools)},
            {"title": "聊天流工具调用分布", "data": self._pie_items_from_counter(chat_counts, limit=top_chats)},
        ]
        return PieChartResult(pies=pies, total=total, source_count=len(rows))

    def fetch_log_level_trend(self, *, days: int, bucket: str) -> TimeSeriesResult:
        """读取 app_*.log.jsonl 日志等级趋势。"""

        if not self.logs_dir.exists():
            raise StatisticsDataError(f"日志目录不存在: {self.logs_dir}")

        normalized_bucket = self.normalize_bucket(bucket)
        start_time = self._start_time(days)
        counts: DefaultDict[datetime, CounterType[str]] = defaultdict(Counter)
        files = sorted(self.logs_dir.glob("app_*.log.jsonl"), key=lambda path: path.stat().st_mtime)[-self.max_log_files :]
        parsed_lines = 0
        for path in files:
            file_date = self._date_from_app_log_name(path.name)
            if file_date is not None and file_date < start_time.date() - timedelta(days=1):
                continue
            for item in self._iter_jsonl(path):
                timestamp = self._log_item_datetime(item, file_date)
                if timestamp is None or timestamp < start_time:
                    continue
                bucket_time = self._truncate_datetime(timestamp, normalized_bucket)
                level = str(item.get("level") or "unknown").lower()
                counts[bucket_time][level] += 1
                parsed_lines += 1

        levels = ["debug", "info", "warning", "error", "critical", "unknown"]
        return self._build_counter_time_series(counts, levels, parsed_lines)

    def fetch_prompt_duration_stats(
        self,
        *,
        days: int,
        kind_filter: Optional[str],
        top_items: int,
    ) -> CategoryChartResult:
        """读取 maisaka_prompt 调试快照的耗时统计。"""

        prompt_root = self.logs_dir / "maisaka_prompt"
        if not prompt_root.exists():
            raise StatisticsDataError(f"Prompt 日志目录不存在: {prompt_root}")

        start_time = self._start_time(days)
        buckets: DefaultDict[str, List[float]] = defaultdict(list)
        counts: CounterType[str] = Counter()
        files = sorted(prompt_root.rglob("*.json"), key=lambda path: path.stat().st_mtime)[-self.max_prompt_files :]
        scanned = 0
        for path in files:
            timestamp = self._prompt_file_datetime(path)
            if timestamp is None or timestamp < start_time:
                continue

            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            request = payload.get("request") if isinstance(payload, dict) else None
            metadata = payload.get("metadata") if isinstance(payload, dict) else None
            if not isinstance(request, dict) or not isinstance(metadata, dict):
                continue

            kind = str(request.get("kind") or path.parents[1].name)
            if kind_filter and kind != kind_filter:
                continue
            duration_ms = metadata.get("duration_ms")
            if not isinstance(duration_ms, (int, float)):
                continue

            label = str(metadata.get("model_name") or "Unknown") if kind_filter else kind
            buckets[label].append(float(duration_ms) / 1000)
            counts[label] += 1
            scanned += 1

        rows: List[Dict[str, Any]] = []
        for label, values in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))[: max(1, top_items)]:
            sorted_values = sorted(values)
            rows.append(
                {
                    "label": label,
                    "avg_duration": round(sum(values) / len(values), 2),
                    "p95_duration": round(self._percentile(sorted_values, 0.95), 2),
                    "request_count": counts[label],
                }
            )
        total = sum(row["request_count"] for row in rows)
        return CategoryChartResult(data=rows, total=total, source_count=scanned)

    def fetch_memory_probe_trend(self, *, days: int) -> TimeSeriesResult:
        """读取 memory_probe*.jsonl 的内存趋势。"""

        if not self.logs_dir.exists():
            raise StatisticsDataError(f"日志目录不存在: {self.logs_dir}")

        start_time = self._start_time(days)
        points: List[Tuple[datetime, Dict[str, float]]] = []
        for path in sorted(self.logs_dir.glob("memory_probe*.jsonl")):
            for item in self._iter_jsonl(path):
                timestamp = self.parse_datetime(item.get("timestamp"))
                if timestamp is None or timestamp < start_time:
                    continue
                process = item.get("process") if isinstance(item, dict) else None
                tracemalloc_data = item.get("tracemalloc") if isinstance(item, dict) else None
                if not isinstance(process, dict):
                    continue
                points.append(
                    (
                        timestamp,
                        {
                            "rss_mib": float(process.get("rss_mib") or 0),
                            "uss_mib": float(process.get("uss_mib") or 0),
                            "tracemalloc_current_mib": float(
                                (tracemalloc_data or {}).get("current_mib") if isinstance(tracemalloc_data, dict) else 0
                            ),
                        },
                    )
                )

        if len(points) > self.max_memory_points:
            step = max(1, len(points) // self.max_memory_points)
            points = points[::step][-self.max_memory_points :]

        timestamps = [timestamp for timestamp, _values in points]
        values_by_key = {
            "rss_mib": [values["rss_mib"] for _timestamp, values in points],
            "uss_mib": [values["uss_mib"] for _timestamp, values in points],
            "tracemalloc_current_mib": [values["tracemalloc_current_mib"] for _timestamp, values in points],
        }
        labels_by_key = {
            "rss_mib": "RSS (MiB)",
            "uss_mib": "USS (MiB)",
            "tracemalloc_current_mib": "tracemalloc 当前 (MiB)",
        }
        total = max(values_by_key["rss_mib"]) if points else 0
        return TimeSeriesResult(timestamps, values_by_key, labels_by_key, total, len(points))

    def build_summary(self, *, days: int) -> str:
        """构建统计摘要文本。"""

        start_time = self._start_time(days)
        lines = [
            "统计绘图摘要",
            f"数据范围：最近 {days} 天",
        ]
        with self._connect_readonly() as connection:
            for label, table_name, column_name in [
                ("消息", "statistics_message_hourly", "message_count"),
                ("模型请求", "statistics_model_hourly", "request_count"),
                ("工具调用", "statistics_tool_hourly", "call_count"),
            ]:
                if not self._table_exists(table_name):
                    continue
                row = connection.execute(
                    f"""
                    SELECT COALESCE(SUM({column_name}), 0) AS total_count,
                           COUNT(*) AS row_count
                    FROM {table_name}
                    WHERE bucket_time >= ?
                    """,
                    (start_time.strftime("%Y-%m-%d %H:%M:%S"),),
                ).fetchone()
                lines.append(f"{label}：{int(row['total_count'] or 0)}（聚合行 {int(row['row_count'] or 0)}）")
            if self._table_exists("statistics_model_hourly"):
                row = connection.execute(
                    """
                    SELECT COALESCE(SUM(total_tokens), 0) AS total_tokens,
                           COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                           COALESCE(SUM(completion_tokens), 0) AS completion_tokens
                    FROM statistics_model_hourly
                    WHERE bucket_time >= ?
                    """,
                    (start_time.strftime("%Y-%m-%d %H:%M:%S"),),
                ).fetchone()
                lines.append(
                    "Token："
                    f"{int(row['total_tokens'] or 0)}"
                    f"（输入 {int(row['prompt_tokens'] or 0)}，输出 {int(row['completion_tokens'] or 0)}）"
                )
            if self._table_exists("online_time"):
                row = connection.execute(
                    """
                    SELECT COALESCE(SUM(duration_minutes), 0) AS online_minutes
                    FROM online_time
                    WHERE COALESCE(start_timestamp, timestamp) >= ?
                    """,
                    (start_time.strftime("%Y-%m-%d %H:%M:%S"),),
                ).fetchone()
                lines.append(f"在线时长：{float(row['online_minutes'] or 0) / 60:.2f} 小时")
            if self._table_exists("mai_messages"):
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS total_messages,
                           SUM(CASE WHEN is_mentioned OR is_at OR COALESCE(reply_to, '') != '' THEN 1 ELSE 0 END) AS direct_interactions
                    FROM mai_messages
                    WHERE timestamp >= ?
                    """,
                    (start_time.strftime("%Y-%m-%d %H:%M:%S"),),
                ).fetchone()
                lines.append(
                    f"交互消息：{int(row['direct_interactions'] or 0)}"
                    f"（总消息 {int(row['total_messages'] or 0)}）"
                )
            if self._table_exists("llm_usage"):
                row = connection.execute(
                    """
                    SELECT COALESCE(SUM(prompt_cache_hit_tokens), 0) AS hit_tokens,
                           COALESCE(SUM(prompt_cache_miss_tokens), 0) AS miss_tokens
                    FROM llm_usage
                    WHERE timestamp >= ?
                    """,
                    (start_time.strftime("%Y-%m-%d %H:%M:%S"),),
                ).fetchone()
                hit_tokens = float(row["hit_tokens"] or 0)
                miss_tokens = float(row["miss_tokens"] or 0)
                cache_total = hit_tokens + miss_tokens
                hit_rate = hit_tokens / cache_total * 100 if cache_total else 0
                lines.append(f"Prompt Cache：{hit_rate:.2f}% 命中（{int(cache_total)} token）")
        return "\n".join(lines)

    def _build_time_series(
        self,
        *,
        rows: Sequence[sqlite3.Row],
        label_column: str,
        value_column: str,
        ordered_labels: Sequence[str],
    ) -> TimeSeriesResult:
        bucket_values: DefaultDict[datetime, Dict[str, float]] = defaultdict(dict)
        for row in rows:
            bucket_time = self.parse_datetime(row["bucket_label"])
            if bucket_time is None:
                continue
            label = str(row[label_column])
            bucket_values[bucket_time][label] = float(row[value_column] or 0)

        timestamps = sorted(bucket_values)
        used_keys: set[str] = set()
        key_by_label = {label: self._label_key(label, used_keys) for label in ordered_labels}
        values_by_key = {
            key_by_label[label]: [bucket_values[timestamp].get(label, 0) for timestamp in timestamps]
            for label in ordered_labels
        }
        labels_by_key = {key_by_label[label]: label for label in ordered_labels}
        total = sum(sum(values) for values in values_by_key.values())
        return TimeSeriesResult(timestamps, values_by_key, labels_by_key, total, len(rows))

    def _build_counter_time_series(
        self,
        counts: Dict[datetime, CounterType[str]],
        ordered_labels: Sequence[str],
        source_count: int,
    ) -> TimeSeriesResult:
        timestamps = sorted(counts)
        active_labels = [label for label in ordered_labels if any(counter.get(label, 0) for counter in counts.values())]
        values_by_key = {
            label: [float(counts[timestamp].get(label, 0)) for timestamp in timestamps] for label in active_labels
        }
        labels_by_key = {label: label for label in active_labels}
        total = sum(sum(values) for values in values_by_key.values())
        return TimeSeriesResult(timestamps, values_by_key, labels_by_key, total, source_count)

    @staticmethod
    def _pie_items_from_counter(counter: CounterType[str], *, limit: int) -> List[Dict[str, Any]]:
        sorted_items = sorted(counter.items(), key=lambda item: (-item[1], item[0]))
        labels = [{"name": label, "value": int(value)} for label, value in sorted_items[:limit] if value > 0]
        others = sum(value for _, value in sorted_items[limit:] if value > 0)
        if others > 0:
            labels.append({"name": "其他", "value": int(others)})
        return labels

    @staticmethod
    def _truncate_datetime(timestamp: datetime, bucket: str) -> datetime:
        if bucket == "hour":
            return timestamp.replace(minute=0, second=0, microsecond=0)
        return timestamp.replace(hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _date_from_app_log_name(file_name: str):
        match = re.match(r"app_(\d{8})_", file_name)
        if match is None:
            return None
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            return None

    @classmethod
    def _log_item_datetime(cls, item: Dict[str, Any], file_date) -> Optional[datetime]:
        timestamp = item.get("timestamp")
        if file_date is None:
            return cls.parse_datetime(timestamp)
        if isinstance(timestamp, str):
            match = re.match(r"(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})", timestamp)
            if match:
                month, day, hour, minute, second = map(int, match.groups())
                return datetime(file_date.year, month, day, hour, minute, second)
        return cls.parse_datetime(timestamp)

    @staticmethod
    def _prompt_file_datetime(path: Path) -> Optional[datetime]:
        stem = path.stem
        if stem.isdigit():
            try:
                value = int(stem)
                if value > 10_000_000_000:
                    return datetime.fromtimestamp(value / 1000)
                return datetime.fromtimestamp(value)
            except (OSError, ValueError):
                pass
        return datetime.fromtimestamp(path.stat().st_mtime)

    @staticmethod
    def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as file:
                for line in file:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield payload
        except OSError:
            return

    @staticmethod
    def _percentile(sorted_values: Sequence[float], percentile: float) -> float:
        if not sorted_values:
            return 0
        index = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * percentile))))
        return sorted_values[index]
