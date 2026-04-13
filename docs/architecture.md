# AlphaTrader Architecture

## System Overview

```mermaid
graph TB
    subgraph DATA["<b>信息采集层</b>"]
        YF[Yahoo Finance<br/>报价/K线/新闻]
        RSS[15个RSS源<br/>Reuters/BBC/白宫]
        SS[StockTwits/Reddit<br/>散户情绪]
        KRONOS[Kronos GPU模型<br/>K线预测 5根]
        QUANT[DCF/DDM估值<br/>内在价值计算]
        GC[全球市场<br/>VIX/指数/汇率]
        COT[CFTC COT<br/>期货持仓]
    end

    subgraph AI["<b>AI 大脑层</b>"]
        PROMPT[超级Prompt构建<br/>报价+技术+估值+新闻<br/>+情景+情绪+预测+教训]
        LLM[Ollama DRL70B / DeepSeek-R1]
        SIGNAL[JSON信号输出<br/>BUY/SELL/HOLD<br/>confidence + target + stop]
    end

    subgraph FILTER["<b>风控过滤层</b>"]
        F1[跳空过滤<br/>今日+3%不追]
        F2[熊市过滤<br/>SPY低于MA20不买]
        F3[冷却过滤<br/>3天内止损不重买]
        F4[开盘过滤<br/>休市不交易]
        F5[Kelly仓位<br/>半Kelly+VIX缩放]
    end

    subgraph EXEC["<b>执行层</b>"]
        ENGINE[TradingEngine<br/>自动选券商]
        ALP[Alpaca<br/>美股]
        FUTU[富途<br/>A股/港股]
        IBKR[IBKR<br/>全球]
        PAPER[Paper<br/>模拟]
    end

    subgraph FEEDBACK["<b>反馈学习层</b>"]
        RL[RL训练数据<br/>232MB+ JSONL]
        EMAIL[每日邮件日报<br/>盈亏/胜率]
        ARCHIVE[信号归档<br/>90天压缩]
    end

    subgraph SCENARIO["<b>宏观情景引擎</b>"]
        SCAN[关键词扫描<br/>10分钟一次]
        AIREV[AI审查<br/>6小时一次]
        DB[(scenario_states<br/>SQLite)]
        LIFE[ACTIVE → DECLINING → RESOLVED]
    end

    YF --> PROMPT
    RSS --> PROMPT
    RSS --> SCAN
    SS --> PROMPT
    KRONOS --> PROMPT
    QUANT --> PROMPT
    GC --> PROMPT
    COT --> PROMPT

    SCAN --> DB
    AIREV --> DB
    DB --> LIFE
    DB --> PROMPT

    PROMPT --> LLM
    LLM --> SIGNAL

    SIGNAL --> F1
    F1 --> F2
    F2 --> F3
    F3 --> F4
    F4 --> F5

    F5 --> ENGINE
    ENGINE --> ALP
    ENGINE --> FUTU
    ENGINE --> IBKR
    ENGINE --> PAPER

    ENGINE --> RL
    ENGINE --> EMAIL
    ENGINE --> ARCHIVE
    RL -.->|历史教训| PROMPT

    style DATA fill:#1a1a2e,stroke:#16213e,color:#e8e8e8
    style AI fill:#0f3460,stroke:#16213e,color:#e8e8e8
    style FILTER fill:#533483,stroke:#16213e,color:#e8e8e8
    style EXEC fill:#e94560,stroke:#16213e,color:#e8e8e8
    style FEEDBACK fill:#1a5276,stroke:#16213e,color:#e8e8e8
    style SCENARIO fill:#1e8449,stroke:#16213e,color:#e8e8e8
```

## Background Loops

```mermaid
graph LR
    subgraph LOOPS["9个后台定时循环"]
        L1["auto_trade_loop<br/>⏱ 1小时<br/>核心交易循环"]
        L2["news_scan<br/>⏱ 10分钟<br/>突发新闻+情景"]
        L3["stop_loss_monitor<br/>⏱ 15秒<br/>止损监控"]
        L4["global_market_scan<br/>⏱ 5分钟<br/>VIX/指数更新"]
        L5["social_sentiment<br/>⏱ 30分钟<br/>散户情绪"]
        L6["blog_scan<br/>⏱ 30分钟<br/>AI公司博客"]
        L7["event_scan<br/>⏱ 10分钟<br/>财报/并购"]
        L8["pending_executor<br/>⏱ 5分钟<br/>延时订单"]
        L9["email_reporter<br/>⏱ 每日收盘<br/>邮件日报"]
    end

    style L1 fill:#e94560,color:#fff
    style L3 fill:#e94560,color:#fff
```

## Trading Loop Detail

```mermaid
sequenceDiagram
    participant Loop as auto_trade_loop
    participant MD as market_data
    participant NI as news_intelligence
    participant KR as Kronos GPU
    participant AI as LLM (70B)
    participant FLT as 5道过滤器
    participant ENG as TradingEngine
    participant DB as SQLite

    Loop->>Loop: 每1小时触发
    loop 遍历观察列表每只股票
        Loop->>MD: 获取报价+K线+技术指标+新闻
        Loop->>NI: 扫描竞争威胁+利好催化剂
        Loop->>KR: K线预测(下5根)
        Loop->>Loop: 构建超级Prompt
        Loop->>AI: 发送分析请求
        AI-->>Loop: {signal, confidence, target, stop}
        Loop->>DB: 存储AI信号
        Loop->>FLT: 过滤检查
        alt 全部通过
            FLT->>ENG: 执行交易
            ENG->>DB: 记录交易
        else 被过滤
            FLT-->>Loop: 跳过
        end
    end
```

## Scenario Lifecycle

```mermaid
stateDiagram-v2
    [*] --> ACTIVE: 关键词首次匹配 / AI创建
    ACTIVE --> ACTIVE: 持续发现证据 (evidence+1)
    ACTIVE --> DECLINING: 3小时无证据 / 解决关键词≥2
    DECLINING --> ACTIVE: 新证据出现 (20分钟冷却后)
    DECLINING --> RESOLVED: 解决关键词≥4 / AI判定已解决
    DECLINING --> EXPIRED: 12小时无任何证据
    ACTIVE --> RESOLVED: 解决关键词≥4 / AI审查(每6h)
    RESOLVED --> [*]
    EXPIRED --> [*]
```

## Tech Stack

```mermaid
graph LR
    subgraph Backend
        FastAPI --> SQLAlchemy --> SQLite
    end
    subgraph AI
        Ollama["Ollama (DRL70B)"]
        DeepSeek["DeepSeek-R1 API"]
        Kronos["Kronos (A100 GPU)"]
    end
    subgraph Brokers
        Alpaca
        Futu["富途"]
        IBKR
    end
    subgraph Frontend
        HTML/JS/CSS --> WebSocket
        WebSocket --> TradingView["TradingView Charts"]
    end
    subgraph Deploy
        systemd --> HPC["大学HPC服务器"]
    end
```
