#!/usr/bin/env python
"""Quick setup script - creates database, superuser, and sample data."""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'tracker.settings')

# Setup Django
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
django.setup()

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
print("\n[2/3] Creating admin user...")
if not User.objects.filter(username='admin').exists():
    user = User.objects.create_superuser(
        username='admin',
        email='admin@livetrack.com',
        password='admin123',
        first_name='Admin',
        last_name='Agent',
    )
    from tracker.chat.models import AgentProfile
    AgentProfile.objects.get_or_create(user=user)
    print("  Created: username='admin', password='admin123'")
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
print(f"\n  Login: admin / admin123")
print("=" * 50)
