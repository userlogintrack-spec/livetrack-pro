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

        if not username or not password:
            messages.error(request, 'Username and password are required.')
            return render(request, 'core/register.html')

        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
            return render(request, 'core/register.html')

        if email and User.objects.filter(email=email).exists():
            messages.error(request, 'Email already registered.')
            return render(request, 'core/register.html')

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
        login(request, user)
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
  var roomId = null, wsToken = "", socket = null, visitorName = "Visitor";
  var chatRestored = false;

  var style = document.createElement("style");
  var WC = "__WIDGET_COLOR__";
  style.textContent = ".ltw-btn{position:fixed;__POS_CSS__;bottom:24px;z-index:999999;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;color:#fff;font-size:20px;font-weight:800;background:"+WC+";box-shadow:0 12px 30px rgba(0,0,0,.25)}.ltw-panel{position:fixed;__PANEL_POS_CSS__;bottom:94px;z-index:999999;width:min(380px,calc(100vw - 24px));max-height:min(560px,calc(100vh - 120px));background:#fff;border:1px solid #e5e7eb;border-radius:16px;overflow:hidden;box-shadow:0 18px 40px rgba(0,0,0,.18);display:none;flex-direction:column;font-family:Inter,Arial,sans-serif}.ltw-head{padding:14px 16px;color:#fff;display:flex;justify-content:space-between;align-items:center;background:"+WC+"}.ltw-title{font-size:15px;font-weight:700}.ltw-close{border:0;background:rgba(255,255,255,.2);color:#fff;border-radius:8px;width:28px;height:28px;cursor:pointer}.ltw-body{padding:14px;overflow:auto;background:#fafbff;display:flex;flex-direction:column;gap:8px;min-height:200px}.ltw-msg{max-width:82%;font-size:13px;padding:9px 12px;border-radius:12px;line-height:1.45}.ltw-agent{align-self:flex-start;background:#eef2ff;color:#1f2937}.ltw-visitor{align-self:flex-end;background:"+WC+";color:#fff}.ltw-form{padding:12px;border-top:1px solid #e5e7eb;display:grid;gap:8px;background:#fff}.ltw-form input{width:100%;border:1px solid #d1d5db;border-radius:10px;padding:10px;font-size:13px}.ltw-send-row{display:flex;gap:8px}.ltw-send-row input{flex:1}.ltw-send{border:0;border-radius:10px;padding:0 14px;cursor:pointer;color:#fff;background:"+WC+"}.ltw-newchat{border:0;border-radius:10px;padding:10px 16px;cursor:pointer;color:#fff;font-size:13px;font-weight:600;background:"+WC+";width:100%}.ltw-powered{padding:6px;text-align:center;font-size:10px;color:#9ca3af;background:#fafafa;border-top:1px solid #f0f0f0}.ltw-powered a{color:#6366f1;text-decoration:none;font-weight:600}";
  document.head.appendChild(style);

  var btn = document.createElement("button");
  btn.className = "ltw-btn";
  btn.innerHTML = "💬";

  var panel = document.createElement("div");
  panel.className = "ltw-panel";
  panel.innerHTML = '<div class="ltw-head"><div class="ltw-title">__WIDGET_TITLE__</div><button class="ltw-close" aria-label="Close">×</button></div><div class="ltw-body" id="ltwBody"></div><div class="ltw-form" id="ltwPrechat"><input id="ltwName" placeholder="Your name"/><input id="ltwEmail" placeholder="Your email (optional)"/><input id="ltwSubject" placeholder="Your query"/><button class="ltw-send" id="ltwStart">Start Chat</button></div><div class="ltw-form" id="ltwChat" style="display:none;"><div class="ltw-send-row"><input id="ltwInput" placeholder="Type your message..."/><button class="ltw-send" id="ltwSend">Send</button></div></div><div class="ltw-form" id="ltwClosed" style="display:none;"><button class="ltw-newchat" id="ltwNewChat">Start New Chat</button></div><div class="ltw-powered">Powered by <a href="__BASE__" target="_blank">LiveTrack</a></div>';
  document.body.appendChild(btn);
  document.body.appendChild(panel);

  function addMessage(text, cls, timestamp) {
    var body = panel.querySelector("#ltwBody");
    var node = document.createElement("div");
    node.className = "ltw-msg " + cls;
    node.textContent = text || "";
    if (timestamp) {
      var t = new Date(timestamp);
      var timeEl = document.createElement("div");
      timeEl.style.cssText = "font-size:10px;opacity:0.6;margin-top:3px;";
      timeEl.textContent = t.toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});
      node.appendChild(timeEl);
    }
    body.appendChild(node);
    body.scrollTop = body.scrollHeight;
  }

  function loadPreviousMessages(messages) {
    var body = panel.querySelector("#ltwBody");
    body.innerHTML = "";
    for (var i = 0; i < messages.length; i++) {
      var m = messages[i];
      var cls = m.sender_type === "visitor" ? "ltw-visitor" : "ltw-agent";
      if (m.msg_type === "file" || m.msg_type === "image") {
        addMessage(m.file_name || "File", cls, m.timestamp);
      } else {
        addMessage(m.content, cls, m.timestamp);
      }
    }
  }

  function post(path, payload) {
    var data = payload || {};
    data.key = WIDGET_KEY;
    return fetch(BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(data)
    }).then(function(r) { return r.json(); });
  }

  function openWs() {
    if (!roomId || !wsToken) return;
    var proto = location.protocol === "https:" ? "wss://" : "ws://";
    var wsHost = BASE.replace(/^https?:\/\//, "");
    socket = new WebSocket(proto + wsHost + "/ws/chat/" + roomId + "/?token=" + encodeURIComponent(wsToken));
    socket.onmessage = function(e) {
      var d = JSON.parse(e.data || "{}");
      if (d.type === "chat_message" && d.sender_type !== "visitor") addMessage(d.message, "ltw-agent");
      if (d.type === "chat_closed") {
        addMessage("Chat closed by agent.", "ltw-agent");
        panel.querySelector("#ltwChat").style.display = "none";
        panel.querySelector("#ltwClosed").style.display = "grid";
        if (socket) { socket.close(); socket = null; }
        roomId = null; wsToken = "";
      }
    };
  }

  btn.onclick = function() { panel.style.display = panel.style.display === "flex" ? "none" : "flex"; };
  panel.querySelector(".ltw-close").onclick = function() { panel.style.display = "none"; };

  function startOrResumeChat(name, email, subject) {
    visitorName = (name || "Visitor").trim() || "Visitor";
    post("/api/widget/init/", {})
      .then(function() { return post("/api/widget/start-chat/", { name: visitorName, email: email || "", subject: subject || "" }); })
      .then(function(data) {
        if (data.error) { addMessage(data.error, "ltw-agent"); return; }
        roomId = data.room_id; wsToken = data.ws_token || "";
        panel.querySelector("#ltwPrechat").style.display = "none";
        panel.querySelector("#ltwChat").style.display = "block";
        panel.querySelector("#ltwClosed").style.display = "none";
        if (data.messages && data.messages.length > 0) {
          loadPreviousMessages(data.messages);
        }
        openWs();
      });
  }

  panel.querySelector("#ltwStart").onclick = function() {
    var name = panel.querySelector("#ltwName").value;
    var email = (panel.querySelector("#ltwEmail").value || "").trim();
    var subject = (panel.querySelector("#ltwSubject").value || "").trim();
    startOrResumeChat(name, email, subject);
  };

  function sendMsg() {
    if (!socket) return;
    var input = panel.querySelector("#ltwInput");
    var text = (input.value || "").trim();
    if (!text) return;
    socket.send(JSON.stringify({ type: "chat_message", message: text, sender_type: "visitor", sender_name: visitorName, msg_type: "text" }));
    addMessage(text, "ltw-visitor");
    input.value = "";
  }

  panel.querySelector("#ltwSend").onclick = sendMsg;
  panel.querySelector("#ltwInput").addEventListener("keydown", function(e) { if (e.key === "Enter") sendMsg(); });

  panel.querySelector("#ltwNewChat").onclick = function() {
    panel.querySelector("#ltwClosed").style.display = "none";
    panel.querySelector("#ltwBody").innerHTML = "";
    panel.querySelector("#ltwPrechat").style.display = "grid";
    panel.querySelector("#ltwName").value = visitorName !== "Visitor" ? visitorName : "";
  };

  // Auto-restore existing chat on widget load
  function tryRestoreChat() {
    if (chatRestored) return;
    chatRestored = true;
    post("/api/widget/init/", {})
      .then(function() { return post("/api/widget/start-chat/", { name: "", email: "", subject: "", restore_only: true }); })
      .then(function(data) {
        if (data.reused && data.messages && data.messages.length > 0) {
          roomId = data.room_id; wsToken = data.ws_token || "";
          visitorName = "Visitor";
          for (var i = data.messages.length - 1; i >= 0; i--) {
            if (data.messages[i].sender_type === "visitor" && data.messages[i].sender_name) {
              visitorName = data.messages[i].sender_name;
              break;
            }
          }
          panel.querySelector("#ltwPrechat").style.display = "none";
          panel.querySelector("#ltwChat").style.display = "block";
          loadPreviousMessages(data.messages);
          openWs();
        }
      })
      .catch(function() {});
  }
  tryRestoreChat();

  // Proactive chat trigger
  if ("__PROACTIVE__" === "true") {
    setTimeout(function() {
      if (!roomId && panel.style.display !== "flex") {
        btn.style.animation = "ltw-pulse 1.5s ease infinite";
        var notif = document.createElement("div");
        notif.style.cssText = "position:fixed;__PANEL_POS_CSS__;bottom:90px;z-index:999999;background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:12px 16px;box-shadow:0 8px 24px rgba(0,0,0,0.12);font-family:Inter,Arial,sans-serif;max-width:260px;cursor:pointer;animation:ltw-fadein .3s;";
        notif.innerHTML = '<div style="font-size:13px;font-weight:600;color:#1f2937;">__PROACTIVE_MSG__</div><div style="font-size:11px;color:#9ca3af;margin-top:4px;">Click to chat with us</div>';
        notif.onclick = function() { notif.remove(); panel.style.display = "flex"; };
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

        session_key = request.session.session_key
        if not session_key:
            return JsonResponse({'error': 'No session'}, status=400)

        # Resolve org from widget key
        org = _get_org_from_request(request)

        from tracker.visitors.models import Visitor
        from tracker.chat.models import ChatRoom
        from django.db.models import Max

        try:
            visitor = Visitor.objects.get(session_key=session_key, organization=org)
        except Visitor.DoesNotExist:
            return JsonResponse({'error': 'Visitor not found'}, status=404)
        if visitor.is_banned:
            return JsonResponse({'error': 'Chat disabled for this visitor. Please contact support.'}, status=403)

        # Auto-close globally stale chats (30 min inactivity).
        close_stale_chats(inactive_minutes=30)

        # Reuse existing open chat for same visitor if still active recently.
        open_room = (
            ChatRoom.objects
            .filter(visitor=visitor, status__in=['waiting', 'active'])
            .annotate(last_message_at=Max('messages__timestamp'))
            .order_by('-updated_at')
            .first()
        )
        if open_room:
            recent_cutoff = timezone.now() - timedelta(minutes=30)
            last_activity = open_room.last_message_at or open_room.updated_at or open_room.created_at
            if last_activity >= recent_cutoff:
                messages = []
                for msg in open_room.messages.order_by('timestamp')[:100]:
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
                })

            open_room.status = 'closed'
            open_room.closed_at = timezone.now()
            open_room.save(update_fields=['status', 'closed_at', 'updated_at'])

        # If restore_only mode, don't create new chat - just return no existing chat
        if data.get('restore_only'):
            return JsonResponse({'reused': False, 'messages': []})

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
