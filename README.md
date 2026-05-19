# LiveTrack Pro - Real-Time Visitor Tracker + Live Chat

LiveTrack Pro is a Django-based app for tracking visitors in real time and handling live chat from a single agent dashboard.

## Features

### Visitor Tracking
- Real-time active visitor list (30-minute activity window)
- IP, browser, OS, and device detection
- Referrer/source detection (direct, search, social, referral)
- Per-visitor page view timeline
- Visitor detail page with chat history

### Live Chat
- WebSocket-based chat (Django Channels)
- Widget init + pre-chat flow
- Waiting -> Active -> Closed chat lifecycle
- Typing indicators and system messages
- File/image upload support in chat
- Chat rating + feedback after chat

### Agent Productivity
- Chat tags and priority (low/medium/high)
- Pinned chats
- Visitor notes for internal context
- Canned responses
- Offline message inbox
- Agent performance stats dashboard
- CSV export for visitors and chats

## Tech Stack
- Python 3.10+
- Django 4.2+
- Channels 4+
- Daphne (ASGI)
- SQLite (default)
- HTML/CSS/Vanilla JS

## Project Structure

```text
tracker/
|- manage.py
|- setup.py
|- requirements.txt
|- db.sqlite3
|- tracker/
|  |- settings.py
|  |- urls.py
|  |- asgi.py
|  |- core/
|  |- visitors/
|  |- chat/
|  |- dashboard/
|  |- pages/
|  |- templates/
```

## Quick Start

### 1) Install dependencies
```bash
pip install -r requirements.txt
```

### 2) Run setup
```bash
python setup.py
```
This runs migrations, creates default website settings, and creates:
- Username: `admin`
- Password: `admin123`

### 3) Start server
```bash
python manage.py runserver 8000
```

### 4) Open app
- Landing: `http://127.0.0.1:8000/`
- Login: `http://127.0.0.1:8000/accounts/login/`
- Dashboard: `http://127.0.0.1:8000/dashboard/`
- Admin: `http://127.0.0.1:8000/admin/`

## API Endpoints

### Public Widget APIs
- `POST /api/widget/init/`
- `POST /api/widget/start-chat/`
- `POST /api/chat/upload/<room_id>/`
- `POST /api/chat/rate/<room_id>/`
- `POST /api/chat/offline-message/`

### Dashboard APIs (Auth Required)
- `GET /dashboard/api/stats/`
- `POST /dashboard/chats/<room_id>/close/`
- `POST /dashboard/chats/<room_id>/tags/`
- `POST /dashboard/chats/<room_id>/priority/`
- `POST /dashboard/visitors/<visitor_id>/note/`

## WebSocket Endpoints
- Chat stream: `ws://<host>/ws/chat/<room_id>/`
- Dashboard stream: `ws://<host>/ws/dashboard/`

## Important Settings (Current Defaults)
- `DEBUG = True`
- `ALLOWED_HOSTS = ['*']`
- Channel layer: `InMemoryChannelLayer`
- Timezone: `Asia/Kolkata`

## Production Deployment

### 1. Required environment variables

Copy `.env.example` and fill these (see the file for full list):

| Variable | Notes |
|---|---|
| `SECRET_KEY` | `python -c "import secrets; print(secrets.token_urlsafe(50))"` |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | Your domain + optional `.subdomain.com` for wildcards |
| `DATABASE_URL` | PostgreSQL connection string (Neon / Render / Supabase) |
| `REDIS_URL` | Redis instance. Free option: [Upstash](https://upstash.com) — use `rediss://` (TLS) |
| `CSRF_TRUSTED_ORIGINS` | `https://yourdomain.com,https://*.yourdomain.com` |
| `FIELD_ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `EMAIL_HOST_USER` + `EMAIL_HOST_PASSWORD` | For password reset, magic-link emails. Gmail: use an App Password |
| `SENTRY_DSN` *(optional)* | Error monitoring |

### 2. Build / release steps

```bash
pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate
```

Render's "Build Command" should chain these together. The first deploy will apply ~20 migrations.

### 3. Schedule cron jobs

These management commands need to run periodically:

| Command | Frequency | Purpose |
|---|---|---|
| `python manage.py poll_email_inboxes` | every 2–5 min | Pulls customer emails into the dashboard as chats. Required only if Email-to-Chat is enabled. |
| `python manage.py process_webhook_retries` | every 1–5 min | Retries failed webhook deliveries with exponential backoff |
| `python manage.py send_auto_replies` | every 5–15 min | Sends auto-responder messages on long-waiting chats |
| `python manage.py purge_old_data` | daily | Deletes chats / recordings / events past the org's retention window |

**Render Cron Jobs** (recommended):
- Dashboard → "+ New" → "Cron Job"
- Schedule (cron string), e.g. `*/2 * * * *` for every 2 minutes
- Build command: same as your web service
- Command: `python manage.py <command_name>`

**System cron** (self-hosted):
```cron
*/2 * * * * cd /path/to/app && python manage.py poll_email_inboxes
*/5 * * * * cd /path/to/app && python manage.py process_webhook_retries
*/15 * * * * cd /path/to/app && python manage.py send_auto_replies
0 3 * * *   cd /path/to/app && python manage.py purge_old_data
```

### 4. First-time setup after first deploy

1. Visit `https://yourdomain.com/admin/` and log in with the bootstrap admin (created by `setup.py`).
2. Change the admin password immediately.
3. Visit `/dashboard/settings/website/` to:
   - Set widget title, color, welcome message
   - Configure business hours
   - Set allowed-domains (anti-abuse) if your widget should only run on specific sites
4. (Optional) Visit `/dashboard/ai-bot/` to plug in a Gemini or Claude API key for AI features.
5. (Optional) Visit `/dashboard/email-mailboxes/` to connect a Gmail/Outlook/custom mailbox for Email-to-Chat.

### 5. Health checks

| Endpoint | Use |
|---|---|
| `GET /healthz/` | Liveness probe (DB only — cheap, safe to poll every 30s) |
| `GET /healthz/full/` | Deep probe — DB + cache + channel layer. Use manually for debugging |

Render's "Health Check Path" should be set to `/healthz/`.

### 6. Security posture

The app ships with:
- Field-level encryption (Fernet) for 2FA secrets, webhook keys, AI API keys
- TOTP 2FA + 8 hashed backup codes per agent
- Magic-link passwordless login
- Throttled login, magic-link, password reset, AI endpoints
- `session.cycle_key()` rotation at every auth boundary
- HSTS, secure cookies, CSP-friendly widget script
- Per-org isolation enforced at every query

Run `python manage.py check --deploy` before launch.

## Troubleshooting

### Migrations/setup issues
```bash
python manage.py makemigrations
python manage.py migrate
python setup.py
```

### Reset DB (Windows)
```powershell
Remove-Item .\db.sqlite3 -Force
python manage.py migrate
python setup.py
```

### Reset DB (Linux/macOS)
```bash
rm db.sqlite3
python manage.py migrate
python setup.py
```

### Port already in use
```bash
python manage.py runserver 8080
```

## What Else You Can Add

- JWT/API token auth for widget endpoints
- Multi-tenant support (multiple websites per account)
- Auto-assignment rules for chats
- Redis + Celery for async jobs/notifications
- Slack/WhatsApp/email integrations
- Saved filters + advanced analytics charts
- Automated tests (unit + integration + WebSocket)
- Docker + docker-compose setup
- CI pipeline (lint, tests, build)

## License
MIT
