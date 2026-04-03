from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_home, name='home'),
    # Chats - search/transcript BEFORE <str:room_id> to avoid conflict
    path('chats/', views.chat_list, name='chat_list'),
    path('chats/search/', views.chat_search_view, name='chat_search'),
    path('chats/<str:room_id>/', views.chat_room_view, name='chat_room'),
    path('chats/<str:room_id>/close/', views.close_chat, name='close_chat'),
    path('chats/<str:room_id>/transfer/', views.transfer_chat, name='transfer_chat'),
    path('chats/<str:room_id>/notes/', views.internal_notes, name='internal_notes'),
    path('chats/<str:room_id>/tags/', views.update_chat_tags, name='update_tags'),
    path('chats/<str:room_id>/priority/', views.update_chat_priority, name='update_priority'),
    path('chats/<str:room_id>/transcript/', views.chat_transcript, name='chat_transcript'),
    path('chats/<str:room_id>/takeover/', views.chat_takeover, name='chat_takeover'),
    # Visitors
    path('visitors/', views.visitor_list, name='visitor_list'),
    path('visitors/banned/', views.ban_list_view, name='ban_list'),
    path('visitors/<int:visitor_id>/', views.visitor_detail, name='visitor_detail'),
    path('visitors/<int:visitor_id>/note/', views.add_visitor_note, name='add_note'),
    path('visitors/<int:visitor_id>/ban/', views.ban_visitor, name='ban_visitor'),
    # Exports
    path('export/visitors/', views.export_visitors_csv, name='export_visitors'),
    path('export/chats/', views.export_chats_csv, name='export_chats'),
    # Offline Messages
    path('offline-messages/', views.offline_messages_view, name='offline_messages'),
    path('offline-messages/<int:msg_id>/read/', views.mark_offline_read, name='mark_offline_read'),
    # Agent Stats
    path('agent-stats/', views.agent_stats, name='agent_stats'),
    # Canned Responses
    path('canned-responses/', views.canned_responses_view, name='canned_responses'),
    # Settings
    path('settings/website/', views.website_settings_view, name='website_settings'),
    path('settings/agents/', views.add_agent_view, name='add_agent'),
    path('settings/agents/<int:agent_id>/remove/', views.remove_agent, name='remove_agent'),
    path('settings/agents/<int:agent_id>/toggle/', views.toggle_agent_availability, name='toggle_agent'),
    path('settings/webhooks/', views.webhook_list, name='webhooks'),
    path('settings/webhooks/<int:webhook_id>/delete/', views.webhook_delete, name='webhook_delete'),
    path('settings/webhooks/<int:webhook_id>/toggle/', views.webhook_toggle, name='webhook_toggle'),
    path('settings/labels/', views.chat_labels_view, name='chat_labels'),
    path('settings/labels/<int:label_id>/delete/', views.delete_label, name='delete_label'),
    # Activity Log & Analytics
    path('activity-log/', views.activity_log_view, name='activity_log'),
    path('analytics/', views.analytics_view, name='analytics'),
    path('notifications/', views.notification_center_view, name='notification_center'),
    # Onboarding & Profile
    path('onboarding/', views.onboarding_view, name='onboarding'),
    path('profile/', views.profile_view, name='profile'),
    # Misc
    path('chats/<str:room_id>/email-transcript/', views.email_transcript, name='email_transcript'),
    path('chats/<str:room_id>/export-html/', views.export_chat_html, name='export_chat_html'),
    # Chat actions
    path('chats/<str:room_id>/snooze/', views.chat_snooze, name='chat_snooze'),
    path('chats/<str:room_id>/bookmark/', views.chat_bookmark, name='chat_bookmark'),
    path('chats/bulk-action/', views.chat_bulk_action, name='chat_bulk_action'),
    # Saved replies
    path('saved-replies/', views.saved_replies_view, name='saved_replies'),
    path('saved-replies/<int:reply_id>/delete/', views.delete_saved_reply, name='delete_saved_reply'),
    # API
    path('api/stats/', views.api_stats, name='api_stats'),
]
