import json
import logging
import hashlib
import re
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import JsonResponse, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from tracker.chat.models import AgentProfile
from tracker.chat.security import create_ws_token
from tracker.chat.utils import close_stale_chats, auto_assign_agent


logger = logging.getLogger(__name__)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ALLOWED_FILE_EXTENSIONS = {
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'txt', 'csv', 'zip', 'rar', '7z'
}
ALLOWED_IMAGE_EXTENSIONS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'}
ALLOWED_MIME_TYPES = {
    'application/pdf',
    'application/msword',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.ms-excel',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'text/plain',
    'text/csv',
    'application/zip',
    'application/x-zip-compressed',
    'application/x-rar-compressed',
    'application/octet-stream',
    'image/jpeg',
    'image/png',
    'image/gif',
    'image/webp',
    'image/bmp',
}


def _client_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '0.0.0.0')


def _parse_json_body(request):
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except json.JSONDecodeError:
        return None


def _rate_limit(request, scope, limit, window_seconds):
    ip = _client_ip(request)
    key = f"rl:{scope}:{ip}"
    current = cache.get(key, 0)
    if current >= limit:
        return True
    if current == 0:
        cache.set(key, 1, timeout=window_seconds)
    else:
        cache.incr(key)
    return False


def _adaptive_rate_limit(request, scope, org=None, base_limit=30, window_seconds=60, session_key=''):
    """Rate limiter with org-aware attack mode and IP burst protection."""
    ip = _client_ip(request)
    org_id = getattr(org, 'id', 0) or 0
    sk = (session_key or request.session.session_key or '')[:24]
    strict = bool(getattr(org, 'attack_mode_enabled', False))
    limit = max(3, int(base_limit * (0.35 if strict else 1.0)))
    global_limit = max(8, int(limit * (2 if strict else 4)))

    key = f"arl:{scope}:{org_id}:{ip}:{sk}"
    gkey = f"arlg:{scope}:{org_id}:{ip}"
    current = cache.get(key, 0)
    gcurrent = cache.get(gkey, 0)
    if current >= limit or gcurrent >= global_limit:
        return True

    if current == 0:
        cache.set(key, 1, timeout=window_seconds)
    else:
        cache.incr(key)
    if gcurrent == 0:
        cache.set(gkey, 1, timeout=window_seconds)
    else:
        cache.incr(gkey)
    return False


def _is_honeypot_triggered(data):
    return bool(
        (data.get('website') or '').strip()
        or (data.get('company') or '').strip()
        or (data.get('fax') or '').strip()
    )


def _spam_score(text):
    text = (text or '').strip()
    if not text:
        return 0
    score = 0
    if len(text) > 1000:
        score += 2
    url_hits = len(re.findall(r'(https?://|www\.)', text, flags=re.IGNORECASE))
    if url_hits >= 2:
        score += 3
    if re.search(r'(.)\1{7,}', text):
        score += 2
    words = re.findall(r'\w+', text.lower())
    if len(words) >= 12:
        uniq_ratio = len(set(words)) / max(1, len(words))
        if uniq_ratio < 0.35:
            score += 2
    uppercase_ratio = sum(1 for c in text if c.isupper()) / max(1, len(text))
    if len(text) > 20 and uppercase_ratio > 0.6:
        score += 1
    return score


def _resolve_or_create_visitor(org, ip, ua, session_key, defaults=None, visitor_fingerprint='', website=None):
    """Centralised dedup: returns the canonical Visitor row for this device+website.

    Match priority:
        1. existing row with session_key + org + website -> exact match
        2. fingerprint + org + website -> returning visitor on same domain
        3. brand new row

    Visitors are scoped per-website so the same user on two different domains
    gets two separate Visitor rows. This keeps analytics domain-isolated.
    """
    from tracker.visitors.models import Visitor
    if not session_key:
        session_key = uuid.uuid4().hex

    # Build website filter — match same website (or both NULL)
    ws_match = {'website': website} if website else {'website__isnull': True}

    # 1. Exact session + website match
    if session_key:
        v = Visitor.objects.filter(organization=org, session_key=session_key, **ws_match).first()
        if v:
            return v, False

    # 2. Fingerprint fallback (per-website)
    if visitor_fingerprint:
        cutoff = timezone.now() - timedelta(days=30)
        v = (Visitor.objects.filter(
            organization=org, visitor_fingerprint=visitor_fingerprint, last_seen__gte=cutoff, **ws_match
        ).order_by('-last_seen').first())
        if v:
            if session_key and v.session_key != session_key:
                v.session_key = session_key
            if ip and not v.ip_address:
                v.ip_address = ip
            if ua and not v.user_agent:
                v.user_agent = ua
            updates = []
            if session_key and v.session_key == session_key:
                updates.append('session_key')
            if ip and v.ip_address == ip:
                updates.append('ip_address')
            if ua and v.user_agent == ua:
                updates.append('user_agent')
            if updates:
                v.save(update_fields=updates)
            return v, False

    # 3. Create new — one visitor per session per website
    create_kwargs = {
        'organization': org,
        'website': website,
        'session_key': session_key,
        'visitor_fingerprint': (visitor_fingerprint or '')[:100],
        'ip_address': ip,
        'user_agent': ua,
    }
    if defaults:
        create_kwargs.update(defaults)
    v = Visitor.objects.create(**create_kwargs)
    return v, True


def _split_multiline_csv(value):
    if not value:
        return []
    return [item.strip() for item in value.replace(',', '\n').splitlines() if item.strip()]


def _normalize_domain(host):
    host = (host or '').strip().lower()
    if not host:
        return ''
    if host.startswith('http://') or host.startswith('https://'):
        try:
            host = urlparse(host).hostname or host
        except Exception:
            pass
    if ':' in host:
        host = host.split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host


def _extract_parent_domain(request, body_data=None):
    body_data = body_data or {}
    candidates = [
        body_data.get('parent_domain', ''),
        request.GET.get('pd', ''),
    ]
    origin = request.META.get('HTTP_ORIGIN', '')
    if origin:
        candidates.append(origin)
    referer = request.META.get('HTTP_REFERER', '')
    if referer:
        candidates.append(referer)
    for raw in candidates:
        domain = _normalize_domain(raw)
        if domain:
            return domain
    return ''


def _extract_fingerprint(body_data=None):
    body_data = body_data or {}
    fp = (body_data.get('fingerprint') or body_data.get('visitor_fingerprint') or '').strip()
    if not fp:
        return ''
    # Keep only safe compact token chars.
    fp = re.sub(r'[^a-zA-Z0-9:_-]', '', fp)
    return fp[:100]


def _monthly_visitor_limit_state(org, session_key='', visitor_fingerprint=''):
    """Return whether org can create a *new* visitor this month.

    Rules:
    - Free plan: max 100 visitors/month
    - Paid plans: unlimited visitors
    - Existing visitors (same session_key/fingerprint) always allowed
    """
    from tracker.core.models import Subscription
    from tracker.visitors.models import Visitor

    if not org:
        return {'allowed': True, 'is_new': False, 'limit': None, 'count': 0, 'plan': 'free'}

    sub = Subscription.objects.filter(organization=org).first()
    plan = (sub.plan if sub else 'free').lower()
    if plan != 'free':
        return {'allowed': True, 'is_new': False, 'limit': None, 'count': 0, 'plan': plan}

    existing = None
    if session_key:
        existing = Visitor.objects.filter(organization=org, session_key=session_key).first()
    if not existing and visitor_fingerprint:
        cutoff = timezone.now() - timedelta(days=30)
        existing = Visitor.objects.filter(
            organization=org,
            visitor_fingerprint=visitor_fingerprint,
            last_seen__gte=cutoff
        ).order_by('-last_seen').first()
    if existing:
        return {'allowed': True, 'is_new': False, 'limit': 100, 'count': 0, 'plan': plan}

    month_start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_count = Visitor.objects.filter(organization=org, first_visit__gte=month_start).count()
    if monthly_count >= 100:
        return {'allowed': False, 'is_new': True, 'limit': 100, 'count': monthly_count, 'plan': plan}
    return {'allowed': True, 'is_new': True, 'limit': 100, 'count': monthly_count, 'plan': plan}


def _domain_allowed(org, domain):
    if not org or not org.allowed_domains_enabled:
        return True
    allowed = [_normalize_domain(x) for x in _split_multiline_csv(org.allowed_domains)]
    allowed = [d for d in allowed if d]
    if not allowed:
        return True
    if not domain:
        return False
    return any(domain == rule or domain.endswith('.' + rule) for rule in allowed)


def _resolve_country_for_request(request, ip_address):
    country_name = ''
    country_code = (request.META.get('HTTP_CF_IPCOUNTRY') or '').strip().upper()
    city_name = ''
    cache_key = f'geoip:{ip_address}'
    cached = cache.get(cache_key)
    if cached:
        country_name = cached.get('country', '')
        city_name = cached.get('city', '')
    else:
        try:
            from tracker.visitors.middleware import get_geo_from_ip
            country_name, city_name = get_geo_from_ip(ip_address)
            cache.set(cache_key, {'country': country_name, 'city': city_name}, 60 * 60 * 24)
        except Exception:
            country_name, city_name = '', ''
    return (country_name or '').strip(), country_code, (city_name or '').strip()


def _country_blocked(org, country_name, country_code):
    if not org or not org.blocked_countries_enabled:
        return False
    blocked = [x.strip().lower() for x in _split_multiline_csv(org.blocked_countries)]
    if not blocked:
        return False
    name_token = (country_name or '').strip().lower()
    code_token = (country_code or '').strip().lower()
    return (name_token and name_token in blocked) or (code_token and code_token in blocked)


def _resolve_room_actor(request, room):
    """Determine who is acting on a chat room (agent / collaborator / visitor).

    For agents: primary agent OR any ChatParticipant on the room can act (collaboration).
    For visitors: session_key must match room.visitor accepts session_key from
    cookie, POST body, form data, or query string (for cross-origin iframes where
    third-party cookies are blocked).
    """
    if request.user.is_authenticated:
        # Primary agent or superuser ? always allowed
        if not room.agent_id or room.agent_id == request.user.id or request.user.is_superuser:
            sender_name = request.user.get_full_name() or request.user.username
            return {'sender_type': 'agent', 'sender_name': sender_name}
        # Collaborator (any joined participant) ? allowed
        from tracker.chat.models import ChatParticipant
        if ChatParticipant.objects.filter(room=room, user=request.user).exists():
            sender_name = request.user.get_full_name() or request.user.username
            return {'sender_type': 'agent', 'sender_name': sender_name}
        return None

    # Visitor: try multiple sources for session_key (cookie may be blocked cross-origin)
    session_key = request.session.session_key or ''
    if not session_key:
        # Body JSON
        try:
            body_data = json.loads(request.body) if request.body else {}
            session_key = (body_data.get('session_key') or '').strip()
        except (ValueError, AttributeError):
            session_key = ''
    if not session_key:
        # POST form / multipart upload
        session_key = (request.POST.get('session_key') or '').strip()
    if not session_key:
        # Query string fallback
        session_key = (request.GET.get('sk') or '').strip()

    if not session_key or session_key != room.visitor.session_key:
        return None
    return {'sender_type': 'visitor', 'sender_name': room.visitor_name or 'Visitor'}


def get_user_org(user):
    """Return the Organization for an authenticated agent user."""
    profile = getattr(user, 'agent_profile', None)
    if profile:
        return profile.organization
    return None


def get_plan_limits(org):
    """Return plan limits for an organization."""
    if not org:
        return {'max_visitors_per_month': 100, 'max_agents': 1, 'advanced_analytics': False, 'ai_bot': False}
    from tracker.core.models import Subscription
    sub = Subscription.objects.filter(organization=org).first()
    if not sub:
        sub = Subscription.objects.create(organization=org, plan='free', status='active')
    return sub.plan_limits


def check_plan_feature(org, feature):
    """Check if org's plan allows a specific feature. Returns True/False."""
    limits = get_plan_limits(org)
    return limits.get(feature, False)


def login_view(request):
    if request.user.is_authenticated:
        return redirect('dashboard:home')
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            # Ensure agent profile exists with org
            profile = AgentProfile.objects.filter(user=user).select_related('organization').first()
            if not profile:
                # Find org where user is owner, or first available org
                from tracker.core.models import Organization
                org = Organization.objects.filter(owner=user).first() or Organization.objects.first()
                AgentProfile.objects.create(user=user, organization=org, role='agent')
            return redirect('dashboard:home')
        messages.error(request, 'Invalid username or password.')
    else:
        form = AuthenticationForm()
    return render(request, 'core/login.html', {'form': form})


def register_view(request):
    """Sign up: creates user + organization + agent profile (owner role)."""
    if request.user.is_authenticated:
        return redirect('dashboard:home')
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        org_name = request.POST.get('org_name', '').strip() or f"{username}'s Organization"

        # Keep form data on error
        form_data = {'username': username, 'email': email, 'first_name': first_name, 'last_name': last_name, 'org_name': org_name}

        if not username or not password:
            messages.error(request, 'Username and password are required.')
            return render(request, 'core/register.html', form_data)

        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
            return render(request, 'core/register.html', form_data)

        if email and User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
            return render(request, 'core/register.html', form_data)

        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            first_name=first_name,
            last_name=last_name,
        )

        # Create organization for this user
        from tracker.core.models import Organization
        from django.utils.text import slugify
        import uuid as _uuid
        slug = slugify(org_name)[:90]
        # Ensure unique slug
        if Organization.objects.filter(slug=slug).exists():
            slug = f"{slug}-{_uuid.uuid4().hex[:6]}"
        org = Organization.objects.create(
            name=org_name,
            slug=slug,
            owner=user,
        )

        # Create agent profile with owner role
        AgentProfile.objects.create(user=user, organization=org, role='owner')
        # Create subscription based on selected plan
        from tracker.core.models import Subscription
        selected_plan = request.POST.get('plan', 'free') or request.GET.get('plan', 'free')
        if selected_plan not in ('free', 'pro', 'enterprise'):
            selected_plan = 'free'
        Subscription.objects.get_or_create(organization=org, defaults={'plan': selected_plan if selected_plan == 'free' else 'free', 'status': 'active'})

        # Best-effort welcome email. Do not block signup if SMTP is unavailable.
        if user.email:
            from tracker.core.email_utils import send_welcome_email
            send_welcome_email(
                user,
                login_url=request.build_absolute_uri('/accounts/login/'),
                dashboard_url=request.build_absolute_uri('/dashboard/'),
            )
        login(request, user)
        # If paid plan selected, redirect to billing to complete payment
        if selected_plan in ('pro', 'enterprise'):
            return redirect(f'/dashboard/billing/?upgrade={selected_plan}')
        return redirect('dashboard:onboarding')
    return render(request, 'core/register.html')


def logout_view(request):
    logout(request)
    return redirect('core:login')


def _get_org_from_request(request):
    """Get Organization from widget_key in request body or query params."""
    from tracker.core.models import Organization
    org, _ = _get_website_from_request(request)
    return org


def _get_website_from_request(request):
    """Get (Organization, Website) from tracking_key in request body or query params.

    Auto-detection: When a key matches an org-level widget_key and the requesting
    domain doesn't have a Website record yet, one is created automatically so the
    user never needs to manually add each domain.
    """
    from tracker.core.models import Organization, Website
    from tracker.chat.models import AgentProfile, AgentWebsiteAccess

    data = _parse_json_body(request) if request.body else {}
    key = (data or {}).get('key') or request.GET.get('key') or ''
    parent_domain = _extract_parent_domain(request, data or {})

    if key:
        # 1. Direct website match by tracking_key
        website = Website.objects.select_related('organization').filter(tracking_key=key).first()
        if website:
            return website.organization, website

        # 2. Org-level widget_key — auto-detect domain
        org = Organization.objects.filter(widget_key=key).first()
        if org:
            # Try to find existing website for this domain
            if parent_domain:
                normalized = _normalize_domain(parent_domain)
                if normalized:
                    existing_ws = Website.objects.filter(organization=org, domain=normalized).first()
                    if existing_ws:
                        return org, existing_ws
                    # Auto-create website for this domain
                    ws = _auto_register_website(org, normalized)
                    if ws:
                        return org, ws
            # Fallback to first website
            return org, org.websites.first()

    # Fallback
    org = Organization.objects.first()
    return org, org.websites.first() if org else None


def _auto_register_website(org, domain):
    """Auto-register a new website for the org when script detects a new domain.

    Returns the new Website or None if creation is skipped (rate-limited, blocked, etc).
    """
    from tracker.core.models import Website
    from tracker.chat.models import AgentProfile, AgentWebsiteAccess

    if not org or not domain:
        return None

    # Skip localhost/IP-like domains in production-like environments
    skip_domains = {'localhost', '127.0.0.1', '0.0.0.0', ''}
    if domain in skip_domains:
        return None

    # Rate-limit: max 20 auto-registered websites per org
    existing_count = Website.objects.filter(organization=org).count()
    if existing_count >= 20:
        return None

    # Don't auto-register if domain is explicitly blocked
    if org.allowed_domains_enabled and not _domain_allowed(org, domain):
        return None

    # Create website with pending approval
    name = domain.split('.')[0].capitalize() if '.' in domain else domain.capitalize()
    ws = Website.objects.create(
        organization=org,
        name=name,
        domain=domain,
        is_auto_detected=True,
        approval_status='pending',
    )

    # Grant all existing agents access
    for agent in AgentProfile.objects.filter(organization=org):
        AgentWebsiteAccess.objects.get_or_create(agent=agent, website=ws)

    return ws


@csrf_exempt
def widget_init(request):
    """Initialize chat widget - creates visitor session and returns config."""
    if request.method == 'POST':
        if not request.session.session_key:
            request.session.create()

        org, website = _get_website_from_request(request)

        from tracker.visitors.middleware import get_client_ip, parse_user_agent

        body_data = _parse_json_body(request) or {}
        body_session = (body_data.get('session_key') or '').strip()
        visitor_fingerprint = _extract_fingerprint(body_data)
        session_key = body_session or request.session.session_key
        limit_state = _monthly_visitor_limit_state(org, session_key=session_key, visitor_fingerprint=visitor_fingerprint)
        if not limit_state.get('allowed', True):
            return JsonResponse({
                'error': 'Free plan visitor limit reached (100/month). Upgrade for unlimited visitors.',
                'code': 'VISITOR_LIMIT_REACHED',
                'limit': limit_state.get('limit'),
                'count': limit_state.get('count'),
            }, status=402)
        if _adaptive_rate_limit(request, 'widget_init', org=org, base_limit=40, window_seconds=60, session_key=session_key):
            msg = org.attack_mode_message if getattr(org, 'attack_mode_enabled', False) else 'Too many requests. Please wait and retry.'
            return JsonResponse({'error': msg}, status=429)
        parent_domain = _extract_parent_domain(request, body_data)
        if not _domain_allowed(org, parent_domain):
            return JsonResponse({'error': 'Widget blocked on this domain.', 'blocked': True}, status=403)
        ip = get_client_ip(request)
        country_name, country_code, city_name = _resolve_country_for_request(request, ip)
        if _country_blocked(org, country_name, country_code):
            return JsonResponse({'error': 'Widget is blocked in your country.', 'blocked': True}, status=403)
        ua = request.META.get('HTTP_USER_AGENT', '')
        browser, os_name, device_type = parse_user_agent(ua)

        visitor, visitor_created = _resolve_or_create_visitor(
            org=org, ip=ip, ua=ua, session_key=session_key, visitor_fingerprint=visitor_fingerprint,
            website=website,
            defaults={
                'browser': browser, 'os': os_name, 'device_type': device_type,
                'country': country_name, 'city': city_name,
            },
        )
        # Notify dashboard agents about new visitor
        if visitor_created and org:
            from tracker.chat.notifications import send_dashboard_notification
            domain = website.domain if website else 'direct'
            location = country_name or 'Unknown'
            if city_name:
                location = f'{city_name}, {location}'
            send_dashboard_notification(
                org_id=org.id,
                category='new_visitor',
                title='New Visitor',
                body=f'{location} on {domain} ({browser}, {device_type})',
                severity='info',
                url=f'/dashboard/visitors/{visitor.id}/',
                sound=False,
                website=website,
            )

        if (country_name and not visitor.country) or (city_name and not visitor.city):
            visitor.country = visitor.country or country_name
            visitor.city = visitor.city or city_name
            visitor.save(update_fields=['country', 'city'])

        return JsonResponse({
            'session_key': visitor.session_key or session_key,
            'visitor_id': visitor.id,
            'welcome_message': (website.welcome_message if website and website.welcome_message else None) or (org.welcome_message if org else 'Hi! How can we help you?'),
            'widget_color': (website.widget_color if website and website.widget_color else None) or (org.widget_color if org else '#7c3aed'),
        })
    return JsonResponse({'error': 'POST required'}, status=405)


@csrf_exempt
def widget_track_pageview(request):
    """Record a page view from the embedded widget on a customer's website."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    data = _parse_json_body(request) or {}
    url = (data.get('url') or '')[:500]
    title = (data.get('title') or '')[:300]
    referrer = (data.get('referrer') or '')[:500]
    body_session = data.get('session_key', '')
    visitor_fingerprint = _extract_fingerprint(data)

    if not url:
        return JsonResponse({'error': 'url required'}, status=400)

    org, website = _get_website_from_request(request)
    if not org:
        return JsonResponse({'error': 'org not found'}, status=404)
    parent_domain = _extract_parent_domain(request, data)
    if not _domain_allowed(org, parent_domain):
        return JsonResponse({'error': 'Widget blocked on this domain.', 'blocked': True}, status=403)

    from tracker.visitors.middleware import (
        get_client_ip, parse_user_agent, get_referrer_source,
    )
    from tracker.visitors.models import Visitor, PageView

    if not request.session.session_key:
        request.session.create()
    session_key = (body_session or request.session.session_key or '').strip()
    limit_state = _monthly_visitor_limit_state(org, session_key=session_key, visitor_fingerprint=visitor_fingerprint)
    if not limit_state.get('allowed', True):
        return JsonResponse({
            'error': 'Free plan visitor limit reached (100/month). Upgrade for unlimited visitors.',
            'code': 'VISITOR_LIMIT_REACHED',
            'limit': limit_state.get('limit'),
            'count': limit_state.get('count'),
        }, status=402)
    if _adaptive_rate_limit(request, 'widget_track', org=org, base_limit=120, window_seconds=60, session_key=session_key):
        msg = org.attack_mode_message if getattr(org, 'attack_mode_enabled', False) else 'Too many requests.'
        return JsonResponse({'error': msg}, status=429)

    ip = get_client_ip(request)
    country_name, country_code, city_name = _resolve_country_for_request(request, ip)
    if _country_blocked(org, country_name, country_code):
        return JsonResponse({'error': 'Widget is blocked in your country.', 'blocked': True}, status=403)
    ua = request.META.get('HTTP_USER_AGENT', '')
    browser, os_name, device_type = parse_user_agent(ua)

    visitor, created = _resolve_or_create_visitor(
        org=org, ip=ip, ua=ua, session_key=session_key, visitor_fingerprint=visitor_fingerprint,
        website=website,
        defaults={
            'browser': browser, 'os': os_name, 'device_type': device_type,
            'referrer': referrer, 'referrer_source': get_referrer_source(referrer),
            'is_online': True, 'landing_page': url,
            'country': country_name, 'city': city_name,
        },
    )
    if (country_name and not visitor.country) or (city_name and not visitor.city):
        Visitor.objects.filter(pk=visitor.pk).update(
            country=visitor.country or country_name,
            city=visitor.city or city_name,
        )
        visitor.country = visitor.country or country_name
        visitor.city = visitor.city or city_name

    now = timezone.now()
    page_count = (visitor.total_visits or 0) + (1 if not created else 1)
    Visitor.objects.filter(pk=visitor.pk).update(
        last_seen=now,
        total_visits=page_count,
        is_online=True,
        score=min(100, page_count * 5),
        exit_page=url,
        pages_per_session=page_count,
        is_bounced=page_count <= 1,
    )

    # Mark previous pageview as not-exit
    PageView.objects.filter(visitor=visitor, is_exit=True).update(is_exit=False)

    PageView.objects.create(
        visitor=visitor,
        url=url,
        page_title=title or url,
        is_entry=created,
        is_exit=True,
    )

    # Real-time broadcast to dashboard (throttled)
    cache_key = f'ws_broadcast_{visitor.id}'
    if visitor.organization_id and not cache.get(cache_key):
        cache.set(cache_key, True, 2)
        try:
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
                    'score': min(100, page_count * 5),
                    'score_label': visitor.score_label,
                    'current_page': url,
                    'page_title': title or url,
                    'total_pages': page_count,
                    'is_chatting': False,
                }
            )
        except Exception:
            pass

    # Hot lead notification (score >= 70, once per visitor session)
    computed_score = min(100, page_count * 5)
    if computed_score >= 70 and visitor.organization_id:
        hot_key = f'hot_lead_notified:{visitor.id}'
        if not cache.get(hot_key):
            cache.set(hot_key, True, 3600)
            from tracker.chat.notifications import send_dashboard_notification
            location = visitor.country or 'Unknown'
            if visitor.city:
                location = f'{visitor.city}, {location}'
            send_dashboard_notification(
                org_id=visitor.organization_id,
                category='hot_lead',
                title='Hot Lead',
                body=f'Visitor from {location} scored {computed_score} ({page_count} pages viewed)',
                severity='warning',
                url=f'/dashboard/visitors/{visitor.id}/',
                sound=True,
            )

    return JsonResponse({
        'ok': True,
        'session_key': session_key,
        'visitor_id': visitor.id,
        'page_count': page_count,
    })


def widget_script(request):
    """
    Public embeddable widget script.
    Usage:
    <script src="https://your-domain/api/widget/script.js?key=YOUR_KEY"></script>
    """
    base_url = request.build_absolute_uri('/').rstrip('/')
    # Force HTTPS for any deployment whose host is NOT localhost prevents mixed-content
    # blocks when the customer's site is on HTTPS but our absolute URL ended up http://
    # (happens behind some proxies even with SECURE_PROXY_SSL_HEADER set).
    host = request.get_host().split(':')[0]
    if host not in ('localhost', '127.0.0.1', '0.0.0.0') and base_url.startswith('http://'):
        base_url = 'https://' + base_url[len('http://'):]
    widget_key = request.GET.get('key', '')

    # Load org customization via Website
    from tracker.core.models import Organization, Website
    website = Website.objects.select_related('organization').filter(tracking_key=widget_key).first() if widget_key else None
    org = website.organization if website else (Organization.objects.filter(widget_key=widget_key).first() if widget_key else None)
    script_domain = _extract_parent_domain(request, {})
    if org and not _domain_allowed(org, script_domain):
        blocked_js = (
            "(function(){console.warn('LiveVisitorHub widget blocked on this domain.');})();"
        )
        blocked_resp = HttpResponse(blocked_js, content_type='application/javascript; charset=utf-8')
        blocked_resp['Cache-Control'] = 'public, max-age=300'
        return blocked_resp
    # Website-level settings override org defaults
    widget_color = (website.widget_color if website and website.widget_color else None) or (org.widget_color if org else '#7c3aed')
    widget_title = (website.widget_title if website and website.widget_title else None) or (org.widget_title if org else 'LiveVisitorHub Support')
    widget_position = (website.widget_position if website and website.widget_position else None) or (org.widget_position if org else 'bottom-right')
    pos_css = 'left:24px' if widget_position == 'bottom-left' else 'right:24px'
    panel_pos_css = 'left:24px' if widget_position == 'bottom-left' else 'right:24px'

    js = r"""
(function() {
  if (window.LiveTrackWidgetLoaded) return;
  window.LiveTrackWidgetLoaded = true;
  // Swallow any unexpected widget error so the host page is never affected.
  function _safe(fn) { return function() { try { return fn.apply(this, arguments); } catch(e) {} }; }
  var _idle = window.requestIdleCallback || function(cb){ return setTimeout(function(){ cb({ didTimeout:false, timeRemaining:function(){return 50;} }); }, 1); };
  try {
  var BASE = "__BASE__";
  var WIDGET_KEY = "__WIDGET_KEY__";
  var WC = "__WIDGET_COLOR__";
  var isOpen = false;

  // ===== Visitor session persistence (cross-page) =====
  var SK_KEY = "ltw_session_key_" + WIDGET_KEY;
  function getSessionKey() {
    try { return localStorage.getItem(SK_KEY) || ""; } catch(e) { return ""; }
  }
	  function setSessionKey(k) {
	    try { localStorage.setItem(SK_KEY, k); } catch(e) {}
	  }
	  function getFingerprint() {
	    try {
	      var raw = [
	        navigator.userAgent || "",
	        navigator.language || "",
	        navigator.platform || "",
	        (screen.width || 0) + "x" + (screen.height || 0),
	        new Date().getTimezoneOffset()
	      ].join("|");
	      var h = 0;
	      for (var i = 0; i < raw.length; i++) { h = ((h << 5) - h) + raw.charCodeAt(i); h |= 0; }
	      return "fp_" + Math.abs(h);
	    } catch(e) { return ""; }
	  }

  // ===== Page view tracking =====
  var lastTrackedUrl = "";
  var recPages = 1;
  function trackPageView() {
    var url = location.href;
    if (url === lastTrackedUrl) return;
    if (lastTrackedUrl && url !== lastTrackedUrl) recPages += 1;
    lastTrackedUrl = url;
    var payload = {
      key: WIDGET_KEY,
	      session_key: getSessionKey(),
	      fingerprint: getFingerprint(),
	      parent_domain: location.hostname || "",
	      url: url,
      title: document.title || "",
      referrer: document.referrer || ""
    };
    try {
      fetch(BASE + "/api/widget/track/", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        credentials: "include",
        body: JSON.stringify(payload),
        keepalive: true
      }).then(function(r){ return r.json(); }).then(function(d){
        if (d && d.session_key) {
          setSessionKey(d.session_key);
          if (!recSessionId) { recCreateAttempts = 0; setTimeout(startSessionRecording, 200); }
        }
      }).catch(function(){});
    } catch(e) {}
  }

  // Initial pageview
  trackPageView();

  // SPA navigation hooks
  var _push = history.pushState;
  history.pushState = function() { _push.apply(this, arguments); setTimeout(trackPageView, 0); };
  var _replace = history.replaceState;
  history.replaceState = function() { _replace.apply(this, arguments); setTimeout(trackPageView, 0); };
  window.addEventListener("popstate", function(){ setTimeout(trackPageView, 0); });
  window.addEventListener("hashchange", function(){ setTimeout(trackPageView, 0); });

  // ===== Session Recording (lightweight) =====
  var recSessionId = "";
  var recStartedAt = Date.now();
  var recBuffer = [];
  var recHasRage = false;
  var recHasDead = false;
  var recHasErrors = false;
  var clickBurst = [];
  var recCreateAttempts = 0;
  var recCreateMaxAttempts = 6;

  function recPayload(extra) {
    var payload = {
      key: WIDGET_KEY,
      session_key: getSessionKey(),
      parent_domain: location.hostname || "",
      session_id: recSessionId
    };
    for (var k in extra) payload[k] = extra[k];
    return payload;
  }

  function recPost(path, body, keepalive) {
    try {
      return fetch(BASE + path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        credentials: "include",
        keepalive: !!keepalive,
        body: JSON.stringify(body)
      }).then(function(r) {
        if (!r.ok) {
          console.warn("[LTW] rec API " + r.status + " on " + path);
          return r.json().catch(function(){ return {error: r.status}; });
        }
        return r.json();
      });
    } catch (e) {
      console.warn("[LTW] rec fetch error:", e);
      return Promise.resolve(null);
    }
  }

  function trackRecEvent(type, data) {
    var evt = { t: Date.now(), type: type };
    for (var k in (data || {})) evt[k] = data[k];
    recBuffer.push(evt);
    if (recBuffer.length > 400) recBuffer = recBuffer.slice(-400);
  }

  function startSessionRecording() {
    var sk = getSessionKey();
    if (!sk) {
      if (recCreateAttempts < recCreateMaxAttempts) {
        recCreateAttempts += 1;
        setTimeout(startSessionRecording, 1500);
      } else {
        console.warn("[LTW] Session recording: no session key after retries");
      }
      return;
    }
    recPost("/api/track/session/", recPayload({
      action: "create",
      url: location.href,
      screen_w: screen.width || 0,
      screen_h: screen.height || 0
    }), false).then(function(d){
      if (d && d.session_id) {
        recSessionId = d.session_id;
        return;
      }
      if (d && d.error) console.warn("[LTW] Session create failed:", d.error);
      if (recCreateAttempts < recCreateMaxAttempts) {
        recCreateAttempts += 1;
        setTimeout(startSessionRecording, 1500);
      }
    }).catch(function(e){ console.warn("[LTW] Session create error:", e); });
  }

  function flushSessionRecording(force) {
    if (!recSessionId || recBuffer.length === 0) return;
    var batch = recBuffer.splice(0, 120);
    recPost("/api/track/session/", recPayload({
      action: "append",
      events: batch,
      duration: Math.max(1, Math.floor((Date.now() - recStartedAt) / 1000)),
      pages: recPages,
      has_rage: recHasRage,
      has_dead: recHasDead,
      has_errors: recHasErrors
    }), !!force).catch(function(){});
  }

  document.addEventListener("click", function(e) {
    var target = e.target || {};
    var now = Date.now();
    var x = e.clientX || 0, y = e.clientY || 0;
    trackRecEvent("click", {
      x: x,
      y: y,
      tag: (target.tagName || "").toLowerCase(),
      text: ((target.innerText || target.textContent || "").trim().slice(0, 80))
    });
    clickBurst.push({t: now, x: x, y: y});
    clickBurst = clickBurst.filter(function(c){ return now - c.t <= 1000; });
    if (clickBurst.length >= 4) recHasRage = true;
  }, { capture: true, passive: true });

  var _scrollTimer = null;
  window.addEventListener("scroll", function() {
    if (_scrollTimer) clearTimeout(_scrollTimer);
    _scrollTimer = setTimeout(function() {
      var doc = document.documentElement || document.body;
      var maxScroll = Math.max(doc.scrollHeight - window.innerHeight, 1);
      var scrollPct = Math.min(100, Math.round((window.scrollY / maxScroll) * 100));
      trackRecEvent("scroll", { y: window.scrollY || 0, pct: scrollPct });
    }, 150);
  }, {passive: true});

  // Report only widget-originated errors; never hijack host page errors.
  window.addEventListener("error", _safe(function(ev) {
    var src = (ev && ev.filename) || "";
    if (!src || src.indexOf(BASE) !== 0) return;
    recHasErrors = true;
    trackRecEvent("error", { msg: (ev.message || "").slice(0, 200) });
    recPost("/api/track/js-error/", recPayload({
      message: ev.message || "Script error",
      source: src,
      line: ev.lineno || 0,
      col: ev.colno || 0,
      stack: ev.error && ev.error.stack ? String(ev.error.stack).slice(0, 1800) : "",
      url: location.href
    }), false).catch(function(){});
  }));

  // pagehide is more reliable than beforeunload on mobile Safari.
  window.addEventListener("pagehide", _safe(function() { flushSessionRecording(true); }));
  document.addEventListener("visibilitychange", _safe(function() {
    if (document.visibilityState === "hidden") flushSessionRecording(true);
  }));
  setInterval(_safe(function(){ flushSessionRecording(false); }), 15000);
  // Defer recording start until browser is idle — zero impact on host LCP/TTI.
  _idle(function(){ try { startSessionRecording(); } catch(e) {} }, { timeout: 3000 });

  var style = document.createElement("style");
  style.textContent = ".ltw-btn{position:fixed;__POS_CSS__;bottom:24px;z-index:999999;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;color:#fff;font-size:22px;background:"+WC+";box-shadow:0 8px 24px rgba(0,0,0,.2);transition:all .3s;display:flex;align-items:center;justify-content:center;}.ltw-btn:hover{transform:scale(1.08);box-shadow:0 12px 32px rgba(0,0,0,.3)}.ltw-frame{position:fixed;__PANEL_POS_CSS__;bottom:94px;z-index:999999;width:min(400px,calc(100vw - 24px));height:min(600px,calc(100vh - 120px));border:none;border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,.15),0 0 0 1px rgba(0,0,0,.04);display:none;background:white;overflow:hidden;}@media(max-width:480px){.ltw-btn{width:48px;height:48px;font-size:18px;bottom:16px}.ltw-frame{bottom:72px;width:calc(100vw - 16px);height:calc(100vh - 88px);border-radius:16px}}";
  document.head.appendChild(style);

  var ICON_CHAT = '<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  var ICON_CLOSE = '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

  var btn = document.createElement("button");
  btn.className = "ltw-btn";
  btn.innerHTML = ICON_CHAT;

  var frame = document.createElement("iframe");
  frame.className = "ltw-frame";
  // Pass current session_key into iframe so chat & tracking share the same visitor
  var _sk = encodeURIComponent(getSessionKey() || "");
	  var _pd = encodeURIComponent(location.hostname || "");
	  frame.src = BASE + "/api/widget/embed/?key=" + WIDGET_KEY + (_sk ? "&sk=" + _sk : "") + (_pd ? "&pd=" + _pd : "");
  frame.allow = "microphone;camera;display-capture";

  document.body.appendChild(btn);
  document.body.appendChild(frame);

  function closePanel() {
    isOpen = false;
    frame.style.display = "none";
    btn.innerHTML = ICON_CHAT;
  }

  btn.onclick = function() {
    isOpen = !isOpen;
    frame.style.display = isOpen ? "block" : "none";
    btn.innerHTML = isOpen ? ICON_CLOSE : ICON_CHAT;
  };

  // Listen for messages from the iframe (close button, session sync)
  window.addEventListener("message", function(ev) {
    var d = ev.data;
    if (d === "ltw-close") { closePanel(); return; }
    if (d && typeof d === "object") {
      if (d.type === "ltw-close") { closePanel(); return; }
      if (d.type === "ltw-session" && d.sessionKey) { setSessionKey(d.sessionKey); return; }
      if (d.type === "ltw-open") {
        isOpen = true;
        frame.style.display = "block";
        btn.innerHTML = ICON_CLOSE;
        return;
      }
    }
  });

  // Proactive chat trigger
  if ("__PROACTIVE__" === "true") {
    setTimeout(function() {
      if (!isOpen) {
        btn.style.animation = "ltw-pulse 1.5s ease infinite";
        var notif = document.createElement("div");
        notif.style.cssText = "position:fixed;__PANEL_POS_CSS__;bottom:90px;z-index:999999;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:12px 16px;box-shadow:0 8px 24px rgba(0,0,0,0.12);font-family:Inter,Arial,sans-serif;max-width:260px;cursor:pointer;";
        notif.innerHTML = '<div style="font-size:13px;font-weight:600;color:#1f2937;">__PROACTIVE_MSG__</div><div style="font-size:11px;color:#9ca3af;margin-top:4px;">Click to chat with us</div>';
        notif.onclick = function() { notif.remove(); btn.click(); };
        document.body.appendChild(notif);
        setTimeout(function() { if (notif.parentNode) notif.remove(); }, 10000);
      }
    }, parseInt("__PROACTIVE_DELAY__") * 1000);
  }
  } catch(e) { /* widget top-level guard — never propagate to host page */ }
})();
"""
    # Proactive chat settings
    proactive_enabled = 'true' if (org and org.proactive_enabled) else 'false'
    proactive_msg = org.proactive_message if org else 'Need help? Chat with us!'
    proactive_delay = str(org.proactive_delay if org else 30)

    js = js.replace("__BASE__", base_url)
    js = js.replace("__WIDGET_KEY__", widget_key)
    js = js.replace("__WIDGET_COLOR__", widget_color)
    js = js.replace("__WIDGET_TITLE__", widget_title)
    js = js.replace("__POS_CSS__", pos_css)
    js = js.replace("__PANEL_POS_CSS__", panel_pos_css)
    js = js.replace("__PROACTIVE__", proactive_enabled)
    js = js.replace("__PROACTIVE_MSG__", proactive_msg.replace('"', '\\"'))
    js = js.replace("__PROACTIVE_DELAY__", proactive_delay)

    # ─── Browser/CDN caching so host sites don't re-download on every page view ───
    import hashlib
    etag = '"' + hashlib.md5(js.encode('utf-8')).hexdigest() + '"'
    if request.META.get('HTTP_IF_NONE_MATCH') == etag:
        resp = HttpResponse(status=304)
    else:
        resp = HttpResponse(js, content_type='application/javascript; charset=utf-8')
    resp['ETag'] = etag
    # 5 min fresh, serve stale for another hour while revalidating in background
    resp['Cache-Control'] = 'public, max-age=300, stale-while-revalidate=3600'
    resp['Vary'] = 'Accept-Encoding'
    return resp


def _get_time_greeting(name):
    """Generate time-based greeting."""
    hour = timezone.now().hour
    if hour < 12:
        greeting = 'Good morning'
    elif hour < 17:
        greeting = 'Good afternoon'
    else:
        greeting = 'Good evening'
    return f'{greeting}, {name}! An agent will be with you shortly.'


@csrf_exempt
def widget_start_chat(request):
    """Start a new chat from the widget."""
    if request.method == 'POST':
        data = _parse_json_body(request)
        if data is None:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)
        if _is_honeypot_triggered(data):
            return JsonResponse({'error': 'Unable to start chat right now.'}, status=400)
        visitor_fingerprint = _extract_fingerprint(data)

        # Get session  prefer cookie, fallback to session_key in body (cross-origin widget)
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key

        # Cross-origin fallback: widget passes session_key from init response
        body_session = data.get('session_key', '')
        if body_session:
            session_key = body_session

        # Resolve org + website from widget key
        org, website = _get_website_from_request(request)
        limit_state = _monthly_visitor_limit_state(org, session_key=session_key, visitor_fingerprint=visitor_fingerprint)
        if not limit_state.get('allowed', True):
            return JsonResponse({
                'error': 'Free plan visitor limit reached (100/month). Upgrade for unlimited visitors.',
                'code': 'VISITOR_LIMIT_REACHED',
                'limit': limit_state.get('limit'),
                'count': limit_state.get('count'),
            }, status=402)

        if _adaptive_rate_limit(request, 'widget_start_chat', org=org, base_limit=10, window_seconds=60, session_key=session_key):
            msg = org.attack_mode_message if getattr(org, 'attack_mode_enabled', False) else 'Too many requests. Please wait and try again.'
            return JsonResponse({'error': msg}, status=429)
        parent_domain = _extract_parent_domain(request, data)
        if not _domain_allowed(org, parent_domain):
            return JsonResponse({'error': 'Widget blocked on this domain.', 'blocked': True}, status=403)

        from tracker.visitors.models import Visitor
        from tracker.chat.models import ChatRoom
        from django.db.models import Max

        # Find or create visitor (with smart dedup by IP+UA so we don't create duplicates)
        from tracker.visitors.middleware import get_client_ip, parse_user_agent
        ip = get_client_ip(request)
        subject_text = (data.get('subject') or '').strip()
        spam_score = _spam_score(subject_text)
        if spam_score >= 4:
            return JsonResponse({'error': 'Message blocked for safety checks. Please rephrase and try again.'}, status=429)
        if subject_text:
            msg_hash = hashlib.sha1(subject_text.lower().encode('utf-8')).hexdigest()[:16]
            dup_key = f'spamdup:{getattr(org, "id", 0)}:{ip}:{msg_hash}'
            dup_count = cache.get(dup_key, 0)
            if dup_count >= 3:
                return JsonResponse({'error': 'Repeated messages detected. Please wait before retrying.'}, status=429)
            if dup_count == 0:
                cache.set(dup_key, 1, timeout=600)
            else:
                cache.incr(dup_key)
        country_name, country_code, city_name = _resolve_country_for_request(request, ip)
        if _country_blocked(org, country_name, country_code):
            return JsonResponse({'error': 'Widget is blocked in your country.', 'blocked': True}, status=403)
        ua = request.META.get('HTTP_USER_AGENT', '')
        browser, os_name, device_type = parse_user_agent(ua)
        visitor, _ = _resolve_or_create_visitor(
            org=org, ip=ip, ua=ua, session_key=session_key, visitor_fingerprint=visitor_fingerprint,
            website=website,
            defaults={
                'browser': browser, 'os': os_name, 'device_type': device_type,
                'is_online': True,
                'country': country_name, 'city': city_name,
            },
        )
        if (country_name and not visitor.country) or (city_name and not visitor.city):
            Visitor.objects.filter(pk=visitor.pk).update(
                country=visitor.country or country_name,
                city=visitor.city or city_name,
            )
            visitor.country = visitor.country or country_name
            visitor.city = visitor.city or city_name
        # Use the canonical session_key going forward (the one stored on the visitor)
        if visitor.session_key:
            session_key = visitor.session_key
        if visitor.is_banned:
            return JsonResponse({'error': 'Chat disabled for this visitor. Please contact support.'}, status=403)

        # Sweep very old abandoned chats only (24h+)  never close active visitor sessions early.
        close_stale_chats(inactive_minutes=24 * 60)

        # Reuse existing open chat for the same visitor  visitor stays in the SAME room
        # until they explicitly end it. No auto-close on the visitor's side.
        open_room = (
            ChatRoom.objects
            .filter(visitor=visitor, status__in=['waiting', 'active'])
            .annotate(last_message_at=Max('messages__timestamp'))
            .order_by('-updated_at')
            .first()
        )
        if open_room:
            messages = []
            for msg in open_room.messages.order_by('timestamp')[:200]:
                messages.append({
                    'sender_type': msg.sender_type,
                    'sender_name': msg.sender_name,
                    'content': msg.content,
                    'msg_type': msg.msg_type,
                    'file_name': msg.file_name,
                    'file_url': msg.file.url if msg.file else '',
                    'timestamp': msg.timestamp.isoformat(),
                })
            return JsonResponse({
                'room_id': open_room.room_id,
                'status': open_room.status,
                'ws_token': create_ws_token(open_room.room_id, 'visitor', session_key),
                'reused': True,
                'messages': messages,
                'session_key': session_key,
            })

        # If restore_only mode, don't create new chat - just return no existing chat
        if data.get('restore_only'):
            return JsonResponse({'reused': False, 'messages': [], 'session_key': session_key})

        room_id = uuid.uuid4().hex[:12]
        visitor_name = data.get('name', 'Visitor') or 'Visitor'
        room = ChatRoom.objects.create(
            organization=org,
            website=website,
            room_id=room_id,
            visitor=visitor,
            visitor_name=visitor_name,
            visitor_email=data.get('email', ''),
            subject=data.get('subject', ''),
            status='waiting',
        )
        assigned = auto_assign_agent(room)

        # Notify dashboard agents about new chat
        from tracker.chat.notifications import send_dashboard_notification
        subject_preview = (data.get('subject', '') or '')[:60]
        chat_body = f'{visitor_name} started a chat'
        if subject_preview:
            chat_body += f': {subject_preview}'
        send_dashboard_notification(
            org_id=org.id,
            category='new_chat',
            title='New Chat',
            body=chat_body,
            severity='warning',
            url=f'/dashboard/chats/{room_id}/',
            sound=True,
            website=website,
        )
        # Extra alert if no agent was assigned
        if not assigned:
            send_dashboard_notification(
                org_id=org.id,
                category='no_agents',
                title='No Agents Available',
                body=f'{visitor_name} is waiting - no agents online to handle this chat',
                severity='error',
                url=f'/dashboard/chats/{room_id}/',
                sound=True,
            )

        # Fire webhook for new chat
        from tracker.dashboard.views import fire_webhook
        fire_webhook(org, 'chat.created', {
            'event': 'chat.created',
            'room_id': room_id,
            'visitor_name': visitor_name,
            'visitor_email': data.get('email', ''),
            'subject': data.get('subject', ''),
        })

        # Save welcome message to DB so agent dashboard also shows it
        from tracker.chat.models import Message
        now = timezone.now()
        Message.objects.create(
            room=room,
            sender_type='system',
            sender_name='System',
            content=_get_time_greeting(visitor_name),
            msg_type='text',
            timestamp=now,
        )

        # Send email notification for new chat
        if org and org.notify_on_new_chat and org.notify_email:
            from tracker.core.email_utils import send_new_chat_notification
            send_new_chat_notification(
                org,
                visitor_name=visitor_name,
                subject=data.get('subject', '-'),
                room_id=room_id,
                dashboard_url=request.build_absolute_uri('/dashboard/'),
            )

        # Save the visitor's initial message (subject/query) as their first chat message
        subject_text = (data.get('subject') or '').strip()
        if subject_text:
            Message.objects.create(
                room=room,
                sender_type='visitor',
                sender_name=visitor_name,
                content=subject_text,
                msg_type='text',
                timestamp=now + timedelta(seconds=1),
            )

        # Notify dashboard clients to refresh badge counts in real-time
        channel_layer = get_channel_layer()
        dashboard_group = f'dashboard_updates_{org.id}' if org else 'dashboard_updates'
        async_to_sync(channel_layer.group_send)(
            dashboard_group,
            {
                'type': 'dashboard_update',
                'reason': 'new_chat',
                'room_id': room_id,
            }
        )

        # Build messages list to return to widget
        welcome_messages = [
            {
                'sender_type': 'system',
                'sender_name': 'System',
                'content': _get_time_greeting(visitor_name),
                'msg_type': 'text',
                'file_name': '',
                'file_url': '',
                'timestamp': now.isoformat(),
            },
        ]
        if subject_text:
            welcome_messages.append({
                'sender_type': 'visitor',
                'sender_name': visitor_name,
                'content': subject_text,
                'msg_type': 'text',
                'file_name': '',
                'file_url': '',
                'timestamp': (now + timedelta(seconds=1)).isoformat(),
            })
        # Queue position
        queue_position = ChatRoom.objects.filter(
            organization=org, status='waiting', created_at__lt=room.created_at
        ).count() + 1

        return JsonResponse({
            'room_id': room.room_id,
            'status': 'waiting',
            'ws_token': create_ws_token(room.room_id, 'visitor', session_key),
            'reused': False,
            'messages': welcome_messages,
            'queue_position': queue_position,
            'session_key': session_key,
        })
    return JsonResponse({'error': 'POST required'}, status=405)


@csrf_exempt
def chat_file_upload(request, room_id):
    """Handle file uploads in chat."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    if _rate_limit(request, 'chat_file_upload', limit=20, window_seconds=60):
        return JsonResponse({'error': 'Too many uploads. Please wait and try again.'}, status=429)

    from tracker.chat.models import ChatRoom, Message

    try:
        room = ChatRoom.objects.select_related('visitor').get(room_id=room_id)
    except ChatRoom.DoesNotExist:
        return JsonResponse({'error': 'Room not found'}, status=404)

    actor = _resolve_room_actor(request, room)
    if not actor:
        return JsonResponse({'error': 'Unauthorized for this room'}, status=403)

    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        return JsonResponse({'error': 'No file provided'}, status=400)

    if uploaded_file.size > MAX_UPLOAD_SIZE:
        return JsonResponse({'error': 'File too large (max 10MB)'}, status=400)

    original_name = uploaded_file.name
    ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
    mime_type = (uploaded_file.content_type or '').lower()

    if ext not in ALLOWED_FILE_EXTENSIONS and ext not in ALLOWED_IMAGE_EXTENSIONS:
        return JsonResponse({'error': 'File type not allowed'}, status=400)
    if mime_type and mime_type not in ALLOWED_MIME_TYPES:
        return JsonResponse({'error': 'Unsupported file MIME type'}, status=400)

    uploaded_file.name = f"{uuid.uuid4().hex}.{ext or 'bin'}"
    is_image = ext in ALLOWED_IMAGE_EXTENSIONS

    msg = Message.objects.create(
        room=room,
        sender_type=actor['sender_type'],
        sender_name=actor['sender_name'],
        content=original_name,
        msg_type='image' if is_image else 'file',
        file=uploaded_file,
        file_name=original_name,
    )
    room.save(update_fields=['updated_at'])

    return JsonResponse({
        'status': 'ok',
        'message_id': msg.id,
        'file_url': msg.file.url,
        'file_name': msg.file_name,
        'msg_type': msg.msg_type,
        'sender_type': actor['sender_type'],
        'sender_name': actor['sender_name'],
        'timestamp': msg.timestamp.isoformat(),
    })


def widget_chat_transcript(request, room_id):
    """Public transcript download for the visitor who owns the chat (no agent login required).
    Auth: visitor must present their session_key as a query param OR own the Django session
    that matches the room's visitor."""
    from tracker.chat.models import ChatRoom
    try:
        room = ChatRoom.objects.select_related('visitor', 'organization', 'agent').get(room_id=room_id)
    except ChatRoom.DoesNotExist:
        return HttpResponse('Not found', status=404)

    sk = (request.GET.get('sk') or '').strip()[:64]
    cookie_sk = request.session.session_key or ''
    if not (sk and room.visitor and sk == room.visitor.session_key) and \
       not (cookie_sk and room.visitor and cookie_sk == room.visitor.session_key):
        return HttpResponse('Forbidden', status=403)

    lines = [
        f'Chat Transcript - {room.visitor_name or "Visitor"}',
        f'Room: {room.room_id}',
        f'Date: {room.created_at.strftime("%Y-%m-%d %H:%M")}',
        f'Agent: {room.agent.get_full_name() if room.agent else "Unassigned"}',
        f'Status: {room.status}',
        '-' * 50,
        '',
    ]
    for msg in room.messages.order_by('timestamp'):
        time_str = msg.timestamp.strftime('%H:%M')
        lines.append(f'[{time_str}] {msg.sender_name} ({msg.sender_type}): {msg.content}')
        if msg.file:
            lines.append(f'  [File: {msg.file_name}]')

    response = HttpResponse('\n'.join(lines), content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="transcript_{room.room_id}.txt"'
    return response


@csrf_exempt
def chat_rate(request, room_id):
    """Visitor rates a chat after it ends."""
    if request.method == 'POST':
        from tracker.chat.models import ChatRoom

        data = _parse_json_body(request)
        if data is None:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)

        try:
            room = ChatRoom.objects.select_related('visitor').get(room_id=room_id)
        except ChatRoom.DoesNotExist:
            return JsonResponse({'error': 'Room not found'}, status=404)

        if request.session.session_key != room.visitor.session_key:
            return JsonResponse({'error': 'Unauthorized'}, status=403)

        try:
            rating = int(data.get('rating', 0))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'Rating must be a number from 1 to 5'}, status=400)

        if rating < 1 or rating > 5:
            return JsonResponse({'error': 'Rating must be between 1 and 5'}, status=400)

        room.rating = rating
        room.rating_feedback = data.get('feedback', '')
        room.save(update_fields=['rating', 'rating_feedback'])
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'POST required'}, status=405)


@csrf_exempt
def submit_offline_message(request):
    """Submit a message when no agents are online."""
    if request.method == 'POST':
        from tracker.chat.models import OfflineMessage
        from tracker.visitors.middleware import get_client_ip

        data = _parse_json_body(request)
        if data is None:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)
        if _is_honeypot_triggered(data):
            return JsonResponse({'error': 'Unable to submit right now.'}, status=400)

        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        message = data.get('message', '').strip()
        if not name or not email or not message:
            return JsonResponse({'error': 'Name, email, and message are required'}, status=400)
        try:
            validate_email(email)
        except ValidationError:
            return JsonResponse({'error': 'Invalid email address'}, status=400)

        org = _get_org_from_request(request)
        if _adaptive_rate_limit(request, 'offline_message', org=org, base_limit=5, window_seconds=600):
            msg = org.attack_mode_message if getattr(org, 'attack_mode_enabled', False) else 'Too many messages. Please try again later.'
            return JsonResponse({'error': msg}, status=429)
        if _spam_score(message) >= 4:
            return JsonResponse({'error': 'Message blocked for safety checks. Please rephrase and try again.'}, status=429)
        OfflineMessage.objects.create(
            organization=org,
            name=name,
            email=email,
            message=message,
            ip_address=get_client_ip(request),
        )
        # Notify dashboard agents about offline message
        if org:
            from tracker.chat.notifications import send_dashboard_notification
            send_dashboard_notification(
                org_id=org.id,
                category='offline_message',
                title='Offline Message',
                body=f'{name} ({email}): {message[:80]}',
                severity='info',
                url='/dashboard/chats/?tab=offline',
                sound=True,
            )
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'POST required'}, status=405)


from django.views.decorators.clickjacking import xframe_options_exempt

@xframe_options_exempt
def widget_embed_page(request):
    """Standalone widget page  loaded inside iframe on external sites."""
    widget_key = request.GET.get('key', '')
    from tracker.core.models import Organization
    org = Organization.objects.filter(widget_key=widget_key).first() if widget_key else Organization.objects.first()
    parent_domain = _extract_parent_domain(request, {})

    # Show a clear setup error when the key is invalid (instead of a blank panel).
    if widget_key and not org:
        html = f"""<!DOCTYPE html>
<html><head><meta charset='UTF-8'><title>Widget setup needed</title>
<style>
  body{{margin:0;font-family:-apple-system,Inter,Arial,sans-serif;background:#fff;color:#1f2937;height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;text-align:center;}}
  .box{{max-width:320px}}
  h2{{font-size:16px;margin:0 0 8px;color:#dc2626}}
  p{{font-size:13px;line-height:1.5;color:#6b7280;margin:0 0 12px}}
  code{{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:11px;word-break:break-all}}
  .icon{{font-size:36px;margin-bottom:8px}}
</style></head>
<body>
  <div class='box'>
    <div class='icon'>??</div>
    <h2>Widget not configured</h2>
    <p>The widget key in your embed script does not match any organization on this server.</p>
    <p>Provided key:<br><code>{widget_key[:64]}</code></p>
    <p style='font-size:11px;margin-top:14px'>Log in to your dashboard ? <b>Settings ? Widget</b> to copy the correct key.</p>
  </div>
</body></html>"""
        from django.http import HttpResponse
        return HttpResponse(html, content_type='text/html; charset=utf-8')

    if org and not _domain_allowed(org, parent_domain):
        return HttpResponse(
            "<!DOCTYPE html><html><body style='font-family:Inter,Arial,sans-serif;padding:16px;color:#6b7280;'>"
            "Widget is blocked on this domain.</body></html>",
            content_type='text/html; charset=utf-8',
            status=403,
        )

    if org:
        ip = _client_ip(request)
        country_name, country_code, _city_name = _resolve_country_for_request(request, ip)
        if _country_blocked(org, country_name, country_code):
            return HttpResponse(
                "<!DOCTYPE html><html><body style='font-family:Inter,Arial,sans-serif;padding:16px;color:#6b7280;'>"
                "Widget is unavailable in your country.</body></html>",
                content_type='text/html; charset=utf-8',
                status=403,
            )

    # Prefer session_key passed by parent script (for cross-origin where cookies are blocked).
    # Fall back to Django session cookie, then create one as a last resort.
    parent_sk = (request.GET.get('sk') or '').strip()[:64]
    if parent_sk:
        session_key = parent_sk
    else:
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key
    return render(request, 'core/widget_embed.html', {
        'org': org,
        'widget_key': widget_key,
        'session_key': session_key,
        'parent_domain': parent_domain,
    })


def landing_page(request):
    """Demo landing page to test the chat widget."""
    status = request.GET.get('status', '').strip().lower()
    if status in {'active', 'waiting'}:
        return redirect(f'/dashboard/chats/?status={status}')
    # Ensure session exists so chat restore works on page load
    if not request.session.session_key:
        request.session.create()
    # Get default org widget key for the demo landing page
    from tracker.core.models import Organization
    org = Organization.objects.first()
    widget_key = org.widget_key if org else ''
    return render(request, 'core/landing.html', {'widget_key': widget_key})


def home_redirect(request):
    """Send users to dashboard/login from root URL."""
    if request.user.is_authenticated:
        return redirect('dashboard:home')
    return redirect('core:login')
