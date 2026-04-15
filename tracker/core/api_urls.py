from django.urls import path
from . import views

urlpatterns = [
    path('widget/script.js', views.widget_script, name='widget_script'),
    path('widget/embed/', views.widget_embed_page, name='widget_embed'),
    path('widget/init/', views.widget_init, name='widget_init'),
    path('widget/track/', views.widget_track_pageview, name='widget_track'),
    path('widget/start-chat/', views.widget_start_chat, name='widget_start_chat'),
    path('chat/upload/<str:room_id>/', views.chat_file_upload, name='chat_file_upload'),
    path('chat/transcript/<str:room_id>/', views.widget_chat_transcript, name='widget_chat_transcript'),
    path('chat/rate/<str:room_id>/', views.chat_rate, name='chat_rate'),
    path('chat/offline-message/', views.submit_offline_message, name='offline_message'),
    path('widget/cursor/', views.widget_cursor_track, name='widget_cursor_track'),
    path('cursor/<str:session_key>/', views.cursor_fetch, name='cursor_fetch'),
]
