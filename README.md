# 🤖 Procurement Bot

> A multi-tenant procurement management system built on Telegram — designed for small and medium-sized businesses.

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Aiogram](https://img.shields.io/badge/Aiogram-3.x-2CA5E0?logo=telegram&logoColor=white)](https://docs.aiogram.dev/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.x-D71F00?logo=sqlalchemy&logoColor=white)](https://www.sqlalchemy.org/)
[![Alembic](https://img.shields.io/badge/Alembic-Migrations-6BA539)](https://alembic.sqlalchemy.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Status](https://img.shields.io/badge/Status-v3.0--alpha-orange)]()
[![CI](https://github.com/ghassonshadi5-tech/procurement-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/ghassonshadi5-tech/procurement-bot/actions/workflows/ci.yml)
---

## 📌 Overview

**Procurement Bot** streamlines the entire procurement workflow inside small and medium-sized enterprises — from the moment an employee registers a need, through manager approval, to sending RFQs (Request for Quotations) and comparing supplier offers — all inside Telegram.

The system is built around a **multi-tenant architecture** with strict data isolation enforced at the database level via **Row-Level Security (RLS)**, ensuring that each company's data (requests, offers, prices, attachments, reports, activity logs) remains completely isolated from other tenants.

> ⚠️ **Status:** `v3.0-alpha` — Active development. Not production-ready.

---

## ✨ Key Features

- 🏢 **Multi-Tenant Architecture** — Full tenant isolation enforced via PostgreSQL RLS.
- 📝 **Purchase Requests** — Structured creation, review, approval, and rejection workflow.
- 💬 **RFQ System** — Directed or open Request for Quotations with configurable deadlines.
- 🌍 **Multi-Language Support** — Arabic (default), Russian, and English via i18n JSON files.
- 👥 **Role-Based Access Control** — Super Admin, Company Owner, Manager, Requester, and more.
- 🔐 **Security-First Design** — Fail-fast configuration, no hardcoded secrets, strict permission checks.
- 📊 **Audit & Traceability** — All critical actions (approvals, decisions, price changes) are logged.
- ⚙️ **Async by Default** — Built on `asyncio` with `asyncpg` for database operations.
- 🗂️ **Migrations via Alembic** — Versioned, reversible schema changes.

---





## 📸 Screenshots

> 🚧 Coming soon — screenshots of the bot will be added here.

<!--
When you have screenshots, save them in `docs/screenshots/` and replace
the block below with:

<p align="center">
  <img src="docs/screenshots/main-menu.png" alt="Main Menu" width="300">
  <img src="docs/screenshots/purchase-request.png" alt="Purchase Request" width="300">
</p>
-->


---

## 🏗️ Tech Stack

| Layer | Technology |
|-------|-----------|
| **Language** | Python 3.11+ |
| **Bot Framework** | Aiogram 3.x |
| **Database** | PostgreSQL 15+ |
| **ORM** | SQLAlchemy 2.x (async) |
| **Migrations** | Alembic |
| **Validation** | Pydantic v2 / pydantic-settings |
| **Scheduler** | APScheduler |
| **i18n** | Custom JSON-based translation system |

---

## 🏛️ Architecture Highlights

### Multi-Tenancy with RLS
Every query is scoped to a `company_id`. PostgreSQL Row-Level Security policies prevent cross-tenant data access at the database engine level — not just in application code.

### Layered Design

```text
┌─────────────────────────────────────┐
│  Telegram Handlers / Routers        │  ← Presentation
├─────────────────────────────────────┤
│  Services (Business Logic)          │  ← Domain
├─────────────────────────────────────┤
│  Models / Repositories              │  ← Data Access
├─────────────────────────────────────┤
│  PostgreSQL (RLS-enforced)          │  ← Persistence
└─────────────────────────────────────┘
```

### Fail-Fast Configuration
`app/config/settings.py` validates all required environment variables at startup. Missing or insecure values (e.g., `DEBUG=True` in production) cause the application to refuse to boot.

---

## 📁 Project Structure

```text
procurement-bot/
├── main.py                     # Entry point
├── init_db.py                  # Database initialization
├── reset_db.py                 # Database reset
├── test_request_service.py     # Test file
├── requirements.txt
├── alembic.ini                 # Alembic migration settings
├── .env.example
├── README.md
├── التوثيق/                    # Project documentation (Word/Excel docs)
├── migrations/                 # Alembic migrations
└── app/
    ├── bot/
    │   ├── handlers/           # Telegram handlers (routers)
    │   ├── keyboards/          # Inline & reply keyboards
    │   └── middleware/         # User context, i18n, permissions
    ├── config/
    │   └── settings.py         # Pydantic settings (fail-fast)
    ├── core/
    │   ├── permissions.py      # RBAC enforcement
    │   ├── i18n.py             # Translation loader
    │   └── logger.py           # Structured logging
    ├── database/
    │   ├── models/             # SQLAlchemy ORM models
    │   └── database.py         # Engine, session, RLS context
    ├── locales/                # ar.json, ru.json, en.json
    ├── services/               # Business logic
    │   ├── user_service.py
    │   ├── company_service.py
    │   ├── supplier_service.py
    │   ├── catalog_service.py
    │   ├── purchase_request_service.py
    │   ├── request_service.py
    │   ├── rfq_service.py
    │   ├── quotation_service.py
    │   ├── award_service.py
    │   ├── goods_receipt_service.py
    │   ├── pricing_service.py
    │   ├── invite_service.py
    │   ├── notification_service.py
    │   └── audit_service.py
    └── scheduler.py            # Background jobs (RFQ deadlines)
```

---

## 🚀 Getting Started

### Prerequisites

- **Python** 3.11 or higher
- **PostgreSQL** 15 or higher
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))

### 1. Clone the repository

```bash
git clone https://github.com/ghassonshadi5-tech/procurement-bot.git
cd procurement-bot
```

### 2. Create a virtual environment

```bash
python -m venv .venv

# Linux/macOS
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env` and fill in your real values:

```env
BOT_TOKEN=your_real_bot_token
BOT_USERNAME=your_bot_username
SUPER_ADMIN_TELEGRAM_ID=your_telegram_id
DATABASE_URL=postgresql://user:strong_password@localhost:5432/procurement_bot
```

> 🔒 Never commit your real `.env` file. It is already ignored by `.gitignore`.

### 5. Create the database

```bash
# Using psql
createdb procurement_bot
```

### 6. Run migrations

```bash
alembic upgrade head
```

### 7. Start the bot

```bash
python main.py
```

If everything is configured correctly, you'll see:

```text
✅ Bot is ready and polling for updates.
```

---

## 🔐 Security Notes

- ✅ All secrets are loaded from `.env` — never hardcoded.
- ✅ `.env` is excluded via `.gitignore`.
- ✅ `settings.py` enforces fail-fast validation at startup.
- ✅ `DEBUG=True` is rejected when `APP_ENV=production`.
- ✅ All sensitive operations are validated server-side (not just in UI).
- ✅ RLS policies enforce tenant isolation at the database level.

---

## 🌍 Localization

The bot supports **Arabic (default)**, **Russian**, and **English** via JSON-based translation files in `app/locales/*.json`.

> ℹ️ **Current status:** Translation coverage is **partial** — core UI screens (welcome, dashboard, language selection, roles) are fully translated. Additional screens will be migrated progressively.

To add a new language:

1. Copy `app/locales/en.json` to `app/locales/<lang>.json`.
2. Translate all values.
3. Add the language code to `SUPPORTED_LANGUAGES` in `.env`.
---

## 🗺️ Roadmap

- [x] Multi-tenant core with RLS
- [x] Purchase request workflow
- [x] Role-based access control
- [x] Multi-language support (ar / ru / en)
- [x] Background scheduler (RFQ deadlines)
- [x] Supplier catalog & pricing engine
- [x] Quotation submission & award decision recording
- [ ] RFQ offer comparison & scoring
- [ ] PDF / Excel report exports
- [ ] Comprehensive test suite
- [ ] Docker deployment

---

## 🤝 Contributing

This project is currently maintained as a solo effort. Contributions, issues, and suggestions are welcome — feel free to open an issue or submit a pull request.

---

## 📄 License

This project is licensed under the MIT License — see the LICENSE file for details.

---

## 📬 Contact

**Ghasson Shadi**

- GitHub: [@ghassonshadi5-tech](https://github.com/ghassonshadi5-tech)
- Email: ghashadi092@gmail.com

<p align="center"><sub>Built with ❤️ using Aiogram 3.x, SQLAlchemy, and PostgreSQL</sub></p>