from django.contrib import admin
from .models import (
    ChatRoom, Message, AgentProfile, OfflineMessage, CannedResponse, VisitorNote,
    ChangelogEntry, WebhookDelivery, MagicLinkToken,
)


@admin.register(ChangelogEntry)
class ChangelogEntryAdmin(admin.ModelAdmin):
    list_display = ['published_at', 'category', 'version', 'title', 'is_published']
    list_filter = ['category', 'is_published', 'published_at']
    search_fields = ['title', 'body', 'version']
    fields = ['title', 'category', 'version', 'body', 'is_published', 'published_at']


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'event', 'webhook', 'status', 'response_status', 'attempt_count']
    list_filter = ['status', 'event', 'created_at']
    readonly_fields = ['webhook', 'event', 'payload', 'response_status', 'response_body',
                       'attempt_count', 'next_retry_at', 'status', 'last_error',
                       'created_at', 'completed_at']
    search_fields = ['event', 'webhook__url']


@admin.register(MagicLinkToken)
class MagicLinkTokenAdmin(admin.ModelAdmin):
    list_display = ['user', 'created_at', 'expires_at', 'consumed_at', 'requested_ip']
    readonly_fields = ['user', 'token', 'expires_at', 'consumed_at', 'requested_ip', 'created_at']


@admin.register(ChatRoom)
class ChatRoomAdmin(admin.ModelAdmin):
    list_display = ['room_id', 'visitor_name', 'visitor_email', 'agent', 'status', 'created_at', 'message_count']
    list_filter = ['status', 'created_at']
    search_fields = ['room_id', 'visitor_name', 'visitor_email']


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ['room', 'sender_type', 'sender_name', 'content', 'timestamp', 'is_read']
    list_filter = ['sender_type', 'is_read', 'timestamp']


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'is_available', 'max_chats', 'total_chats_handled', 'totp_enabled']
    list_filter = ['is_available', 'totp_enabled']
    # Never expose the encrypted TOTP secret or hashed backup codes through
    # the admin — even readonly, surfacing them invites accidental copy-paste
    # leaks. `totp_enabled` is the only 2FA-state knob admins need.
    exclude = ['totp_secret', 'backup_codes']


@admin.register(OfflineMessage)
class OfflineMessageAdmin(admin.ModelAdmin):
    list_display = ['name', 'email', 'is_read', 'created_at']
    list_filter = ['is_read', 'created_at']


@admin.register(CannedResponse)
class CannedResponseAdmin(admin.ModelAdmin):
    list_display = ['title', 'shortcut', 'created_by', 'is_global']


@admin.register(VisitorNote)
class VisitorNoteAdmin(admin.ModelAdmin):
    list_display = ['visitor', 'agent', 'created_at']
