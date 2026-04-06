import re
from django.conf import settings
from django.utils import timezone
from .models import Visitor, PageView


def parse_user_agent(ua_string):
    """Parse user agent string to extract browser, OS, and device type."""
    browser = 'Unknown'
    os_name = 'Unknown'
    device_type = 'desktop'

    # Browser detection
    browser_patterns = [
        (r'Edg[e/](\S+)', 'Edge'),
        (r'OPR/(\S+)', 'Opera'),
        (r'Chrome/(\S+)', 'Chrome'),
        (r'Firefox/(\S+)', 'Firefox'),
        (r'Safari/(\S+)', 'Safari'),
        (r'MSIE (\S+)', 'IE'),
        (r'Trident/.*rv:(\S+)', 'IE'),
    ]
    for pattern, name in browser_patterns:
        match = re.search(pattern, ua_string)
        if match:
            browser = name
            break

    # OS detection
    os_patterns = [
        (r'Windows NT 10', 'Windows 10/11'),
        (r'Windows NT 6\.3', 'Windows 8.1'),
        (r'Windows NT 6\.1', 'Windows 7'),
        (r'Mac OS X', 'macOS'),
        (r'Android (\S+)', 'Android'),
        (r'iPhone|iPad', 'iOS'),
        (r'Linux', 'Linux'),
        (r'Ubuntu', 'Ubuntu'),
    ]
    for pattern, name in os_patterns:
        if re.search(pattern, ua_string):
            os_name = name
            break

    # Device type detection
    if re.search(r'Mobile|Android.*Mobile|iPhone', ua_string):
        device_type = 'mobile'
    elif re.search(r'iPad|Android(?!.*Mobile)|Tablet', ua_string):
        device_type = 'tablet'

    return browser, os_name, device_type


def get_referrer_source(referrer):
    """Categorize the referrer source."""
    if not referrer:
        return 'Direct'
    referrer_lower = referrer.lower()
    sources = {
        'Google': ['google.com', 'google.co'],
        'Bing': ['bing.com'],
        'Yahoo': ['yahoo.com'],
        'Facebook': ['facebook.com', 'fb.com'],
        'Twitter': ['twitter.com', 't.co', 'x.com'],
        'LinkedIn': ['linkedin.com'],
        'Instagram': ['instagram.com'],
        'YouTube': ['youtube.com'],
        'Reddit': ['reddit.com'],
        'GitHub': ['github.com'],
    }
    for source, domains in sources.items():
        if any(d in referrer_lower for d in domains):
            return source
    return 'Other'


def get_client_ip(request):
    """Get the real client IP address."""
    trust_proxy = getattr(settings, 'TRUST_X_FORWARDED_FOR', False)
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if trust_proxy and x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def get_geo_from_ip(ip_address):
    """Get country/city from IP using free ip-api.com (non-blocking, fail-safe)."""
    import threading

    def _fetch(ip, result):
        try:
            import urllib.request
            import json
            if ip in ('127.0.0.1', '0.0.0.0', '::1', 'localhost'):
                return
            resp = urllib.request.urlopen(f'http://ip-api.com/json/{ip}?fields=country,city', timeout=2)
            data = json.loads(resp.read())
            result['country'] = data.get('country', '')
            result['city'] = data.get('city', '')
        except Exception:
            pass

    result = {}
    t = threading.Thread(target=_fetch, args=(ip_address, result))
    t.start()
    t.join(timeout=3)
    return result.get('country', ''), result.get('city', '')


class VisitorTrackingMiddleware:
    SKIP_PATHS = ('/static/', '/media/', '/admin/', '/favicon.ico', '/ws/', '/api/', '/accounts/')
    _default_org = None  # Class-level cache

    def __init__(self, get_response):
        self.get_response = get_response

    @classmethod
    def _get_default_org(cls):
        if cls._default_org is None:
            from tracker.core.models import Organization
            cls._default_org = Organization.objects.first()
        return cls._default_org

    def __call__(self, request):
        path = request.path

        # Fast skip - tuple check is faster than list
        if path.startswith(self.SKIP_PATHS):
            return self.get_response(request)

        # Skip dashboard for agents
        if request.user.is_authenticated and path.startswith('/dashboard'):
            return self.get_response(request)

        # Ensure session
        if not request.session.session_key:
            request.session.create()

        session_key = request.session.session_key
        ip_address = get_client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')
        referrer = request.META.get('HTTP_REFERER', '')

        browser, os_name, device_type = parse_user_agent(ua)
        default_org = self._get_default_org()

        # Extract UTM parameters
        utm_source = request.GET.get('utm_source', '')[:200]
        utm_medium = request.GET.get('utm_medium', '')[:200]
        utm_campaign = request.GET.get('utm_campaign', '')[:200]
        utm_term = request.GET.get('utm_term', '')[:200]
        utm_content = request.GET.get('utm_content', '')[:200]

        # Language detection from Accept-Language header
        accept_lang = request.META.get('HTTP_ACCEPT_LANGUAGE', '')
        language = accept_lang.split(',')[0].split(';')[0].strip()[:20] if accept_lang else ''

        full_url = request.build_absolute_uri()

        # Single query: get or create visitor
        visitor_defaults = {
            'ip_address': ip_address,
            'user_agent': ua,
            'browser': browser,
            'os': os_name,
            'device_type': device_type,
            'referrer': referrer[:500] if referrer else '',
            'referrer_source': get_referrer_source(referrer),
            'is_online': True,
            'language': language,
            'landing_page': full_url[:500],
        }
        # Only set UTM if present (don't overwrite with empty)
        if utm_source:
            visitor_defaults['utm_source'] = utm_source
        if utm_medium:
            visitor_defaults['utm_medium'] = utm_medium
        if utm_campaign:
            visitor_defaults['utm_campaign'] = utm_campaign
        if utm_term:
            visitor_defaults['utm_term'] = utm_term
        if utm_content:
            visitor_defaults['utm_content'] = utm_content

        visitor, created = Visitor.objects.get_or_create(
            session_key=session_key,
            organization=default_org,
            defaults=visitor_defaults,
        )

        # Geo-location only for new non-local visitors
        if created and ip_address not in ('127.0.0.1', '0.0.0.0', '::1'):
            try:
                country, city = get_geo_from_ip(ip_address)
                if country or city:
                    Visitor.objects.filter(pk=visitor.pk).update(country=country, city=city)
            except Exception:
                pass

        if not created:
            now = timezone.now()
            # Calculate session duration
            session_secs = int((now - visitor.first_visit).total_seconds()) if visitor.first_visit else 0
            page_count = visitor.total_visits + 1

            update_fields = {
                'last_seen': now,
                'total_visits': page_count,
                'is_online': True,
                'score': min(100, page_count * 5),
                'exit_page': full_url[:500],
                'session_duration': session_secs,
                'pages_per_session': page_count,
                'is_bounced': page_count <= 1,
            }
            # Update UTM only if new params present (don't clear existing)
            if utm_source:
                update_fields['utm_source'] = utm_source
            if utm_medium:
                update_fields['utm_medium'] = utm_medium
            if utm_campaign:
                update_fields['utm_campaign'] = utm_campaign
            if language and not visitor.language:
                update_fields['language'] = language

            Visitor.objects.filter(pk=visitor.pk).update(**update_fields)
            visitor.total_visits = page_count
            visitor.score = min(100, page_count * 5)

        # Mark previous pageview as exit=False, this as potential exit
        # (last page is always exit until next page)
        PageView.objects.filter(visitor=visitor, is_exit=True).update(is_exit=False)

        # Record page view
        is_entry = created  # First page of session
        pv = PageView.objects.create(
            visitor=visitor, url=full_url, page_title=path,
            is_entry=is_entry, is_exit=True,
            utm_source=utm_source, utm_medium=utm_medium, utm_campaign=utm_campaign,
        )

        # Check goal completions
        self._check_goals(visitor, full_url, default_org)

        # WebSocket broadcast (throttled - skip if same page within 2 sec)
        cache_key = f'ws_broadcast_{visitor.id}'
        from django.core.cache import cache
        if visitor.organization_id and not cache.get(cache_key):
            cache.set(cache_key, True, 2)  # Throttle: 1 broadcast per 2 seconds per visitor
            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer
                channel_layer = get_channel_layer()
                async_to_sync(channel_layer.group_send)(
                    f'dashboard_updates_{visitor.organization_id}',
                    {
                        'type': 'visitor_activity',
                        'visitor_id': visitor.id,
                        'ip': visitor.ip_address,
                        'browser': visitor.browser,
                        'os': visitor.os,
                        'device': visitor.device_type,
                        'country': visitor.country or '',
                        'score': visitor.score,
                        'score_label': visitor.score_label,
                        'current_page': full_url,
                        'page_title': path,
                        'total_pages': visitor.total_visits,
                        'is_chatting': False,  # Avoid extra query
                    }
                )
            except Exception:
                pass

        request.visitor = visitor

        response = self.get_response(request)
        return response

    def _check_goals(self, visitor, page_url, org):
        """Check if any goals are completed by this pageview."""
        if not org:
            return
        try:
            from .models import Goal, GoalCompletion
            goals = Goal.objects.filter(organization=org, is_active=True, goal_type='pageview')
            for goal in goals:
                if goal.target_url and goal.target_url in page_url:
                    # Don't double-count: check if already completed in last 30 min
                    from datetime import timedelta
                    recent = GoalCompletion.objects.filter(
                        goal=goal, visitor=visitor,
                        completed_at__gte=timezone.now() - timedelta(minutes=30)
                    ).exists()
                    if not recent:
                        GoalCompletion.objects.create(
                            goal=goal, visitor=visitor, page_url=page_url
                        )
        except Exception:
            pass
