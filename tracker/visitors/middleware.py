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
    # Skip tracking for these paths
    SKIP_PATHS = ['/static/', '/media/', '/admin/', '/favicon.ico', '/ws/', '/api/', '/accounts/']

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip tracking for static files and admin
        path = request.path
        if any(path.startswith(skip) for skip in self.SKIP_PATHS):
            return self.get_response(request)

        # Skip authenticated agent users viewing dashboard
        if request.user.is_authenticated and path.startswith('/dashboard'):
            return self.get_response(request)

        # Ensure session exists
        if not request.session.session_key:
            request.session.create()

        session_key = request.session.session_key
        ip_address = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        referrer = request.META.get('HTTP_REFERER', '')

        browser, os_name, device_type = parse_user_agent(user_agent)
        referrer_source = get_referrer_source(referrer)

        # Get default org for middleware-tracked visitors
        from tracker.core.models import Organization
        default_org = Organization.objects.first()

        # Get or create visitor
        visitor, created = Visitor.objects.get_or_create(
            session_key=session_key,
            organization=default_org,
            defaults={
                'ip_address': ip_address,
                'user_agent': user_agent,
                'browser': browser,
                'os': os_name,
                'device_type': device_type,
                'referrer': referrer[:500] if referrer else '',
                'referrer_source': referrer_source,
                'is_online': True,
            }
        )

        # Fetch geo-location for new visitors (async, non-blocking)
        if created and ip_address not in ('127.0.0.1', '0.0.0.0', '::1'):
            try:
                country, city = get_geo_from_ip(ip_address)
                if country or city:
                    Visitor.objects.filter(pk=visitor.pk).update(country=country, city=city)
            except Exception:
                pass

        if not created:
            visitor.last_seen = timezone.now()
            visitor.total_visits += 1
            visitor.is_online = True
            # Update engagement score
            score = min(100, visitor.total_visits * 5 + visitor.page_views.count() * 2)
            visitor.score = score
            visitor.save(update_fields=['last_seen', 'total_visits', 'is_online', 'score'])

        # Record page view
        full_url = request.build_absolute_uri()
        PageView.objects.create(
            visitor=visitor,
            url=full_url,
            page_title=path,
        )

        # Store visitor in request for use in views
        request.visitor = visitor

        response = self.get_response(request)
        return response
