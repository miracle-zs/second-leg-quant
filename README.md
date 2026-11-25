# 突破追多与移动保本系统（v3.5 Pro）

专注于币安 USDT 永续合约中**真实流通市值低于 3,000 万美元、负费率逼空、巨量高换手、1h EMA 顺势共振**的纯加密标的（排除 TradFi 股票/大宗商品/外汇通证），以 TradingView 15m 对数下降趋势线实体收盘突破为入场信号，以“破线跌回立即止损（< 1.5%）+ 浮盈超 6% 移动保本锁定至 +1%”为执行纪律，捕捉二段行情机会。

---

## 核心交付终端

通过本地或反向代理运行 [`scripts/account_dashboard_api.py`](scripts/account_dashboard_api.py) 后访问对应页面（也可由构建脚本编译纯静态 HTML 壳文件）：

1. **策略监控看板（[`index.html`](index.html) / `/`）**
   - 5 块联动 KPI 机会区过滤卡（👑 顺势高换手共振区、⚠️ 负费率贴水区、⚡ 巨量高换手区、📈 1h EMA 顺势区、🌐 全量候选底池）。
   - 前端 [`web/candidates.js`](web/candidates.js) 从 `api/candidates` 拉取候选池（支持 `ETag` 与 30 秒 `api/candidate-version` 无刷新增量更新），并每 60 秒直连币安公开 API 刷新实时价格、24h 分位、动态市值、换手倍数与 4h 等效资费（后台标签页自动暂停）。
   - 内置双引擎 K 线弹窗（币安直连极速 Canvas K 线 + TradingView 内嵌切换）、TV 一键复制 / `.txt` 导出、战法四步法速查抽屉、整点资费结算倒计时及快捷键（`/` 搜币、`1-5` 切分区、`Esc` 清空/退出）。
2. **账户数据分析与量化风控看板（[`account.html`](account.html) / `/account.html`）**
   - 由 [`web/account.js`](web/account.js) 从 `api/account` 拉取账户快照（支持 `ETag` / `304 Not Modified` 及 30 秒轮询），动态渲染 10 项资产与量化风控 KPI、资金净值与回撤/每日盈亏/胜负分布/单笔收益散点图表、在途持仓监控与历史流水筛选排序。
3. **突破追多与移动保本战法手册（[`reports/strategy_second_leg_playbook.html`](reports/strategy_second_leg_playbook.html)）**
   - 系统化呈现实战四步法（标的初选、TV 对数画线、15m 实体突破开多、破线止损与移动保本）以及币安 API 联动自动化风控体系。

---

## 目录结构

```text
second-leg-quant/
├── index.html                                 # 策略监控看板静态壳 (编译产物)
├── account.html                               # 账户数据分析看板静态壳 (编译产物)
├── README.md                                  # 项目说明与运行指南
├── .env.example                               # 统一密钥与配置模板
├── .env                                       # 私密配置 (CMC / Server酱 / 币安合约 API / COIN_DATABASE)
│
├── web/                                       # 前端共享样式与客户端脚本
│   ├── styles.css                             # 三端共享暗色 HUD 样式
│   ├── candidates.js                          # 候选池看板渲染、轮询与 K 线弹窗逻辑
│   └── account.js                             # 账户看板渲染、Chart.js 图表与流水筛选排序
│
├── reports/                                   # 战法手册产物
│   └── strategy_second_leg_playbook.html      # 突破追多与移动保本战法手册
│
├── scripts/                                   # 核心流水线与服务端脚本
│   ├── config.py                              # 路径、.env、SQLite (WAL) 连接、原子写入与文件锁中心
│   ├── binance_client.py                      # 币安合约统一 HTTP/签名/重试客户端
│   ├── binance_position_client.py             # 币安合约实盘持仓同步与 15m 趋势线匹配
│   ├── tv_trendline_engine.py                 # TV 云端画线解析器与对数趋势线解算引擎
│   ├── scan_second_leg_pool.py                # 币安 + CMC Pro 全池核验与 1h 趋势/共振扫描
│   ├── hud_common.py                          # 公共导航栏、策略看板与战法手册静态壳生成
│   ├── build_index.py                         # 编译策略监控看板静态壳 index.html
│   ├── build_second_leg_playbook.py           # 编译战法手册静态壳 HTML
│   ├── account_analytics_engine.py            # 币安账户/流水/FIFO 成交重建与量化风控指标引擎
│   ├── build_account_analytics.py             # 编译账户分析看板静态壳 account.html
│   ├── account_dashboard_api.py               # SQLite WAL 读写与多线程 HTTP 看板/API 服务
│   ├── trade_alert_daemon.py                  # 交易风控与四重预警守护进程 (Server酱 + 持仓同步)
│   ├── sync_all.py                            # 一键串联“扫描 → 编译静态壳 → 账户分析 → 风控评测”
│   └── hourly_sync.sh                         # 每小时自动同步脚本 (原子锁 + 120s 冷却防重)
│
├── deploy/                                    # 生产环境部署模板
│   ├── nginx/
│   │   └── ai-coin-selector.locations.conf    # Nginx 反代至 127.0.0.1:18083
│   └── systemd/                               # API、风控守护、账户同步与每小时刷新 service/timer
│
├── runs/                                      # 运行时状态缓存、SQLite 与日志
└── tests/                                     # 单元与回归测试套件
```

---

## 统一配置说明

系统采用标准化 `.env` 单文件集中管理密钥、数据库与风控参数：

```bash
cp .env.example .env
# 编辑 .env 填入密钥 (CMC_API_KEY, SERVERCHAN_SENDKEY, BINANCE_API_KEY/SECRET, COIN_DATABASE 等)
```

支持参数列表详见 [`.env.example`](.env.example)。

---

## 常用命令

```bash
# 一键全量同步（扫描候选池 + 编译静态壳 + 更新账户分析 + 执行单次风控评测）
python3 scripts/sync_all.py

# 启动看板 Web 与 JSON API 服务（默认监听 127.0.0.1:18083）
python3 scripts/account_dashboard_api.py --database runs/account.sqlite3 --port 18083

# 手动同步账户实盘快照到 SQLite
python3 scripts/account_analytics_engine.py --database runs/account.sqlite3

# 启动交易预警与风控守护后台进程 (10s 移动锁利巡检 + 15m 突破/止损确认)
python3 scripts/trade_alert_daemon.py --daemon

# 单独重新编译三端纯静态 HTML 壳
python3 scripts/build_index.py
python3 scripts/build_account_analytics.py
python3 scripts/build_second_leg_playbook.py

# 运行完整测试套件
python3 -m unittest discover -s tests -p "test_*.py"
```

---

## Linux 服务器部署

服务器使用单一 SQLite 文件（默认 `runs/account.sqlite3`，通过 `WAL` 模式支持并发读写）保存账户报表、资金流水、成交明细与候选池快照。首次部署时可保留已有 `runs/account.sqlite3`、`runs/trade_positions.json` 和 `runs/resonance_state.json`。

- `deploy/systemd/` 包含常驻风控守护进程（`ai-coin-selector.service`）、看板 HTTP 服务（`ai-coin-selector-account-api.service`，仅监听 `127.0.0.1:18083`）、每 5 分钟账户同步定时器（`ai-coin-selector-account-sync.{service,timer}`）和每小时候选池刷新定时器（`ai-coin-selector-refresh.{service,timer}`）。
- `deploy/nginx/ai-coin-selector.locations.conf` 将 `/ai-coin-selector/` 统一反向代理至 `http://127.0.0.1:18083/`，由看板服务统一提供静态页面壳、`static/*` 资源与 `/api/*` JSON 接口，无需再向 Nginx 目录手动同步静态 HTML 文件。
- 所有脚本与 systemd 服务在运行前需配置项目目录下的 `.env`（含 `COIN_DATABASE=/opt/ai-coin-selector/runs/account.sqlite3` 或由 unit 注入），并确保 `runs/` 目录对服务用户 `ai-coin-selector` 可写。如需对公网暴露账户数据接口，建议在外层 HTTPS server 块配置访问控制。
- Nginx 和 systemd 配置修改后分别执行 `nginx -t` 与 `systemd-analyze verify`。
