# 🚀 Elite AI Coding Assistant Telegram Bot
Yeh ek high-performance, production-ready AI Coding Assistant Telegram Bot hai jo FastAPI, SQLAlchemy, Supabase PostgreSQL, aur Google Gemini API ka upyog karta hai.
## 📁 Repository Structure
```text
telegram_ai_bot/
├── requirements.txt         # Dependencies
├── render.yaml              # Render deployment template
├── Dockerfile               # Container configurations
├── src/
│   ├── main.py              # Main FastAPI & Webhook entrypoint
│   ├── config/
│   │   └── settings.py      # App configurations
│   ├── database/
│   │   ├── connection.py    # Database async sessions
│   │   ├── models.py        # SQLAlchemy models
│   │   └── repository.py    # Database CRUD operations
│   ├── errors/
│   │   └── handlers.py      # Custom exceptions
│   ├── services/
│   │   └── ai_engine.py     # Gemini AI interface
│   └── telegram/
│       ├── commands.py      # /start, /help, /clear, /newproject handlers
│       ├── handlers.py      # Direct chat messages & /Do handler
│       └── middleware.py    # Rate limiting & automatic user onboarding

```
## ⚙️ Environment Variables Config (Render me settings)
 * ENVIRONMENT: production
 * LOG_LEVEL: INFO
 * SECRET_KEY: (Koi bhi random password/hex string)
 * TELEGRAM_BOT_TOKEN: (Aapke @BotFather se mila token)
 * WEBHOOK_URL: https://your-service-name.onrender.com (Aapka Render Web Service URL)
 * WEBHOOK_SECRET_TOKEN: (Koi bhi secret password header verify karne ke liye)
 * DATABASE_URL: postgresql+asyncpg://<username>:<password>@<host>:5432/postgres (Supabase Connection Uri)
 * GEMINI_API_KEY: (Google AI Studio se mili API key)
 
