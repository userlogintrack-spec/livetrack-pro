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
    path('visitors/bulk/', views.visitors_bulk_action, name='visitors_bulk_action'),
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
    # Website Management
    path('settings/websites/', views.website_manage_view, name='website_manage'),
    path('settings/websites/<int:website_id>/delete/', views.website_delete, name='website_delete'),
    path('set-website/', views.set_active_website, name='set_website'),
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
    path('api/live-visitors/', views.live_visitors_api, name='live_visitors_api'),

    # Feature 7: Departments
    path('departments/', views.departments_view, name='departments'),

    # Feature 8: SLA Management
    path('sla/', views.sla_policies_view, name='sla_policies'),

    # Feature 9: Surveys / NPS
    path('surveys/', views.surveys_view, name='surveys'),
    path('surveys/<int:survey_id>/', views.survey_detail_view, name='survey_detail'),

    # Feature 1: AI Auto-Reply Bot
    path('ai-bot/', views.ai_bot_config_view, name='ai_bot_config'),

    # Feature 2: Chatbot Flow Builder
    path('chatbot-flows/', views.chatbot_flows_view, name='chatbot_flows'),
    path('chatbot-flows/<int:flow_id>/editor/', views.chatbot_flow_editor, name='chatbot_flow_editor'),

    # Feature 3: Knowledge Base (management)
    path('knowledge-base/', views.kb_manage_view, name='kb_manage'),

    # Feature 4: WhatsApp Integration
    path('whatsapp/', views.whatsapp_config_view, name='whatsapp_config'),

    # Feature 5: Visitor Segmentation
    path('segments/', views.visitor_segments_view, name='visitor_segments'),

    # Google Analytics Features
    path('advanced-analytics/', views.advanced_analytics_view, name='advanced_analytics'),
    path('goals/', views.goals_view, name='goals'),
    path('scheduled-reports/', views.scheduled_reports_view, name='scheduled_reports'),

    # Tour Guide
    path('tour/', views.tour_guide_view, name='tour_guide'),

    # Billing & Subscription
    path('billing/', views.billing_view, name='billing'),
    path('billing/checkout/', views.create_checkout_session, name='checkout'),
    path('billing/success/', views.billing_success, name='billing_success'),
    path('billing/cancel/', views.cancel_subscription, name='cancel_subscription'),
    path('billing/validate-coupon/', views.validate_coupon, name='validate_coupon'),
    path('billing/coupons/', views.manage_coupons_view, name='manage_coupons'),

    # Super Admin
    path('super-admin/', views.super_admin_view, name='super_admin'),

    # Microsoft Clarity Features
    path('heatmaps/', views.heatmaps_view, name='heatmaps'),
    path('recordings/', views.session_recordings_view, name='session_recordings'),
    path('recordings/<str:session_id>/', views.session_replay_view, name='session_replay'),
    path('js-errors/', views.js_errors_view, name='js_errors'),
    path('frustration/', views.frustration_dashboard_view, name='frustration_dashboard'),

    # Multi-Website Features
    path('settings/websites/<int:website_id>/verify/', views.website_verify_script, name='website_verify'),
    path('settings/websites/<int:website_id>/dashboard/', views.website_dashboard, name='website_dashboard'),
    path('settings/websites/<int:website_id>/approve/', views.website_approve, name='website_approve'),
    path('settings/websites/<int:website_id>/notifications/', views.website_notifications, name='website_notifications'),
    path('websites/compare/', views.website_compare, name='website_compare'),
    path('websites/activity-feed/', views.website_activity_feed, name='website_activity_feed'),
    path('websites/groups/', views.website_groups, name='website_groups'),
    path('websites/cross-domain/', views.cross_domain_visitors, name='cross_domain_visitors'),
    path('websites/badge/', views.visitor_badge, name='visitor_badge'),
]
