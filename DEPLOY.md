# LiveTrack Pro - Deployment Guide

## Why Not SQLite on Render?

Render uses **ephemeral filesystem** - every deploy/restart wipes all files including `db.sqlite3`. Your data (users, chats, visitors) will be **lost every time**. That's why we use **PostgreSQL** which Render provides free.

---

## Deploy on Render.com (Free)

### What You Get Free:
- Web Service (750 hrs/month = 24/7 for 1 service)
- PostgreSQL Database (1GB free)
- Redis (25MB free - for WebSocket/real-time)
- Auto HTTPS/SSL
- Custom domain support
- Auto-deploy from GitHub

---

### Step 1: Push Code to GitHub

Open terminal in project folder (`D:\Claude Tool\tracker`):

```bash
# Initialize git (skip if already done)
git init

# Add all files
git add .

# Commit
git commit -m "LiveTrack Pro - ready for deployment"

# Create repo on GitHub first (github.com/new), then:
git remote add origin https://github.com/YOUR_USERNAME/livetrack-pro.git
git branch -M main
git push -u origin main
```

---

### Step 2: Create Render Account

1. Go to **https://render.com**
2. Click **"Get Started for Free"**
3. Sign up with **GitHub** (easiest)

---

### Step 3: Create PostgreSQL Database

1. Dashboard > **"New"** > **"PostgreSQL"**
2. Settings:
   - **Name**: `livetrack-db`
   - **Region**: Singapore (closest to India) or Oregon
   - **Plan**: **Free**
3. Click **"Create Database"**
4. Wait 1-2 minutes for it to be ready
5. Copy the **Internal Database URL** (starts with `postgres://...`)
   - You'll need this in Step 5

---

### Step 4: Create Redis Instance

1. Dashboard > **"New"** > **"Redis"**
2. Settings:
   - **Name**: `livetrack-redis`
   - **Region**: Same as database
   - **Plan**: **Free**
   - **Max Memory Policy**: `allkeys-lru`
3. Click **"Create Redis"**
4. Copy the **Internal Redis URL** (starts with `redis://...`)
   - You'll need this in Step 5

---

### Step 5: Create Web Service

1. Dashboard > **"New"** > **"Web Service"**
2. Connect your **GitHub repo** (livetrack-pro)
3. Settings:
   - **Name**: `livetrack` (your app URL will be `livetrack-xxxx.onrender.com`)
   - **Region**: Same as database
   - **Runtime**: **Python**
   - **Build Command**:
     ```
     pip install -r requirements.txt && python manage.py collectstatic --noinput && python manage.py migrate
     ```
   - **Start Command**:
     ```
     daphne -b 0.0.0.0 -p $PORT tracker.asgi:application
     ```
   - **Plan**: **Free**

4. Click **"Advanced"** and add these **Environment Variables**:

   | Key | Value |
   |-----|-------|
   | `DEBUG` | `False` |
   | `SECRET_KEY` | Click "Generate" (random 50-char string) |
   | `ALLOWED_HOSTS` | `.onrender.com` |
   | `CSRF_TRUSTED_ORIGINS` | `https://livetrack-xxxx.onrender.com` (your actual URL) |
   | `DATABASE_URL` | Paste the PostgreSQL Internal URL from Step 3 |
   | `REDIS_URL` | Paste the Redis Internal URL from Step 4 |
   | `TRUST_X_FORWARDED_FOR` | `True` |
   | `PYTHON_VERSION` | `3.10.7` |

5. Click **"Create Web Service"**

---

### Step 6: Wait for Build (3-5 minutes)

Render will:
1. Clone your repo
2. Install Python packages
3. Collect static files
4. Run database migrations (creates all tables in PostgreSQL)
5. Start Daphne server

Watch the **Logs** tab - when you see `Listening on TCP address 0.0.0.0:XXXX`, it's ready!

---

### Step 7: Create Admin User

1. Go to your Web Service on Render
2. Click **"Shell"** tab (top right)
3. Run:
   ```bash
   python manage.py createsuperuser
   ```
4. Enter username, email, password

OR just go to `https://your-app.onrender.com/accounts/register/` and sign up normally.

---

### Step 8: Access Your App!

- **Landing Page**: `https://your-app.onrender.com/`
- **Sign Up**: `https://your-app.onrender.com/accounts/register/`
- **Sign In**: `https://your-app.onrender.com/accounts/login/`
- **Dashboard**: `https://your-app.onrender.com/dashboard/`
- **Admin**: `https://your-app.onrender.com/admin/`

---

## Optional: Custom Domain

1. Buy a domain (Namecheap, GoDaddy, Cloudflare)
2. In Render Web Service > **Settings** > **Custom Domains**
3. Add your domain (e.g., `app.livetrack.in`)
4. Add the CNAME record in your domain DNS:
   ```
   Type: CNAME
   Name: app
   Value: your-app.onrender.com
   ```
5. Update env var:
   ```
   ALLOWED_HOSTS = .onrender.com,.livetrack.in
   CSRF_TRUSTED_ORIGINS = https://app.livetrack.in,https://your-app.onrender.com
   ```
6. Render auto-provisions SSL certificate

---

## Optional: Email Setup (Gmail SMTP)

To send password reset emails and chat notifications:

1. Create a Gmail App Password:
   - Go to https://myaccount.google.com/apppasswords
   - Generate a new app password for "Mail"

2. Add env vars on Render:

   | Key | Value |
   |-----|-------|
   | `EMAIL_BACKEND` | `django.core.mail.backends.smtp.EmailBackend` |
   | `EMAIL_HOST` | `smtp.gmail.com` |
   | `EMAIL_PORT` | `587` |
   | `EMAIL_HOST_USER` | `your-email@gmail.com` |
   | `EMAIL_HOST_PASSWORD` | `your-app-password` |
   | `DEFAULT_FROM_EMAIL` | `LiveTrack <your-email@gmail.com>` |

---

## Troubleshooting

### App shows "Service Unavailable"
- Check Logs tab for errors
- Make sure DATABASE_URL and REDIS_URL are set correctly

### Static files not loading (no CSS)
- Make sure build command includes `collectstatic --noinput`
- WhiteNoise is in MIDDLEWARE (already configured)

### WebSocket not connecting
- Make sure REDIS_URL is set
- Daphne (not gunicorn) must be the start command
- Check browser console for WebSocket errors

### Database errors after model changes
- Push new code to GitHub
- Render auto-deploys and runs migrations

### Slow cold start (free plan)
- Free plan sleeps after 15 min of inactivity
- First request after sleep takes 30-60 seconds
- Upgrade to paid ($7/mo) for always-on

---

## Architecture on Render

```
[Browser] --> [Render Web Service (Daphne)]
                    |
                    |--> [PostgreSQL (Free)] - Users, Chats, Visitors
                    |
                    |--> [Redis (Free)] - WebSocket channels, real-time
                    |
                    |--> [WhiteNoise] - Static files (CSS, JS)
```

---

## Cost Summary

| Component | Plan | Cost |
|-----------|------|------|
| Web Service | Free | $0 |
| PostgreSQL | Free | $0 |
| Redis | Free | $0 |
| SSL/HTTPS | Auto | $0 |
| Custom Domain | Optional | $10/year |
| **Total** | | **$0/month** |

---

## Alternative Free Platforms

If Render doesn't work for you:

| Platform | Command | Notes |
|----------|---------|-------|
| **Railway.app** | Same setup, $5 free credit/month | Slightly faster |
| **Fly.io** | `flyctl launch` | 3 free VMs |
| **Koyeb.com** | GitHub connect | 1 free app |

All support WebSocket + PostgreSQL + Redis.
