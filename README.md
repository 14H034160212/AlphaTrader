# AlphaTrader Pro

**AlphaTrader Pro** 是一套全自动 AI 量化交易系统，基于 Python/FastAPI 后端 + 纯 HTML/JS 前端，通过本地 DeepSeek-R1 70B + Kronos K 线预测模型进行分析，经由 Alpaca Live API 执行真实交易。

---

## 核心功能

- **实时行情**：Yahoo Finance 全球市场数据，自动刷新（每 2 分钟，错峰请求）
- **K 线预测**：Kronos foundation model（在 45+ 交易所数据上训练）预测未来 5 根蜡烛
- **AI 决策**：本地 DeepSeek-R1 70B（Ollama DRL70B）综合 K 线预测 + 技术指标 + 新闻 + 社交情绪
- **自动交易**：置信度 ≥ 70% 自动通过 Alpaca 下单（Notional 金额下单，支持实盘 / 模拟盘）
- **地缘政治监控**：15 路 RSS 实时监控（白宫、路透社、BBC、半岛电视台等），自动识别战争/制裁等 CRITICAL 事件
- **宏观情景检测**：自动识别降息/关税/经济衰退/地缘冲突等宏观事件并调整仓位策略
- **多数据源**：yfinance 新闻 + RSS + StockTwits + Reddit + AI 公司博客
- **数据压缩归档**：90 天滚动窗口，历史信号按周聚合压缩至 `signal_archives`，元数据自动清理
- **RL 反馈循环**：每次交易写入 `rl_training_data.jsonl`，持续积累训练数据
- **JWT 多用户**：安全认证，每用户独立持仓/设置/交易记录

---

## 📡 Data Sources and Acquisition Channels

AlphaTrader Pro utilizes multi-modal data inputs, continuously fetched in the background by automated daemon tasks:

1. **Market Data & Historical K-Lines**
   - **Channel**: Yahoo Finance (`yfinance` Python library).
   - **Content**: Real-time global stock prices, historical OHLCV data (for Kronos model input), and dozens of auto-calculated technical indicators (MACD, RSI, etc.).
   - **Mechanism**: Auto-polled every 2 minutes with staggered requests to prevent API rate limiting.
2. **Stock-Specific News & Company Updates**
   - **Channel**: Yahoo Finance News API and official AI company blog RSS feeds.
   - **Content**: Selected watchlist news summaries, major earnings releases, and industry trends.
   - **Mechanism**: Scanned automatically every 15 minutes.
3. **Retail Social Sentiment**
   - **Channel**: StockTwits and Reddit (e.g., r/wallstreetbets, r/stocks).
   - **Content**: Extraction of retail discussion volume and bullish/bearish emotion tags.
   - **Mechanism**: Polled via API or specific web scraping every 30 minutes.
4. **Geopolitical & Macroeconomic Events (Core Feature)**
   - **Channel**: 15 integrated top-tier global RSS feeds (White House, Reuters, BBC, Financial Times, etc.).
   - **Content**: Real-time capture of "CRITICAL" global macro events such as sudden wars, major sanctions, tariffs, or rate cuts.
   - **Mechanism**: High-frequency concurrent scanning every 10 minutes to trigger specific scenario playbooks and auto-execute trades on beneficiary assets.
5. **Real-World Trading Execution**
   - **Channel**: Alpaca Live API.
   - **Content**: A commission-free, API-native broker acting as the system's "execution arm".
   - **Mechanism**: Executes millisecond-level live/paper trades, strictly using Notional (dollar-amount) orders for maximum reliability.
6. **Daily Trading Experience & Feedback Loop**
   - **Channel**: Internal System Logs & Reinforcement Learning (RL) Data Collector.
   - **Content**: Extracted insights from daily profitable and losing trades, assessing why signals succeeded or failed.
   - **Mechanism**: Systematically archives execution records into `rl_training_data.jsonl` to form an ongoing feedback loop, fine-tuning future LLM trading logic.

---

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│                  信息层（后台持续运行）                │
│  yfinance → 价格/新闻/技术指标                        │
│  StockTwits/Reddit → 社交情绪                        │
│  RSS(Bloomberg/CNBC/AI博客) → 宏观+催化剂事件        │
│  地缘政治RSS(白宫/路透社/BBC/半岛等) → 战争/制裁事件  │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│                  分析层                               │
│  Kronos-base (GPU-7, A100 80GB)                      │
│    400根K线 → 预测未来5根蜡烛                         │
│  DeepSeek-R1 70B (Ollama, DRL70B:latest)             │
│    综合所有信息 → BUY/SELL/HOLD + 置信度              │
│  宏观情景引擎                                         │
│    地缘政治 + 经济数据 → 受益/回避股票列表             │
└──────────────────────┬──────────────────────────────┘
                       ↓
┌─────────────────────────────────────────────────────┐
│                  执行层                               │
│  Alpaca Live API → Notional金额下单（更可靠）          │
│  Paper Mode → 模拟交易                                │
│  SQLite → 持仓/交易/信号/归档数据                     │
└─────────────────────────────────────────────────────┘
```

---

## 环境要求

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.10（conda） | `alphatrader` conda 环境 |
| CUDA | 12.4+ | A100 GPU 运行 Kronos |
| Ollama | 任意版本 | 运行 DRL70B 模型 |
| GPU | A100 80GB × 1 | 推荐 GPU-7（空闲最多） |
| SQLite | 内置 | 无需单独安装 |

---

## 一次性安装（首次部署）

### 1. 克隆仓库

```bash
git clone https://github.com/14H034160212/AlphaTrader.git
cd /data/qbao775/AlphaTrader
```

### 2. 创建 conda 环境并安装依赖

```bash
conda create -n alphatrader python=3.10 -y

# 安装 PyTorch（CUDA 12.4）
/data/qbao775/miniconda3/envs/alphatrader/bin/pip install \
    torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124

# 安装所有项目依赖
/data/qbao775/miniconda3/envs/alphatrader/bin/pip install \
    numpy pandas \
    fastapi "uvicorn[standard]" \
    sqlalchemy \
    pydantic \
    "python-jose[cryptography]" \
    bcrypt \
    python-multipart \
    requests \
    yfinance \
    ta \
    feedparser \
    "alpaca-trade-api" \
    "alpha_vantage==2.3.1" \
    transformers \
    huggingface_hub \
    accelerate \
    sentencepiece \
    einops \
    safetensors \
    tqdm
```

### 3. 下载 Kronos 模型代码

```bash
cd /data/qbao775/AlphaTrader/kronos_lib
git clone https://github.com/shiyu-coder/Kronos.git .
```

### 4. 下载 Kronos 模型权重（HuggingFace）

```bash
/data/qbao775/miniconda3/envs/alphatrader/bin/python3 -c "
from huggingface_hub import snapshot_download
snapshot_download(
    repo_id='NeoQuasar/Kronos-base',
    local_dir='/data/qbao775/AlphaTrader/kronos_lib/weights/Kronos-base',
    ignore_patterns=['*.bin']
)
snapshot_download(
    repo_id='NeoQuasar/Kronos-Tokenizer-base',
    local_dir='/data/qbao775/AlphaTrader/kronos_lib/weights/Kronos-Tokenizer-base'
)
print('Done')
"
```

### 5. 安装 Ollama 并拉取 DeepSeek-R1 70B

```bash
# 安装 Ollama（如未安装）
curl -fsSL https://ollama.com/install.sh | sh

# 拉取 DeepSeek-R1 70B（约 42GB）
ollama pull DRL70B:latest

# 验证
ollama list
# 应看到：DRL70B:latest   42.5GB
```

### 6. 配置 systemd 自动启动

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/alphatrader.service << 'EOF'
[Unit]
Description=AlphaTrader Backend Service
After=network.target

[Service]
Type=simple
WorkingDirectory=/data/qbao775/AlphaTrader
ExecStart=/bin/bash /data/qbao775/AlphaTrader/start.sh
Restart=on-failure
RestartSec=5
StandardOutput=append:/tmp/alphatrader.log
StandardError=append:/tmp/alphatrader.log

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable alphatrader
```

---

## 日常启动 / 停止

### 启动服务

```bash
systemctl --user start alphatrader
```

### 停止服务

```bash
systemctl --user stop alphatrader
```

### 重启服务

```bash
systemctl --user restart alphatrader
```

### 查看服务状态

```bash
systemctl --user status alphatrader
```

### 查看实时日志

```bash
tail -f /tmp/alphatrader.log
```

### 验证服务正常

```bash
curl http://localhost:8000/api/health
# 返回：{"status":"ok","timestamp":"..."}
```

---

## 初次使用配置（Web UI）

访问 `http://<服务器IP>:8000`，进入设置页面：

| 设置项 | 推荐值 | 说明 |
|--------|--------|------|
| AI Provider | `本地 Ollama` | 使用 DRL70B（DeepSeek-R1 70B） |
| Alpaca API Key | 你的 Key | 实盘用 Live，测试用 Paper |
| Alpaca Secret Key | 你的 Secret | 同上 |
| Alpaca Mode | `live` / `paper` | `paper` = 模拟，`live` = 实盘 |
| Auto-Trading | `开启` | 置信度 ≥ 70% 自动下单 |
| Min Confidence | `0.70` | 最低置信阈值 |
| Risk Per Trade | `2.0%` | 每笔交易最大风险敞口 |

---

## 通过 API 快速配置（命令行）

```bash
# 获取 token
TOKEN=$(curl -s http://localhost:8000/api/auth/auto-login | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 配置 Alpaca 实盘
curl -s -X POST http://localhost:8000/api/settings \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"key":"alpaca_api_key","value":"YOUR_KEY"}'

curl -s -X POST http://localhost:8000/api/settings \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"key":"alpaca_secret_key","value":"YOUR_SECRET"}'

curl -s -X POST http://localhost:8000/api/settings \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"key":"alpaca_paper_mode","value":"false"}'

# 开启自动交易
curl -s -X POST http://localhost:8000/api/settings \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"key":"auto_trade_enabled","value":"true"}'

curl -s -X POST http://localhost:8000/api/settings \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"key":"ai_provider","value":"ollama"}'
```

---

## 项目文件结构

```
AlphaTrader/
├── start.sh                    # 启动脚本（含自动重启守护进程）
├── stop.sh                     # 停止脚本
├── rl_training_data.jsonl      # RL 强化学习训练数据（每次交易追加）
├── intelligence_attribution_report.json  # 信号归因分析报告
│
├── backend/
│   ├── main.py                 # FastAPI 主应用 + 所有后台任务
│   ├── auth.py                 # JWT 认证（bcrypt 直接调用）
│   ├── database.py             # SQLAlchemy 模型 + SQLite（含 SignalArchive）
│   ├── trading_engine.py       # 交易引擎（Alpaca Notional 下单 + 空头保护）
│   ├── market_data.py          # 行情获取 + 技术指标计算
│   ├── deepseek_ai.py          # DeepSeek-R1 / Ollama AI 分析
│   ├── kronos_analysis.py      # Kronos K 线预测（A100 GPU）
│   ├── news_intelligence.py    # 新闻获取 + 宏观情景检测 + 地缘政治 RSS
│   ├── social_sentiment.py     # StockTwits + Reddit 情绪扫描
│   ├── blog_monitor.py         # AI 公司博客 RSS 监控
│   ├── event_monitor.py        # 财报/宏观事件日历
│   ├── intelligence_feedback.py # RL 信号反馈与奖励计算
│   ├── rl_data_collector.py    # RL 训练数据收集器
│   ├── quant_models.py         # 量化模型（DCF/DDM/VPA）
│   ├── notifier.py             # 通知推送
│   ├── trading_platform.db     # SQLite 数据库
│   └── requirements.txt        # Python 依赖（参考，实际用 conda env）
│
├── frontend/
│   ├── index.html              # 单页应用主页面
│   ├── app.js                  # 前端逻辑（行情/交易/AI分析）
│   └── styles.css              # 深色主题样式
│
└── kronos_lib/                 # Kronos 模型（git clone 进来）
    ├── model/                  # Kronos 模型代码
    │   ├── __init__.py
    │   └── kronos.py           # KronosTokenizer, Kronos, KronosPredictor
    ├── prediction_results/     # Kronos 预测结果（JSON，>90天自动 gzip 归档）
    └── weights/
        ├── Kronos-base/        # 模型权重（HuggingFace 下载）
        └── Kronos-Tokenizer-base/  # 分词器权重
```

---

## 数据存储

| 数据 | 位置 | 说明 |
|------|------|------|
| 用户/持仓/交易/信号 | `backend/trading_platform.db` | SQLite，自动创建 |
| 信号周聚合归档 | `signal_archives`（同一 DB） | 90天前的信号压缩为周摘要 |
| 价格缓存 | 内存 | 重启后 ~2 分钟重建 |
| Kronos 预测结果 | `kronos_lib/prediction_results/` | JSON；>90天自动 gzip 压缩 |
| RL 训练数据 | `rl_training_data.jsonl` | JSONL 格式，持续追加 |
| 信号归因报告 | `intelligence_attribution_report.json` | 定期更新 |
| 服务日志 | `/tmp/alphatrader.log` | >200MB 时自动 gzip 轮转 |

**数据保留策略（90 天）**：每天 UTC 00:00 自动执行维护任务：
- AI 信号 >90 天 → 按 (用户, 股票, 周) 聚合写入 `signal_archives`，删除原始行
- Kronos JSON >90 天 → gzip 压缩，删除原文件
- 日志 >200MB → 保留最后 500 行摘要，gzip 旧日志，截断当前文件

---

## 后台任务说明

服务启动后自动运行以下后台循环：

| 任务 | 频率 | 说明 |
|------|------|------|
| `background_price_refresh` | 每 2 分钟 | 刷新价格缓存，错峰请求防限流 |
| `background_auto_trade_loop` | 持续 | 扫描自选股，AI 分析，自动下单 |
| `background_news_scan` | 每 15 分钟 | yfinance 新闻 + 宏观情景检测 |
| `background_news_scan`（地缘政治子任务） | 每 10 分钟 | 15路 RSS 地缘政治扫描，CRITICAL 事件自动触发受益股 AI 分析 |
| `background_event_scan` | 每 15 分钟 | 竞争威胁 + 催化剂识别 |
| `background_social_sentiment_scan` | 每 30 分钟 | StockTwits/Reddit 情绪 |
| `background_blog_scan` | 每 15 分钟 | AI 公司博客 RSS |
| `background_daily_summary` | 每天 | 日报摘要 |
| `background_pending_trade_executor` | 每分钟 | 执行挂单 |
| `_run_daily_maintenance` | 每天 UTC 00:00 | 信号压缩归档 + Kronos gzip + 日志轮转 |

---

## 地缘政治 RSS 监控

系统监控以下 15 个来源，实时检测战争、制裁、关税等 CRITICAL 宏观事件：

| 来源 | 说明 |
|------|------|
| 美国白宫 | whitehouse.gov 官方 RSS |
| 美国国务院 | state.gov 新闻发布 |
| 美国财政部 | treasury.gov 公告 |
| 路透社 | Reuters 顶级新闻 + 世界新闻 |
| BBC | BBC 世界新闻 |
| 半岛电视台 | Al Jazeera 英文 RSS |
| 卫报 | The Guardian 世界版 |
| NPR | NPR 国际新闻 |
| 金融时报 | FT 世界新闻 |
| 美联社 | AP 顶级头条 |
| 以色列时报 | Times of Israel |
| 耶路撒冷邮报 | Jerusalem Post |
| OilPrice.com | 石油市场新闻 |

### 已内置宏观情景

| 情景 | 严重级别 | 受益标的 | 回避标的 |
|------|----------|----------|----------|
| `middle_east_war_2026` | CRITICAL | GLD, IAU, SLV, XOM, LMT, RTX, NOC | TSLA, AMZN, AAPL, QQQ, TQQQ, SOXL |
| `fed_rate_cut` | HIGH | QQQ, ARKK, TSLA, NVDA, AMZN | GLD（部分） |
| `tariff_war` | HIGH | 国内制造、农业 | 进出口依赖股 |
| `recession_fears` | HIGH | GLD, TLT | 周期性股票 |

当检测到 CRITICAL/HIGH 情景时，系统自动对受益股票触发 AI 分析，置信度 ≥ 70% 时自动下单。

---

## Alpaca 下单说明

系统使用 **Notional（金额）下单** 代替 qty（数量）下单，原因：

- Alpaca 对碎股数量有最小单位限制，qty 方式小额订单常被取消
- Notional 方式（如 `notional=18.00`）直接指定花费金额，可靠性更高
- 最小下单金额：$1.00

**空头保护**：卖出前系统自动向 Alpaca 验证是否持有该股票，若 Alpaca 端无持仓则跳过卖出，防止意外触发裸卖空导致订单被拒。

---

## 故障排查

### 服务无法启动

```bash
# 查看详细错误
tail -50 /tmp/alphatrader.log

# 检查端口占用
ss -tlnp | grep 8000

# 手动测试启动
cd /data/qbao775/AlphaTrader/backend
/data/qbao775/miniconda3/envs/alphatrader/bin/python3 -c "
import uvicorn
uvicorn.run('main:app', host='0.0.0.0', port=8000)
"
```

### Kronos 加载失败（CUDA OOM）

```bash
# 检查 GPU 内存使用
nvidia-smi --query-gpu=index,memory.used,memory.free --format=csv

# 修改 start.sh，选择更空闲的 GPU
# 将 CUDA_VISIBLE_DEVICES=7 改为其他空闲 GPU 编号
```

### Yahoo Finance 限流（Too Many Requests）

价格刷新已设置 1.5s 延迟 + 2 分钟间隔，一般不会触发。
若仍限流，可临时增加 `start.sh` 中的刷新间隔。

### Alpaca 卖出被拒（not allowed to short）

系统已内置空头保护逻辑，卖出前自动验证 Alpaca 持仓。若仍报错：

```bash
# 检查本地持仓与 Alpaca 实际持仓是否一致
curl -s http://localhost:8000/api/positions -H "Authorization: Bearer $TOKEN"

# 通过 Alpaca API 查询真实持仓
curl -s https://api.alpaca.markets/v2/positions \
    -H "APCA-API-KEY-ID: YOUR_KEY" \
    -H "APCA-API-SECRET-KEY: YOUR_SECRET"
```

### Alpaca 买入订单被取消

系统已改为 Notional 金额下单，正常情况下不会被取消。若仍被取消：
- 检查账户余额是否足够（最低 $1）
- 检查股票是否支持碎股交易（部分 OTC 股票不支持）

### Ollama 无响应

```bash
# 检查 Ollama 进程
ps aux | grep ollama

# 验证模型可用
curl http://localhost:11434/api/tags

# 重启 Ollama
pkill ollama && ollama serve &
```

---

## 免责声明

本项目仅供学习和实验用途。AI 交易信号不构成投资建议，开发者不对任何交易损失负责。在使用实盘模式前，请确保已充分了解相关风险并在模拟盘中验证策略。
