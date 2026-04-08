import json
import logging
import uuid
from datetime import timedelta

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


def _resolve_room_actor(request, room):
    if request.user.is_authenticated:
        if room.agent_id and room.agent_id != request.user.id and not request.user.is_superuser:
            return None
        sender_name = request.user.get_full_name() or request.user.username
        return {'sender_type': 'agent', 'sender_name': sender_name}

    session_key = request.session.session_key
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
    data = _parse_json_body(request) if request.body else {}
    key = (data or {}).get('key') or request.GET.get('key') or ''
    if key:
        org = Organization.objects.filter(widget_key=key).first()
        if org:
            return org
    # Fallback to first org (for backward compatibility / landing page without key)
    return Organization.objects.first()


@csrf_exempt
def widget_init(request):
    """Initialize chat widget - creates visitor session and returns config."""
    if request.method == 'POST':
        if not request.session.session_key:
            request.session.create()

        org = _get_org_from_request(request)

        from tracker.visitors.middleware import get_client_ip, parse_user_agent
        from tracker.visitors.models import Visitor

        session_key = request.session.session_key
        ip = get_client_ip(request)
        ua = request.META.get('HTTP_USER_AGENT', '')
        browser, os_name, device_type = parse_user_agent(ua)

        visitor, _ = Visitor.objects.get_or_create(
            session_key=session_key,
            organization=org,
            defaults={
                'ip_address': ip,
                'user_agent': ua,
                'browser': browser,
                'os': os_name,
                'device_type': device_type,
            },
        )

        return JsonResponse({
            'session_key': session_key,
            'visitor_id': visitor.id,
            'welcome_message': org.welcome_message if org else 'Hi! How can we help you?',
            'widget_color': org.widget_color if org else '#7c3aed',
        })
    return JsonResponse({'error': 'POST required'}, status=405)


@csrf_exempt
def widget_track_pageview(request):
    """Record a page view from the embedded widget on a customer's website."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    if _rate_limit(request, 'widget_track', limit=120, window_seconds=60):
        return JsonResponse({'error': 'Too many requests'}, status=429)

    data = _parse_json_body(request) or {}
    url = (data.get('url') or '')[:500]
    title = (data.get('title') or '')[:300]
    referrer = (data.get('referrer') or '')[:500]
    body_session = data.get('session_key', '')

    if not url:
        return JsonResponse({'error': 'url required'}, status=400)

    org = _get_org_from_request(request)
    if not org:
        return JsonResponse({'error': 'org not found'}, status=404)

    from tracker.visitors.middleware import (
        get_client_ip, parse_user_agent, get_referrer_source,
    )
    from tracker.visitors.models import Visitor, PageView

    # Resolve session: cookie first, fallback to body session_key (cross-origin)
    if not request.session.session_key:
        request.session.create()
    session_key = body_session or request.session.session_key

    ip = get_client_ip(request)
    ua = request.META.get('HTTP_USER_AGENT', '')
    browser, os_name, device_type = parse_user_agent(ua)

    visitor, created = Visitor.objects.get_or_create(
        session_key=session_key,
        organization=org,
        defaults={
            'ip_address': ip,
            'user_agent': ua,
            'browser': browser,
            'os': os_name,
            'device_type': device_type,
            'referrer': referrer,
            'referrer_source': get_referrer_source(referrer),
            'is_online': True,
            'landing_page': url,
        },
    )

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
    widget_key = request.GET.get('key', '')

    # Load org customization
    from tracker.core.models import Organization
    org = Organization.objects.filter(widget_key=widget_key).first() if widget_key else None
    widget_color = org.widget_color if org else '#7c3aed'
    widget_title = org.widget_title if org else 'LiveTrack Support'
    widget_position = org.widget_position if org else 'bottom-right'
    pos_css = 'left:24px' if widget_position == 'bottom-left' else 'right:24px'
    panel_pos_css = 'left:24px' if widget_position == 'bottom-left' else 'right:24px'

    js = r"""
(function() {
  if (window.LiveTrackWidgetLoaded) return;
  window.LiveTrackWidgetLoaded = true;
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

  // ===== Page view tracking =====
  var lastTrackedUrl = "";
  function trackPageView() {
    var url = location.href;
    if (url === lastTrackedUrl) return;
    lastTrackedUrl = url;
    var payload = {
      key: WIDGET_KEY,
      session_key: getSessionKey(),
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
        if (d && d.session_key) setSessionKey(d.session_key);
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

  var style = document.createElement("style");
  style.textContent = ".ltw-btn{position:fixed;__POS_CSS__;bottom:24px;z-index:999999;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;color:#fff;font-size:22px;background:"+WC+";box-shadow:0 8px 24px rgba(0,0,0,.2);transition:all .3s;display:flex;align-items:center;justify-content:center;}.ltw-btn:hover{transform:scale(1.08);box-shadow:0 12px 32px rgba(0,0,0,.3)}.ltw-frame{position:fixed;__PANEL_POS_CSS__;bottom:94px;z-index:999999;width:min(400px,calc(100vw - 24px));height:min(600px,calc(100vh - 120px));border:none;border-radius:20px;box-shadow:0 20px 60px rgba(0,0,0,.15),0 0 0 1px rgba(0,0,0,.04);display:none;background:white;overflow:hidden;}@media(max-width:480px){.ltw-btn{width:48px;height:48px;font-size:18px;bottom:16px}.ltw-frame{bottom:72px;width:calc(100vw - 16px);height:calc(100vh - 88px);border-radius:16px}}";
  document.head.appendChild(style);

  var btn = document.createElement("button");
  btn.className = "ltw-btn";
  btn.innerHTML = "💬";

  var frame = document.createElement("iframe");
  frame.className = "ltw-frame";
  // Pass current session_key into iframe so chat & tracking share the same visitor
  var _sk = encodeURIComponent(getSessionKey() || "");
  frame.src = BASE + "/api/widget/embed/?key=" + WIDGET_KEY + (_sk ? "&sk=" + _sk : "");
  frame.allow = "microphone;camera;display-capture";

  document.body.appendChild(btn);
  document.body.appendChild(frame);

  function closePanel() {
    isOpen = false;
    frame.style.display = "none";
    btn.innerHTML = "💬";
    btn.style.fontSize = "22px";
  }

  btn.onclick = function() {
    isOpen = !isOpen;
    frame.style.display = isOpen ? "block" : "none";
    btn.innerHTML = isOpen ? "✕" : "💬";
    btn.style.fontSize = isOpen ? "18px" : "22px";
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
        btn.innerHTML = "✕";
        btn.style.fontSize = "18px";
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
    return HttpResponse(js, content_type='application/javascript; charset=utf-8')


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
        if _rate_limit(request, 'widget_start_chat', limit=10, window_seconds=60):
            return JsonResponse({'error': 'Too many requests. Please wait and try again.'}, status=429)

        data = _parse_json_body(request)
        if data is None:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)

        # Get session — prefer cookie, fallback to session_key in body (cross-origin widget)
        if not request.session.session_key:
            request.session.create()
        session_key = request.session.session_key

        # Cross-origin fallback: widget passes session_key from init response
        body_session = data.get('session_key', '')
        if body_session:
            session_key = body_session

        # Resolve org from widget key
        org = _get_org_from_request(request)

        from tracker.visitors.models import Visitor
        from tracker.chat.models import ChatRoom
        from django.db.models import Max

        try:
            visitor = Visitor.objects.get(session_key=session_key, organization=org)
        except Visitor.DoesNotExist:
            # Fallback: create visitor on-the-fly for cross-origin
            from tracker.visitors.middleware import get_client_ip, parse_user_agent
            ip = get_client_ip(request)
            ua = request.META.get('HTTP_USER_AGENT', '')
            browser, os_name, device_type = parse_user_agent(ua)
            visitor = Visitor.objects.create(
                session_key=session_key, organization=org,
                ip_address=ip, user_agent=ua, browser=browser,
                os=os_name, device_type=device_type, is_online=True,
            )
        if visitor.is_banned:
            return JsonResponse({'error': 'Chat disabled for this visitor. Please contact support.'}, status=403)

        # Sweep very old abandoned chats only (24h+) — never close active visitor sessions early.
        close_stale_chats(inactive_minutes=24 * 60)

        # Reuse existing open chat for the same visitor — visitor stays in the SAME room
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
            room_id=room_id,
            visitor=visitor,
            visitor_name=visitor_name,
            visitor_email=data.get('email', ''),
            subject=data.get('subject', ''),
            status='waiting',
        )
        auto_assign_agent(room)

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
            try:
                from django.core.mail import send_mail
                send_mail(
                    f'New chat from {visitor_name} - {org.name}',
                    f'New chat started by {visitor_name}.\nSubject: {data.get("subject", "-")}\nRoom: {room_id}\n\nLogin to respond: {request.build_absolute_uri("/dashboard/")}',
                    'noreply@livetrack.app',
                    [org.notify_email],
                    fail_silently=True,
                )
            except Exception:
                pass

        # Save the visitor's initial message (subject/query) as their first chat message
        subject_text = data.get('subject', '').strip()
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
        if _rate_limit(request, 'offline_message', limit=5, window_seconds=600):
            return JsonResponse({'error': 'Too many messages. Please try again later.'}, status=429)

        from tracker.chat.models import OfflineMessage
        from tracker.visitors.middleware import get_client_ip

        data = _parse_json_body(request)
        if data is None:
            return JsonResponse({'error': 'Invalid JSON body'}, status=400)

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
        OfflineMessage.objects.create(
            organization=org,
            name=name,
            email=email,
            message=message,
            ip_address=get_client_ip(request),
        )
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'POST required'}, status=405)


from django.views.decorators.clickjacking import xframe_options_exempt

@xframe_options_exempt
def widget_embed_page(request):
    """Standalone widget page — loaded inside iframe on external sites."""
    widget_key = request.GET.get('key', '')
    from tracker.core.models import Organization
    org = Organization.objects.filter(widget_key=widget_key).first() if widget_key else Organization.objects.first()
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
