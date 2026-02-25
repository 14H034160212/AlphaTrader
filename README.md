# AlphaTrader Pro 📈🤖

![AlphaTrader](/frontend/assets/logo.png) <!-- (Optional: Add your own logo/screenshot here later) -->

**AlphaTrader Pro** is an autonomous, AI-driven quantitative stp built with a Python/FastAPI backend and a pure HTML/JS frontend. It seamlessly integrates real-time global market data with advanced local AI models (like DeepSeek-R1 via Ollama) to automatically analyze stock charts, technical indicators, and news, ultimately executing trades via the Alpaca Live Brokerage API.

## 🌟 Project Background

The project was conceived as an advanced homelab experiment to transition from manual execution to a fully automated, *"set-it-and-forget-it"* investment pipeline. By pairing **DeepSeek-R1's** industry-leading reasoning capabilities with the **Alpaca API**, the system acts as a 24/7 autonomous hedge fund manager.

Additionally, AlphaTrader natively integrates with the **OpenClaw multi-modal AI Gateway**, allowing the user to interact with their trading agent via WhatsApp or Telegram for on-the-go portfolio summaries and ad-hoc stock analysis.

## ✨ Core Features

*   **🌍 Global Market Tracking:** Real-time data feeds for major indices (S&P 500, NASDAQ, Nikkei, etc.) using Yahoo Finance.
*   **👥 Multi-User Support:** Built-in transition from a single-user system to a robust multi-tenant platform. Support for registrations, secure logins, and data isolation.
*   **🔒 JWT Authentication:** Secure session management with JSON Web Tokens. Every trade and portfolio action is user-scoped.
*   **🧠 Local AI Brain (Free & Private):** Full integration with local instances of **DeepSeek-R1 (14B)** via Ollama. 
*   **🤖 Autonomous Trade Execution:** A background scheduler iterates over all registered users, performing personalized analysis and execution based on individual settings.
*   **🏦 Alpaca Live Brokerage:** Users can independently link their own Alpaca Paper or Live keys.
*   **💰 Simulation Wallet:** Integrated simulation fund management. New users start with a $100,000 virtual balance and can perform "Simulated Transfers" via the UI.
*   **📱 WhatsApp/Telegram Remote Control:** Built-in webhook endpoints for OpenClaw integration.

---

## 🏗️ Architecture Stack

*   **Backend:** Python 3.8, FastAPI, Uvicorn, SQLite (SQLAlchemy), JWT (`python-jose`)
*   **Authentication:** Bcrypt password hashing, JWT Authorization headers
*   **Data Providers:** `yfinance` (Quotes/News), `alpaca-trade-api` (Execution)
*   **AI Inference:** Ollama Engine (local gguf models) or DeepSeek Official Cloud API
*   **Frontend:** Vanilla JS (SPA), Dark Theme CSS, TradingView Charts
*   **Remote Messaging:** OpenClaw Agent Gateway

---

## 🚀 Deployment & Installation Guide

### 1. Prerequisites
Ensure you have the following installed on your Linux server:
*   Python 3.8+
*   [Ollama](https://ollama.com/) (Must have `deepseek-r1:14b` pulled if using local AI)
*   An Alpaca Markets account (Paper or Live)

### 2. Clone the Repository
```bash
git clone https://github.com/14H034160212/AlphaTrader.git
cd AlphaTrader
```

### 3. Install Python Dependencies
```bash
pip install -r backend/requirements.txt
```

*(Note: Ensure you have `fastapi`, `uvicorn`, `yfinance`, `sqlalchemy`, and `alpaca-trade-api` installed)*

### 4. Configure the Local AI (DeepSeek-R1 with Tools)
To enable tool-calling for DeepSeek-R1 in OpenClaw, create a custom Modelfile:
```bash
cat << 'EOF' > Modelfile.deepseek-r1-tools
FROM deepseek-r1:14b
TEMPLATE """{{- if .System }}<|start_header_id|>system<|end_header_id|>
{{ .System }}<|eot_id|>{{- end }}
{{- if .Tools }}<|start_header_id|>tools<|end_header_id|>
{{ .Tools }}<|eot_id|>{{- end }}
{{- range $i, $_ := .Messages }}
{{- if eq .Role "user" }}<|start_header_id|>user<|end_header_id|>
{{ .Content }}<|eot_id|>
{{- else if eq .Role "assistant" }}<|start_header_id|>assistant<|end_header_id|>
{{- if .ToolCalls }}
<tool_calls>
{{- range .ToolCalls }}<tool_call>
{"name": "{{ .Function.Name }}", "arguments": {{ .Function.Arguments }}}
</tool_call>{{- end }}
</tool_calls>
{{- else }}{{ .Content }}{{- end }}<|eot_id|>
{{- else if eq .Role "tool" }}<|start_header_id|>tool<|end_header_id|>
<tool_response>
{"name": "{{ .Name }}", "content": {{ .Content }}}
</tool_response><|eot_id|>
{{- end }}
{{- end }}<|start_header_id|>assistant<|end_header_id|>
"""
EOF

ollama create deepseek-r1-tools:14b -f Modelfile.deepseek-r1-tools
```

### 5. Start the Trading Engine Backend
Move into the `backend` directory and start the server:
```bash
cd backend
nohup python3.8 main.py > server.log 2>&1 &
```

*(Note: The server includes a built-in uvicorn wrapper with auto-reloader enabled. Use `python3.8` for best compatibility with installed dependencies.)*

### 6. Access the Dashboard
Open your web browser and navigate to your server's IP:
```
http://<YOUR_SERVER_IP>:8000
```

---

## ⚙️ Configuration (The UI Settings)

1. Click the **⚙️ Settings** icon in the bottom left of the Dashboard.
2. **AI Provider:** Select "本地 Ollama".
3. **Alpaca Keys:** Input your Alpaca `API_KEY` and `SECRET_KEY`.
4. **Auto-Trading:** Toggle on "Enable AI Auto-Trading" to allow the background scheduler to fire orders on your behalf.
5. Hit **Save**. The engine is now live.

## 🤝 OpenClaw WhatsApp Integration (Optional)

To receive hourly portfolio updates on your phone:
1. Follow the [OpenClaw setup instructions](https://docs.openclaw.ai) to initialize your gateway.
2. Link your WhatsApp: `openclaw channels login`
3. Point your OpenClaw custom skills endpoint to the AlphaTrader webhook:
   `http://127.0.0.1:8000/api/openclaw/webhook`

## ⚠️ Disclaimer
This software is built for educational and homelab experimental purposes. The creator is not responsible for any financial losses incurred from deploying this autonomous bot in a live market environment. Always paper-trade before throwing real money into the void.
