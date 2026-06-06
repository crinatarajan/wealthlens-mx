# 💰 WealthLens MX — AI-Powered Personal Finance Dashboard

<div align="center">

![WealthLens Banner](https://img.shields.io/badge/WealthLens-MX-C9A84C?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHBhdGggZmlsbD0iI0M5QTg0QyIgZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6Ii8+PC9zdmc+)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-2.x-000000?style=for-the-badge&logo=flask&logoColor=white)
![AI Powered](https://img.shields.io/badge/AI-Groq%20%7C%20LLaMA%203.3-FF6B35?style=for-the-badge&logo=anthropic&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-Database-003B57?style=for-the-badge&logo=sqlite&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

**A full-stack AI financial advisor for Mexican investors — tracks CEDEs, stocks, crypto & goals with live LLM-powered insights.**

[🚀 Live Demo](#) · [📖 API Docs](#api-reference) · [🐛 Report Bug](issues) · [✨ Request Feature](issues)

</div>

---

## ✨ Features

| Feature | Description |
|---|---|
| 🧠 **AI Financial Advisor** | LLM-powered chat using Groq (LLaMA 3.3 70B) with your real portfolio context |
| 📊 **Smart Dashboard** | Dynamic KPIs, income/expense charts, and asset allocation donut |
| 🏦 **CEDE Rate Tracker** | Live Mexican bank deposit rates (Banregio, Inbursa, Banbajío, HSBC, Santander) |
| 📈 **Market Data** | Real-time stock prices via Yahoo Finance (MX + US markets) |
| 🪙 **Crypto Tracker** | Live BTC, ETH, SOL prices in MXN via CoinGecko |
| 🎯 **Goal Tracking** | Visual progress tracking for financial goals with deadlines |
| 💬 **AI Chat History** | Persistent conversation history stored in SQLite |
| 📥 **Transaction Import** | CSV/Excel import with auto-categorization |
| 🔒 **Auth System** | Secure login with hashed passwords (Werkzeug) |
| 🌐 **Bilingual** | Full English & Spanish support (EN/ES) |
| 🔌 **REST API** | 8+ documented endpoints with interactive testing dashboard |

---

## 🖥️ Screenshots

### 📊 Financial Dashboard
![Dashboard](screenshots/01_dashboard.png)
*KPI cards, income vs expenses chart, asset allocation donut, and recent transactions*

### 💼 Portfolio Breakdown
![Portfolio](screenshots/02_portfolio.png)
*Full asset table across CEDEs, stocks (MX + US), crypto, and FIBRAs*

### 📈 Market Data
![Market Data](screenshots/03_market_data.png)
*Live Mexican bank CEDE rates, stock prices via Yahoo Finance, and crypto via CoinGecko*

### 🧠 AI Financial Analysis
![AI Analysis](screenshots/04_ai_analysis.png)
*LLM-generated health score, insights, and ranked recommendations — refreshable on demand*

### 💬 AI Advisor Chat
![AI Chat](screenshots/05_ai_advisor.png)
*Conversational advisor with full portfolio context, suggested questions, and quick chips*

### 🔌 API Dashboard — Endpoints
![API Endpoints](screenshots/06_api_dashboard.png)
*Interactive endpoint explorer with one-click testing and live JSON responses*

### 🧪 API Playground
![API Playground](screenshots/07_api_playground.png)
*Custom request builder — select endpoint, edit JSON body, see response*

### 🗄️ Database Schema
![DB Schema](screenshots/08_api_schema.png)
*Full SQLite schema viewer — all 8 tables with CREATE TABLE statements*

### 📱 Mobile View
![Mobile](screenshots/09_mobile_dashboard.png)
*Responsive layout with hamburger nav — works on any screen size*

---

## 🏗️ Architecture

```
wealthlens-mx/
│
├── app.py                  # Flask backend — all routes & AI logic
├── wealthlens_showcase.html # Standalone HTML demo (no backend needed)
├── wealthlens.db           # SQLite database (auto-created on first run)
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
│
├── templates/
│   ├── wealthlens_demo.html # Main dashboard UI
│   ├── api_dashboard.html   # API explorer
│   ├── login.html
│   └── register.html
│
└── uploads/                # User-uploaded CSV/Excel files
```

### Tech Stack

- **Backend:** Python 3.10+, Flask 2.x, SQLite (WAL mode)
- **AI Layer:** Groq API (LLaMA 3.3 70B Versatile) — swappable to DeepSeek or Ollama
- **Market Data:** Yahoo Finance API (stocks), CoinGecko (crypto)
- **Auth:** Werkzeug password hashing, Flask sessions
- **Frontend:** Vanilla JS, CSS custom properties, Playfair Display + DM Sans fonts
- **Deployment-ready:** Environment variable config, no hardcoded secrets

---

## ⚡ Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/wealthlens-mx.git
cd wealthlens-mx
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required — get a free key at console.groq.com
GROQ_API_KEY=gsk_your_key_here

# Optional — override AI model
AI_MODEL=llama-3.3-70b-versatile

# Optional — use DeepSeek or another OpenAI-compatible gateway
# AI_BASE_URL=https://api.deepseek.com/v1/chat/completions
# DEEPSEEK_API_KEY=your_key_here

# Flask session security — change this in production!
SECRET_KEY=change-me-in-production
```

### 5. Run the app

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) — register an account and start tracking!

> **No API key?** Use `wealthlens_showcase.html` — open it directly in your browser for a fully interactive demo without any backend.

---

## 🔌 API Reference

All endpoints require authentication (session cookie). Base URL: `http://localhost:5000`

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/status` | Health check, AI gateway status, version |
| `GET` | `/api/wealth/summary` | Total assets, goals, 30-day cash flow |
| `GET` | `/api/market/deposits` | CEDE rates from Mexican banks |
| `GET` | `/api/market/stocks?symbols=SPY,AMXL.MX` | Stock prices via Yahoo Finance |
| `GET` | `/api/market/crypto?ids=bitcoin,ethereum` | Crypto prices in USD & MXN |
| `POST` | `/api/ai/dashboard` | AI-generated financial report (JSON) |
| `POST` | `/api/chat` | AI advisor chat with context |
| `GET` | `/api/chat/history` | Stored conversation history |

**Example — AI Chat:**
```bash
curl -X POST http://localhost:5000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question": "What is my biggest financial risk?"}'
```

**Example Response:**
```json
{
  "ok": true,
  "answer": "Based on your portfolio, your biggest risk is crypto concentration at 17%...",
  "demo": false
}
```

---

## 🤖 AI Configuration

WealthLens uses an **OpenAI-compatible gateway** — swap providers with one `.env` change:

| Provider | Speed | Cost | `.env` setting |
|---|---|---|---|
| **Groq** *(default)* | ⚡ Ultra-fast | Free tier | `AI_BASE_URL=https://api.groq.com/openai/v1/chat/completions` |
| **DeepSeek** | Fast | Very cheap | `AI_BASE_URL=https://api.deepseek.com/v1/chat/completions` |
| **Ollama** *(local)* | Moderate | Free | `AI_BASE_URL=http://localhost:11434/v1/chat/completions` |
| **OpenRouter** | Variable | Pay-per-use | `AI_BASE_URL=https://openrouter.ai/api/v1/chat/completions` |

---

## 🗄️ Database Schema

```sql
users          — Auth, currency preference, language (EN/ES)
assets         — Portfolio holdings with MXN value
goals          — Financial goals with progress tracking
transactions   — Income & expense ledger (CSV importable)
budgets        — Monthly category limits
recurring      — Subscription & recurring income tracking
alerts         — Smart financial alerts
chat_history   — AI conversation history
```

---

## 🚀 Deployment

### Render (free tier)

1. Push to GitHub
2. New Web Service → connect repo
3. Build: `pip install -r requirements.txt`
4. Start: `gunicorn app:app`
5. Add environment variables in the Render dashboard

### Railway

```bash
railway init
railway add
railway up
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8000"]
```

---

## 📋 requirements.txt

```
flask>=2.3
werkzeug>=2.3
requests>=2.31
python-dotenv>=1.0
gunicorn>=21.0
```

---

## 🗺️ Roadmap

- [ ] Plaid/Belvo bank account sync (open banking)
- [ ] PDF statement parsing with AI categorization
- [ ] Push notifications for goal milestones
- [ ] Multi-currency rebalancing calculator
- [ ] Tax optimization module (ISR/SAT Mexico)
- [ ] Mobile PWA support

---

## 🤝 Contributing

Pull requests are welcome! For major changes, open an issue first to discuss what you'd like to change.

1. Fork the repo
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 👤 Author

**Your Name**
- LinkedIn: [linkedin.com/in/yourprofile](https://linkedin.com/in/yourprofile)
- GitHub: [@yourusername](https://github.com/yourusername)
- Email: your@email.com

---

<div align="center">
  <sub>Built with ❤️ and ☕ · If this helped you, please ⭐ the repo!</sub>
</div>
