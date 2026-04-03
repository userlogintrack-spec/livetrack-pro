from django.contrib import admin
from .models import Visitor, PageView


@admin.register(Visitor)
class VisitorAdmin(admin.ModelAdmin):
    list_display = ['ip_address', 'browser', 'os', 'device_type', 'referrer_source', 'is_online', 'total_visits', 'first_visit', 'last_seen']
    list_filter = ['browser', 'os', 'device_type', 'referrer_source', 'is_online']
    search_fields = ['ip_address', 'session_key']
    readonly_fields = ['session_key', 'user_agent']


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ['visitor', 'url', 'timestamp']
    list_filter = ['timestamp']
    search_fields = ['url']
