# 统计绘图插件

这是一个只读取本机数据的 MaiBot 统计绘图插件。它仿照 `mai_statstic_plugin` 的图片渲染链路，把图表规格交给 React/Recharts，再用 Playwright 截图成 PNG 并发送到聊天流。

## 数据来源

- `data/MaiBot.db`：只读 SQLite 连接，优先使用 `statistics_message_hourly`、`statistics_model_hourly`、`statistics_tool_hourly` 这些本机聚合表，也会读取 `llm_usage`、`online_time` 和 `mai_messages` 做更细的统计。

插件不会访问远端 API，也不会同步或上传遥测数据。

## 命令

- `/statschart [天数] [show_time]` 或 `/统计绘图 [天数] [show_time]`：发送一张摘要图片，显示核心统计、参数说明和命令帮助；天数可写 `7`、`days=7`、`7d`、`1w`。
- `/msgstats [天数] [hour|day] [topN]`：绘制聊天流消息量趋势。
- `/tokenstats [天数] [hour|day] [model|module|provider|type] [topN]`：绘制 token 使用折线图；不传分组时显示总 token、输入 token、输出 token 和请求数。
- `/tokenpie [天数] [model|module|provider|type] [topN]`：绘制 token 使用分布饼图，默认按模型分组。
- `/modelstats [天数] [hour|day] [token|request|cost|latency] [module=模块名] [topN]`：绘制模型调用趋势。
- `/toolstats [天数] [hour|day] [topN]`：绘制工具调用趋势。
- `/toolchat [天数] [topN] [tools=N] [min=N]`：绘制工具调用在聊天流和工具名上的分布。
- `/onlinestats [天数] [hour|day]`：绘制在线时长趋势。
- `/interactionstats [天数] [hour|day]`：绘制交互消息趋势，包括直接互动、提及、回复、图片、表情和命令消息。
- `/cachestats [天数] [hour|day]`：绘制 Prompt Cache 命中 token、未命中 token 和命中率趋势。

所有绘图命令都可以追加 `show_time` 查看数据准备、图表渲染、图片编码和发送等待耗时。

## 配置

```toml
[data]
db_path = "data/MaiBot.db"

[draw]
pic_dir = "data/pic"

[cache]
summary_cache_seconds = 120
```
