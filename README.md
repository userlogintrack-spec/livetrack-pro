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

## Production Checklist
- Change `SECRET_KEY`
- Set `DEBUG = False`
- Restrict `ALLOWED_HOSTS`
- Replace in-memory channel layer with Redis
- Use HTTPS + secure cookie settings
- Change default admin password immediately

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
