# IBKR Live Gateway 接入步骤

> 状态：账户已开通 (2026-05-14)，待 Gateway 安装 + DB settings 写入。
> 模式：**Live**（用户选择直接 live，不跑 paper）

## 1. 下载并安装 IB Gateway（建议优先于 TWS）

IB Gateway 比 TWS 轻量、无图形界面、适合 server 部署。

```bash
# Stable 版本（生产推荐）
wget -O /tmp/ibgateway.sh https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh
bash /tmp/ibgateway.sh   # 安装到 /opt/ibgateway 或 ~/Jts
```

安装完成后，启动 GUI 一次完成首次登录（live 账户的 username/password）。建议同步勾选 "Use SSL"。

## 2. 配置 API 端口（live 模式）

启动 Gateway → Configure → Settings → API → Settings:

- **Read-Only API**: ❌ unchecked（要能下单）
- **Socket port**: `4001`（Gateway live；TWS live 是 `7496`）
- **Trusted IPs**: `127.0.0.1` （如果 Gateway 跟 backend 同机）
- **Master API client ID**: 留空
- **Allow connections from localhost only**: ✅ checked
- **Bypass Order Precautions for API Orders**: ❌ 安全起见保持 unchecked

## 3. 无人值守：用 IBC 自动登录

Gateway 默认会话有效期上限 24h（live 模式还有每周末强制重启逻辑）。无人值守必须配 [IBC](https://github.com/IbcAlpha/IBC)：

```bash
git clone https://github.com/IbcAlpha/IBC.git /opt/ibc
# 配置 /opt/ibc/config.ini:
#   IbLoginId=<your_live_username>
#   IbPassword=<your_live_password>
#   FIX=no
#   TradingMode=live
#   IbDir=/opt/ibgateway
# 创建 systemd unit 让 IBC 守护 Gateway 进程
```

systemd unit 参考（可放到 `~/.config/systemd/user/ibgateway.service`）：

```ini
[Unit]
Description=IBKR Gateway via IBC
After=network.target

[Service]
Type=simple
ExecStart=/opt/ibc/scripts/ibcstart.sh 1030 -gateway --mode=live
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

## 4. 写入 DB settings

Gateway 跑起来并通过 healthcheck 后，执行：

```bash
cd /data/qbao775/AlphaTrader/backend
python3 << 'EOF'
import sqlite3
conn = sqlite3.connect("trading_platform.db")
cur = conn.cursor()
USER_ID = 1
for key, val in [
    ("ibkr_enabled",   "true"),
    ("ibkr_host",      "127.0.0.1"),
    ("ibkr_port",      "4001"),      # Gateway live
    ("ibkr_client_id", "10"),
    ("ibkr_account",   ""),          # 留空走默认账户
]:
    cur.execute(
        "INSERT INTO settings(user_id,key,value) VALUES(?,?,?) "
        "ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value",
        (USER_ID, key, val),
    )
conn.commit()
conn.close()
print("IBKR settings written.")
EOF
```

## 5. 验证

重启 backend 后查看 `/tmp/alphatrader.log`：

```
[IBKR] ib_insync loaded. Target: 127.0.0.1:4001 clientId=10
```

然后用一个小金额的 HK 测试单（例如 100 股低价港股）确认链路：

```bash
curl -X POST http://localhost:8888/api/trade/manual \
  -H "Content-Type: application/json" \
  -d '{"symbol":"0700.HK","side":"BUY","quantity":1,"price":350.0}'
```

确认 IBKR 端能看到 working order。

## 6. 常见陷阱

- **clientId 冲突**：如果同账户其他客户端（TWS GUI、其它脚本）用了 clientId=10，AlphaTrader 会断连。换个数字即可。
- **Live vs Paper 端口搞错**：`4001` live / `4002` paper（Gateway），`7496` live / `7497` paper（TWS）。
- **周末重启**：IBC 默认配置每周日强制 logout，需在 `config.ini` 设置 `IbAutoClosedown=no` 才能 7×24 跑。
- **港股交易时段**：HK 早盘 09:30-12:00 + 午盘 13:00-16:00 (HKT)。在收盘前最后 10 分钟不要主动下单。
