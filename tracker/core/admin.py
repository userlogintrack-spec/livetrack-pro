from django.contrib import admin
from .models import WebsiteSettings

@admin.register(WebsiteSettings)
class WebsiteSettingsAdmin(admin.ModelAdmin):
    list_display = ['site_name', 'chat_widget_color', 'auto_reply_enabled']
