# 统计绘图插件

当前版本：`0.1.3`，支持 MaiBot `1.3.0–1.3.99` 和插件 SDK `2.9.x`。

这是一个只读取本机数据的 MaiBot 统计绘图插件。它仿照 `mai_statstic_plugin` 的图片渲染链路，把图表规格交给 React/Recharts，再用 Playwright 截图成 PNG 并发送到聊天流。

## 数据来源

- `data/MaiBot.db`：只读 SQLite 连接，优先使用 `statistics_message_hourly`、`statistics_model_hourly`、`statistics_tool_hourly` 这些本机聚合表，也会读取 `llm_usage`、`online_time` 和 `mai_messages` 做更细的统计。

插件不会访问远端 API，也不会同步或上传遥测数据。

## 安装与运行

将完整插件解压到 MaiBot 的 `plugins/statistics_chart_plugin` 目录后启用。
发布包自带 `assets/statistics_chart.bundle.js`，运行时无需 Node.js、`dashboard` 源码或 `node_modules`。
需要 Pillow 和 Playwright；Windows 默认使用已安装的 Chrome/Edge，其他环境可安装 Playwright Chromium：

```shell
uv run python -m playwright install chromium
```

也可以用 `MAIBOT_CHART_BROWSER` 指定浏览器可执行文件的完整路径。

## 开发构建

仅修改图表 JSX 后需要在插件目录重新构建，并提交生成的 `assets` 文件：

```shell
npm ci
npm run build
```

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

## 更新记录

### 0.1.3

- 主程序兼容范围修正为 MaiBot `1.3.0–1.3.99`。

### 0.1.2

- 适配 MaiBot 1.3.4 / 插件 SDK 2.9.0。
- 图表脚本随插件发布，修复缺少 `dashboard/node_modules` 时无法出图的问题。
- 数据查询、图表脚本读取和图片编码在线程中执行，避免阻塞插件事件循环。
- 支持 Playwright 自带 Chromium，浏览器和资源错误保留原始信息。
- 增加本地图标。
