from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from tracker.dashboard import views as dashboard_views

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('tracker.core.urls')),
    path('dashboard/', include('tracker.dashboard.urls')),
    path('api/', include('tracker.core.api_urls')),
    # Public Knowledge Base
    path('kb/<slug:org_slug>/', dashboard_views.kb_public_view, name='kb_public'),
    path('kb/<slug:org_slug>/<slug:article_slug>/', dashboard_views.kb_article_view, name='kb_article'),
    # Public API endpoints
    path('api/kb/feedback/<int:article_id>/', dashboard_views.kb_article_feedback, name='kb_feedback'),
    path('api/survey/<int:survey_id>/submit/', dashboard_views.submit_survey_response, name='survey_submit'),
    path('api/whatsapp/webhook/', dashboard_views.whatsapp_webhook, name='whatsapp_webhook'),
    # Stripe webhook removed - using built-in card checkout
    path('api/track/event/', dashboard_views.track_event_api, name='track_event'),
    path('api/track/performance/', dashboard_views.track_performance_api, name='track_performance'),
    path('api/track/clicks/', dashboard_views.track_clicks_api, name='track_clicks'),
    path('api/track/scroll/', dashboard_views.track_scroll_api, name='track_scroll'),
    path('api/track/js-error/', dashboard_views.track_js_error_api, name='track_js_error'),
    path('api/track/session/', dashboard_views.track_session_api, name='track_session'),
    path('api/track/frustration/', dashboard_views.track_frustration_api, name='track_frustration'),
    path('', include('tracker.pages.urls')),
    path('', include('tracker.core.landing_urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
