# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

加密货币永续合约主观量化交易员（以桌面端大屏/多屏高频盯盘为主，兼顾移动端随时巡检持仓与风控状态）。核心任务是从全市场快速筛选出符合二段爆发条件的低市值标的，结合 TradingView 对数趋势线捕捉 15m 收盘突破多单机会，并对在途实盘仓位执行严格的破线止损与移动保本纪律。

## Product Purpose

「突破追多与移动保本系统（v3.5 Pro）」是一套面向币安 USDⓈ-M 永续合约的选币监控、实盘风控与账户绩效复盘终端。帮助交易员过滤第一段无序暴涨与破位急跌噪音，聚焦于多头趋势与高换手共振的二段主升机会，并以自动化风控预警守住底线。

## Positioning

专注于 CoinMarketCap 真实流通市值低于 3,000 万美元（含 7 天溢出跟踪）的纯加密币安 USDT 永续合约（剔除 TradFi 通证），将「1h EMA(5>10) 顺势 + 24h 换手率 ≥ 1.5x + 4h 负费率贴水逼空」选池机制与「TradingView 专属云端布局（`ig0LuDL8`）15m 对数下降趋势线收盘突破入场 + 跌回趋势线立即止损（<1.5%）+ 浮盈 ≥ +6% 锁定 +1% 保本」闭环衔接。

## Operating Context

- **日常工作流**：在「策略监控看板（`index.html`）」按五大机会区筛选标的、站内预览 15m/1h/4h/1d K 线或批量复制/导出至 TradingView 自选列表并在 `ig0LuDL8` 对数坐标画线；后台守护进程监控 15m 收盘突破/破线与 10 秒级锁利并通过 Server 酱推送；在「账户数据分析（`account.html`）」实时查看净值、夏普/索提诺等风控指标、在途多空敞口与历史平仓归因；在「战法手册（`playbook.html`）」核对三原则与四步执行铁律。
- **使用环境**：长时间盯盘环境，对数字对齐（`tabular-nums`）、状态辨识度、多列冻结表格横向浏览及高对比度可读性要求高。

## Capabilities and Constraints

- **三页核心终端**：`web/index.html`（策略监控看板）、`web/account.html`（账户数据分析与风控看板）、`web/playbook.html`（突破追多与移动保本战法手册）。
- **零前端构建框架架构**：纯静态 HTML 外壳 + 原生 JavaScript（`common.js`、`candidates.js`、`kline-modal.js`、`account.js`）+ 预编译静态 `tailwind.css` 与共享 `styles.css` + 本地固化 `vendor/chart.js`，由 `scripts/account_dashboard_api.py` 统一托管静态资源与同源 `/api/*` 接口（支持 Nginx `/ai-coin-selector/` 子路径反向代理与相对路径资源加载）。
- **严格保持现有契约**：保留全部实时轮询（ETag / 304 / 后台标签页暂停）、`sessionStorage` 状态持久化、快捷键（`/`、`1-5`、`Esc`）、原生 `<dialog>` 双引擎 K 线弹窗、全部现有 DOM ID / 数据绑定钩子以及 Python + Node 自动化测试套件契约（如 `account.html` 纯静态无内联 `<style>` 且体积 < 35KB、`styles.css` 保留共振行选择器等）。

## Brand Commitments

- 系统名称：**突破追多与移动保本系统**
- 关联 TradingView 布局 ID：`ig0LuDL8`

## Evidence on Hand

- 候选池与账户真实结构化数据模型由 `/api/candidates`、`/api/candidate-version`、`/api/account` 及同源币安代理 `/api/binance/*` 提供，本地运行目录 `runs/` 存有真实候选池与账户快照 JSON 可供初始化 SQLite 预览验证。
- 严禁虚构不存在的自动下单接口（当前系统为实盘只读同步 + Server 酱信号预警，空头仓位不纳入 15m 自动风控提醒）。

## Product Principles

1. **信号信噪比优先**：将双核黄金共振、贴水逼空与风险状态作为第一视觉锚点，抑制无意义的装饰噪音。
2. **高速扫读与肌肉记忆**：全量金融数值等宽对齐，高频操作（切分区、搜代号、看 15m 图、复制 TV 自选）保持单步直达与键盘友好。
3. **纪律与边界透明**：清晰区分受监控多单与未受监控空单、单期资费与 4h 折算资费、底池与 7d 溢出跟踪，绝不掩盖异常或缺失数据。
4. **轻量自包含交付**：零外部运行时 CDN 依赖（除显式切换的 TV 内嵌 iframe 外），静态资源体积克制、首屏即开即用。
