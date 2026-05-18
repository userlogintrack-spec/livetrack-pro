from django.contrib import admin
from django.db import connection
from django.http import JsonResponse, HttpResponse
from django.urls import path, include, reverse
from django.conf import settings
from django.conf.urls.static import static
from django.views.decorators.cache import never_cache, cache_page
from django.utils import timezone
from tracker.dashboard import views as dashboard_views
from tracker.core.views import chat_reopen_consume


@never_cache
def healthz(request):
    """Cheap liveness probe — DB only, no Redis. Render polls this every
    ~30s; we used to also poke the cache here which burned ~80k Redis
    commands/month for zero benefit (a Redis outage shows up immediately
    on real requests). Use /healthz/full/ for deep dependency checks.

    Returns 200 if Postgres is reachable, 503 otherwise.
    """
    try:
        with connection.cursor() as c:
            c.execute('SELECT 1')
            c.fetchone()
        return JsonResponse({'status': 'ok'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)[:200]}, status=503)


@never_cache
def healthz_full(request):
    """Deep probe — DB + cache + channel layer. Intentionally not the
    target of Render's auto-probe because each call costs a couple of
    Redis ops. Hit this manually when debugging a fresh deploy.
    """
    checks = {'db': 'unknown', 'cache': 'unknown', 'channel_layer': 'unknown'}
    errors = {}

    try:
        with connection.cursor() as c:
            c.execute('SELECT 1')
            c.fetchone()
        checks['db'] = 'ok'
    except Exception as e:
        checks['db'] = 'error'
        errors['db'] = str(e)[:200]

    try:
        from django.core.cache import cache
        token = f'healthz:{timezone.now().timestamp()}'
        cache.set('healthz:probe', token, 5)
        if cache.get('healthz:probe') == token:
            checks['cache'] = 'ok'
        else:
            checks['cache'] = 'error'
            errors['cache'] = 'set/get round-trip mismatch (cache not persisting)'
    except Exception as e:
        checks['cache'] = 'error'
        errors['cache'] = str(e)[:200]

    try:
        from channels.layers import get_channel_layer
        layer = get_channel_layer()
        if layer is None:
            checks['channel_layer'] = 'missing'
        else:
            checks['channel_layer'] = type(layer).__name__
    except Exception as e:
        checks['channel_layer'] = 'error'
        errors['channel_layer'] = str(e)[:200]

    overall_ok = checks['db'] == 'ok' and checks['cache'] == 'ok'
    status_code = 200 if overall_ok else 503
    payload = {'status': 'ok' if overall_ok else 'error', 'checks': checks}
    if errors:
        payload['errors'] = errors
    return JsonResponse(payload, status=status_code)


def robots_txt(request):
    """Production-friendly robots.txt — block dashboard/admin, allow marketing pages."""
    host = request.get_host()
    scheme = 'https' if request.is_secure() else 'http'
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /dashboard/\n"
        "Disallow: /admin/\n"
        "Disallow: /accounts/\n"
        "Disallow: /api/\n"
        "Disallow: /healthz/\n"
        "Disallow: /healthz/full/\n"
        f"\nSitemap: {scheme}://{host}/sitemap.xml\n"
    )
    return HttpResponse(body, content_type='text/plain')


def manifest_json(request):
    """PWA manifest so the dashboard can be installed as an app."""
    return JsonResponse({
        'name': 'LiveTrack Pro',
        'short_name': 'LiveTrack',
        'description': 'Real-time visitor tracking, live chat & AI customer support.',
        'start_url': '/dashboard/',
        'display': 'standalone',
        'background_color': '#fafbff',
        'theme_color': '#7c3aed',
        'orientation': 'portrait-primary',
        'icons': [
            {'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
            {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png'},
        ],
    })


@cache_page(60 * 60)
def sitemap_xml(request):
    """Hand-written sitemap of public marketing routes — small, cacheable."""
    scheme = 'https' if request.is_secure() else 'http'
    host = request.get_host()
    today = timezone.now().date().isoformat()
    routes = [
        ('/', '1.0', 'weekly'),
        ('/features/', '0.9', 'monthly'),
        ('/compare/', '0.8', 'monthly'),
        ('/alternatives/', '0.8', 'monthly'),
        ('/alternatives/intercom/', '0.8', 'monthly'),
        ('/alternatives/tawk/', '0.8', 'monthly'),
        ('/alternatives/crisp/', '0.8', 'monthly'),
        ('/alternatives/drift/', '0.8', 'monthly'),
        ('/changelog/', '0.7', 'weekly'),
        ('/about/', '0.6', 'monthly'),
        ('/contact/', '0.6', 'monthly'),
        ('/privacy/', '0.3', 'yearly'),
        ('/terms/', '0.3', 'yearly'),
        ('/refund/', '0.3', 'yearly'),
    ]
    items = ''.join(
        f'<url><loc>{scheme}://{host}{path}</loc><lastmod>{today}</lastmod>'
        f'<changefreq>{freq}</changefreq><priority>{prio}</priority></url>'
        for path, prio, freq in routes
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f'{items}</urlset>'
    )
    return HttpResponse(body, content_type='application/xml')


urlpatterns = [
    path('healthz/', healthz, name='healthz'),
    path('healthz/full/', healthz_full, name='healthz_full'),
    path('chat/reopen/<str:token>/', chat_reopen_consume, name='chat_reopen'),
    path('robots.txt', robots_txt, name='robots_txt'),
    path('sitemap.xml', sitemap_xml, name='sitemap_xml'),
    path('manifest.json', manifest_json, name='manifest_json'),
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
