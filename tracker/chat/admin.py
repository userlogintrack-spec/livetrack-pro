from django.contrib import admin
from .models import ChatRoom, Message, AgentProfile, OfflineMessage, CannedResponse, VisitorNote


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
    list_display = ['user', 'is_available', 'max_chats', 'total_chats_handled']
    list_filter = ['is_available']


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
