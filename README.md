# 突破追多与移动保本系统（v3.5 Pro）

专注于币安 USDT 永续合约中**真实流通市值低于 3,000 万美元、负费率逼空、巨量高换手、1h EMA 顺势共振**的纯加密标的（彻底排除 TradFi 股票/大宗商品/外汇通证），以 TradingView 15m 对数下降趋势线实体收盘突破为入场信号，以“破线跌回立即止损（< 1.5%）+ 浮盈超 6% 移动保本锁定至 +1%”为执行纪律，捕捉确定性爆发机会。

---

## 核心交付终端

直接在浏览器中打开根目录下的 [`index.html`](file:///Users/zhangshuai/PycharmProjects/ai选币/index.html) 即可进入实战工作台：

1. **中央实战工作台（[`index.html`](file:///Users/zhangshuai/PycharmProjects/ai选币/index.html)）**
   - 5 块联动 KPI 机会区过滤卡（👑 顺势高换手共振区、🔥 负费率逼空区、⚡ 巨量高换手区、📈 1h EMA 顺势区、🌐 全量底池）。
   - 前端每 60 秒直连币安公开 API 轮询刷新价格、24h 区间分位、动态市值、换手倍数与 4h 等效资费，支持后台标签页自动休眠。
   - 内置 TradingView 15m 专属布局直达、一键复制 / 导出 `.txt` 批量导入 TV 自选、实战四步法速查折叠抽屉、整点资费结算倒计时及快捷键（`/` 搜币、`1-5` 切分区、`Esc` 清空/退出）。
2. **突破追多与移动保本战法手册（[`reports/strategy_second_leg_playbook.html`](file:///Users/zhangshuai/PycharmProjects/ai选币/reports/strategy_second_leg_playbook.html)）**
   - 系统化呈现实战四步法（标的初选、TV画对数线、15m实体突破开多、破线止损与移动保本）以及币安 API 联动自动化风控体系。

---

## 目录结构

```text
ai选币/
├── index.html                                 # 中央实战工作台 (浏览器直接打开)
├── README.md                                  # 项目说明与运行指南
├── .env.example                               # 统一密钥与配置模板
├── .env                                       # 私密配置 (CMC / Server酱 / 币安合约 API / 风控参数)
│
├── reports/                                   # 战法手册产物
│   └── strategy_second_leg_playbook.html      # 突破追多与移动保本战法手册
│
├── scripts/                                   # 核心流水线脚本
│   ├── scan_second_leg_pool.py                # 币安 + CMC Pro 全池核验与 1h 趋势/共振时长扫描
│   ├── hud_common.py                          # 双端 HUD 公共渲染、表格行与前端轮询模块
│   ├── build_index.py                         # 编译中央实战工作台 index.html
│   ├── build_second_leg_playbook.py           # 编译战法手册 HTML
│   ├── trade_alert_daemon.py                  # 交易风控与四重预警守护进程 (Server酱推送 + 币安持仓同步)
│   ├── tv_trendline_engine.py                 # TV 云端画线解析器与对数趋势线解算引擎
│   ├── binance_position_client.py             # 币安合约实盘持仓客户端 (HMAC-SHA256)
│   ├── sync_all.py                            # 一键串联“扫描 → 编译首页 → 编译手册 → 风控评测”
│   └── hourly_sync.sh                         # 每小时自动同步脚本 (带原子锁与 120s 冷却防重)
│
├── runs/                                      # 运行时数据缓存与日志
│   ├── second_leg_candidates_pool.json        # 最新核验的纯加密候选币池
│   ├── sub_30m_history.json                   # 曾低于 $30M 标的的 7 天溢出跟踪记录
│   └── cron_sync.log                          # 每小时自动同步执行日志
│
└── tests/                                     # 单元与系统测试套件
    ├── test_trading_system.py                 # 对数数学、TV 画线、突破与通知状态测试
    └── test_binance_position_sync.py          # 币安持仓自动对齐与风控评测测试
```

---

## 统一配置说明

系统采用标准化 `.env` 单文件集中管理所有密钥与风控参数：

```bash
# 复制模板创建本地配置
cp .env.example .env

# 编辑 .env 填入你的密钥 (CMC API, Server酱 SendKey, 币安只读/合约 API)
```

支持参数列表详见 [.env.example](file:///Users/zhangshuai/PycharmProjects/ai选币/.env.example)。

---

## 常用命令

```bash
# 一键全量同步（扫描候选池 + 编译双端 HTML + 执行单次风控评测）
python3 scripts/sync_all.py

# 服务器定时任务：只刷新数据和看板，风控由常驻守护进程执行
bash scripts/hourly_sync.sh
# 可用 PYTHON_BIN 指定 Python 解释器路径

# 启动交易预警与风控守护后台进程 (10s 移动锁利巡检 + 15m 突破/止损确认)
python3 scripts/trade_alert_daemon.py --daemon

# 单独重新编译双端页面（基于 runs/second_leg_candidates_pool.json）
python3 scripts/build_index.py
python3 scripts/build_second_leg_playbook.py

# 运行完整测试套件 (28 项测试)
python3 -m unittest discover -s tests -p "test_*.py"
```


## Linux 服务器运行

服务器使用一个 SQLite 文件保存账户报表、流水、成交和候选池快照。首次部署时应保留已有 `runs/account.sqlite3`、`runs/trade_positions.json` 和 `runs/resonance_state.json`。账户记录起点存放在账户报表中。

`deploy/systemd/` 包含守护进程、小时刷新定时器和只监听本机的看板接口配置。`deploy/nginx/ai-coin-selector.locations.conf` 需要包含在已有 HTTPS server 配置内。账户和策略页面由发布目录提供，选币首页从 SQLite 候选快照生成。部署时在独立服务用户下执行 `python3 scripts/build_account_analytics.py` 和 `python3 scripts/build_second_leg_playbook.py`，将生成文件发布到 Nginx 指向的位置。

小时刷新从 `COIN_DATABASE` 指定的数据库发布候选快照。账户同步定时器每 5 分钟使用 `--database` 更新账户账本。服务运行前须配置项目目录下的 `.env`，并确保数据库目录对服务用户可写。启用定时器前先完成一次刷新，确认候选池和账户记录正常。Nginx 和 systemd 配置修改后分别执行 `nginx -t` 与 `systemd-analyze verify`。
