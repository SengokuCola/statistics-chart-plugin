"""统计图表规格构建。"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional


STATS_CHART_PALETTE = [
    "#c24d24",
    "#0a4550",
    "#c99a3e",
    "#386641",
    "#7f4f24",
    "#2f6f6f",
    "#6d597a",
    "#173f46",
]


def line_chart_spec(
    *,
    title: str,
    description: str,
    data: List[Dict[str, Any]],
    series: List[Dict[str, Any]],
    width: int = 1200,
    height: int = 700,
    x_key: str = "label",
    x_tick_angle: int = 0,
    y_domain: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """构建折线/柱状组合图规格。"""

    return {
        "kind": "composed",
        "title": title,
        "description": description,
        "width": width,
        "height": height,
        "xKey": x_key,
        "xTickAngle": x_tick_angle,
        "yDomain": y_domain,
        "data": data,
        "series": series,
    }


def pie_grid_spec(
    *,
    title: str,
    description: str,
    pies: List[Dict[str, Any]],
    width: int = 1200,
    height: int = 700,
    pie_margin: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """构建多饼图规格。"""

    spec: Dict[str, Any] = {
        "kind": "pie-grid",
        "title": title,
        "description": description,
        "width": width,
        "height": height,
        "pies": pies,
    }
    if pie_margin is not None:
        spec["pieMargin"] = pie_margin
    return spec


def simple_time_series_spec(
    *,
    title: str,
    description: str,
    timestamps: List[datetime],
    values_by_key: Dict[str, List[float]],
    labels_by_key: Dict[str, str],
    width: int = 1200,
    height: int = 700,
    date_format: str = "%m-%d %H:%M",
    y_domain: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """把同一时间轴上的多条序列转换为图表规格。"""

    data: List[Dict[str, Any]] = []
    for index, timestamp in enumerate(timestamps):
        item: Dict[str, Any] = {"label": timestamp.strftime(date_format)}
        for key, values in values_by_key.items():
            item[key] = values[index] if index < len(values) else 0
        data.append(item)

    return line_chart_spec(
        title=title,
        description=description,
        data=data,
        series=[
            {
                "key": key,
                "label": labels_by_key.get(key, key),
                "type": "line",
                "dot": len(timestamps) <= 80,
                "color": STATS_CHART_PALETTE[index % len(STATS_CHART_PALETTE)],
            }
            for index, key in enumerate(values_by_key)
        ],
        width=width,
        height=height,
        x_tick_angle=-35 if len(timestamps) > 10 else 0,
        y_domain=y_domain,
    )


def pie_items(items: Iterable[tuple[str, int]], *, limit: int = 12, other_label: str = "其他") -> List[Dict[str, Any]]:
    """把标签和值压缩为饼图数据，超出上限的部分归入“其他”。"""

    sorted_items = sorted(items, key=lambda item: item[1], reverse=True)
    labels = [{"name": label, "value": value} for label, value in sorted_items[:limit] if value > 0]
    others = sum(value for _, value in sorted_items[limit:] if value > 0)
    if others > 0:
        for label in labels:
            if label["name"] == other_label:
                label["value"] += others
                break
        else:
            labels.append({"name": other_label, "value": others})
    return labels
