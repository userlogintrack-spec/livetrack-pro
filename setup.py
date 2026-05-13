#!/usr/bin/env python
"""Quick setup script - creates database, superuser, and sample data."""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tracker.settings')

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

import secrets
import string

from django.core.management import call_command
from django.contrib.auth.models import User

print("=" * 50)
print("  LiveTrack Pro - Setup")
print("=" * 50)

# Run migrations
print("\n[1/3] Running migrations...")
call_command('migrate', verbosity=0)
print("  Done!")

# Create superuser
# Password precedence: ADMIN_PASSWORD env var > random generated.
# Never hard-code: a checked-in default leaks if setup.py runs in CI/shared envs.
print("\n[2/3] Creating admin user...")
if not User.objects.filter(username='admin').exists():
    admin_password = os.getenv('ADMIN_PASSWORD', '').strip()
    generated = False
    if not admin_password:
        alphabet = string.ascii_letters + string.digits
        admin_password = ''.join(secrets.choice(alphabet) for _ in range(20))
        generated = True
    user = User.objects.create_superuser(
        username='admin',
        email='admin@livetrack.com',
        password=admin_password,
        first_name='Admin',
        last_name='Agent',
    )
    from tracker.chat.models import AgentProfile
    AgentProfile.objects.get_or_create(user=user)
    if generated:
        print(f"  Created: username='admin', password='{admin_password}'")
        print("  >>> SAVE THIS PASSWORD NOW — it will not be shown again. <<<")
    else:
        print("  Created: username='admin' (password from ADMIN_PASSWORD env var)")
else:
    print("  Admin user already exists.")

# Create website settings
print("\n[3/3] Creating default settings...")
from tracker.core.models import WebsiteSettings
if not WebsiteSettings.objects.exists():
    WebsiteSettings.objects.create(
        site_name='LiveTrack Pro',
        welcome_message='Hi! How can we help you today?',
        chat_widget_color='#6366f1',
    )
    print("  Default settings created.")
else:
    print("  Settings already exist.")

print("\n" + "=" * 50)
print("  Setup Complete!")
print("=" * 50)
print(f"\n  Run the server:")
print(f"    python manage.py runserver")
print(f"\n  Then open:")
print(f"    Landing Page:  http://127.0.0.1:8000/")
print(f"    Agent Login:   http://127.0.0.1:8000/accounts/login/")
print(f"    Dashboard:     http://127.0.0.1:8000/dashboard/")
print(f"    Admin Panel:   http://127.0.0.1:8000/admin/")
print(f"\n  Login: admin / <password shown above>")
print("=" * 50)
