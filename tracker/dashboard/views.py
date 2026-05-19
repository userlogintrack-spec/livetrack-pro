import csv
import logging
import uuid
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Q, Avg, Sum, F, Max, Subquery, OuterRef
from django.db.models.functions import TruncDate
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime, timedelta
import json
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from tracker.visitors.models import (
    Visitor, PageView, CustomEvent, Goal, GoalCompletion, ScheduledReport,
    SessionRecording, ClickData, ScrollData, JSError, FrustrationSignal, PageInsight,
)
from tracker.chat.models import (
    ChatRoom, Message, AgentProfile, OfflineMessage, CannedResponse, VisitorNote,
    InternalNote, Webhook, WebhookDelivery, ActivityLog, ChatLabel, SavedReply,
    Department, DepartmentMember, SLAPolicy, SLABreach,
    Survey, SurveyQuestion, SurveyResponse, SurveyAnswer,
    AIBotConfig, AIBotKnowledge, ChatbotFlow,
    KBCategory, KBArticle, WhatsAppConfig, WhatsAppMessage, VisitorSegment,
    AgentWebsiteAccess, ChangelogEntry,
)
from tracker.chat.security import create_ws_token
from tracker.chat.utils import close_stale_chats, check_sla_breaches
from tracker.core.models import WebsiteSettings, Organization, Website, WebsiteGroup
from tracker.core.plan_gating import requires_feature
from tracker.core.views import _parse_json_body, get_user_org, _monthly_visitor_limit_state

logger = logging.getLogger(__name__)


class _Echo:
    """Minimal write-only file substitute that returns whatever it's handed —
    lets `csv.writer` produce one row at a time for StreamingHttpResponse."""
    def write(self, value):
        return value


def _stream_csv(rows, filename):
    """Stream a CSV response without buffering the whole queryset in memory.

    `rows` should be an iterable of lists. Pass a queryset.iterator() to keep
    DB cursor memory bounded too.
    """
    writer = csv.writer(_Echo())
    response = StreamingHttpResponse(
        (writer.writerow(row) for row in rows),
        content_type='text/csv',
    )
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


# ═══════ Website Filter Helper ═══════
def _read_selected_ids(request):
    """Read the session's selected website IDs as a list of ints.

    Supports two storage shapes for backward compat:
    - selected_website_ids: [1, 2, 3]  (new — multi-select)
    - selected_website_id: 1           (old — single-select; auto-migrated)
    Empty list = "All websites".
    """
    raw = request.session.get('selected_website_ids')
    if isinstance(raw, list):
        out = []
        for v in raw:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                continue
        return out
    legacy = request.session.get('selected_website_id')
    if legacy:
        try:
            return [int(legacy)]
        except (TypeError, ValueError):
            pass
    return []


def get_website_filter(request, org):
    """Return a dict filter for website-scoping dashboard queries.
    Owner/admin: selected websites (one, many, or all). Agent: only accessible websites."""
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))

    selected_ids = _read_selected_ids(request)

    if is_owner:
        if not selected_ids:
            return {}  # All websites
        # Validate selection against this org so a stale session can't read another org's data
        valid_ids = list(
            Website.objects.filter(id__in=selected_ids, organization=org).values_list('id', flat=True)
        )
        if not valid_ids:
            return {}
        if len(valid_ids) == 1:
            return {'website_id': valid_ids[0]}
        return {'website_id__in': valid_ids}

    # Agent: only accessible websites
    accessible_ids = list(
        AgentWebsiteAccess.objects.filter(agent=profile).values_list('website_id', flat=True)
    )
    if not accessible_ids:
        # Backward compat: agent with no access rows sees all (legacy agents)
        return {}
    # Intersect selection with what the agent can actually access
    if selected_ids:
        scoped = [i for i in selected_ids if i in accessible_ids]
        if len(scoped) == 1:
            return {'website_id': scoped[0]}
        if scoped:
            return {'website_id__in': scoped}
    return {'website_id__in': accessible_ids}


def get_selected_website(request, org):
    """Return a single selected Website object when exactly one is selected, else None.
    For multi-select or 'all', callers should use get_website_filter() directly."""
    ids = _read_selected_ids(request)
    if len(ids) == 1:
        return Website.objects.filter(id=ids[0], organization=org).first()
    return None


@login_required
def dashboard_home(request):
    org = get_user_org(request.user)

    # Check if org has any websites — if not, show setup screen instead of widgets
    org_websites = list(Website.objects.filter(organization=org).values('id', 'name', 'domain', 'tracking_key')) if org else []
    has_websites = len(org_websites) > 0
    if not has_websites:
        profile = getattr(request.user, 'agent_profile', None)
        is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
        return render(request, 'dashboard/home.html', {
            'has_websites': False,
            'org': org,
            'is_owner': is_owner,
        })

    # Close stale chats only once per minute (in-process gate — per-worker
    # firing is fine, cleanup is idempotent).
    from tracker.core import process_throttle
    if process_throttle.should_run(f'stale_check:{org.id if org else 0}', 60):
        close_stale_chats()
    now = timezone.now()
    sla_minutes = int(getattr(settings, 'CHAT_SLA_MINUTES', 5))
    sla_cutoff = now - timedelta(minutes=sla_minutes)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_30_min = now - timedelta(minutes=30)

    # Date range filter
    range_key = request.GET.get('range', '7d')
    range_map = {'24h': 1, '7d': 7, '30d': 30, '90d': 90}
    range_days = range_map.get(range_key, 7)
    period_start = now - timedelta(days=range_days)
    prev_period_start = period_start - timedelta(days=range_days)

    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))

    ws_filter = get_website_filter(request, org)
    visitors_qs = Visitor.objects.filter(organization=org, **ws_filter)
    pageviews_qs = PageView.objects.filter(visitor__organization=org, **{k.replace('website_id', 'visitor__website_id'): v for k, v in ws_filter.items()})
    chats_qs = ChatRoom.objects.filter(organization=org, **ws_filter)

    # Current period stats
    period_visitors = visitors_qs.filter(first_visit__gte=period_start)
    period_pageviews = pageviews_qs.filter(timestamp__gte=period_start)
    period_chats = chats_qs.filter(created_at__gte=period_start)

    total_visitors = visitors_qs.count()
    online_visitors = visitors_qs.filter(last_seen__gte=last_30_min).count()
    period_visitor_count = period_visitors.count()
    period_pageview_count = period_pageviews.count()
    total_chats = chats_qs.count()
    active_chats = chats_qs.filter(status__in=['waiting', 'active']).count()
    today_visitors = visitors_qs.filter(first_visit__gte=today_start).count()
    today_chats = chats_qs.filter(created_at__gte=today_start).count()
    today_page_views = pageviews_qs.filter(timestamp__gte=today_start).count()
    unread_offline = OfflineMessage.objects.filter(organization=org, is_read=False, **ws_filter).count()

    # Bounce rate & avg duration for period
    bounced_count = period_visitors.filter(is_bounced=True).count()
    bounce_rate = round((bounced_count / period_visitor_count * 100)) if period_visitor_count > 0 else 0
    avg_duration = period_visitors.filter(session_duration__gt=0).aggregate(avg=Avg('session_duration'))['avg'] or 0
    avg_dur_min = int(avg_duration) // 60
    avg_dur_sec = int(avg_duration) % 60

    # Previous period for comparison
    prev_visitors = visitors_qs.filter(first_visit__gte=prev_period_start, first_visit__lt=period_start).count()
    prev_pageviews = pageviews_qs.filter(timestamp__gte=prev_period_start, timestamp__lt=period_start).count()
    prev_bounced = visitors_qs.filter(first_visit__gte=prev_period_start, first_visit__lt=period_start, is_bounced=True).count()
    prev_bounce_rate = round((prev_bounced / prev_visitors * 100)) if prev_visitors > 0 else 0
    prev_chats_count = chats_qs.filter(created_at__gte=prev_period_start, created_at__lt=period_start).count()

    def _pct_change(current, previous):
        if previous == 0:
            return 100 if current > 0 else 0
        return round(((current - previous) / previous) * 100)

    visitor_change = _pct_change(period_visitor_count, prev_visitors)
    pageview_change = _pct_change(period_pageview_count, prev_pageviews)
    bounce_change = _pct_change(bounce_rate, prev_bounce_rate)
    chat_change = _pct_change(period_chats.count(), prev_chats_count)

    waiting_chats = chats_qs.filter(status='waiting').select_related('visitor')
    recent_visitors = visitors_qs.filter(last_seen__gte=last_30_min)[:10]

    browser_stats = period_visitors.exclude(browser='').values('browser').annotate(count=Count('id')).order_by('-count')[:10]
    device_stats = period_visitors.values('device_type').annotate(count=Count('id')).order_by('-count')
    os_stats = period_visitors.exclude(os='').values('os').annotate(count=Count('id')).order_by('-count')[:10]
    referrer_stats = period_visitors.values('referrer_source').annotate(count=Count('id')).order_by('-count')[:10]
    country_stats = period_visitors.exclude(country='').values('country').annotate(count=Count('id')).order_by('-count')[:10]
    city_stats = period_visitors.exclude(city='').values('city').annotate(count=Count('id')).order_by('-count')[:10]
    top_pages = period_pageviews.values('url').annotate(count=Count('visitor', distinct=True)).order_by('-count')[:10]
    entry_pages = period_pageviews.filter(is_entry=True).values('url').annotate(count=Count('visitor', distinct=True)).order_by('-count')[:10]
    exit_pages = period_pageviews.filter(is_exit=True).values('url').annotate(count=Count('visitor', distinct=True)).order_by('-count')[:10]

    # Daily chart data for the period
    daily_data = []
    for i in range(min(range_days, 30)):
        day_start = (now - timedelta(days=range_days - 1 - i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        v_count = visitors_qs.filter(first_visit__gte=day_start, first_visit__lt=day_end).count()
        pv_count = pageviews_qs.filter(timestamp__gte=day_start, timestamp__lt=day_end).count()
        daily_data.append({'date': day_start.strftime('%b %d'), 'visitors': v_count, 'views': pv_count})

    # Hourly data for today
    hourly_data = []
    for hour in range(24):
        hour_start = today_start.replace(hour=hour)
        hour_end = hour_start + timedelta(hours=1)
        count = pageviews_qs.filter(timestamp__gte=hour_start, timestamp__lt=hour_end).values('visitor').distinct().count()
        hourly_data.append({'hour': f'{hour:02d}:00', 'count': count})

    recent_chats = chats_qs.select_related('visitor', 'agent').order_by('-created_at')[:5]
    avg_rating = chats_qs.filter(rating__isnull=False).aggregate(avg=Avg('rating'))['avg']

    # CSAT breakdown
    rating_counts = {}
    for i in range(1, 6):
        rating_counts[i] = chats_qs.filter(rating=i).count()
    total_rated = sum(rating_counts.values())

    # Average response time (time from chat creation to first agent message)
    from django.db.models import Min, Subquery, OuterRef
    first_agent_msg = Message.objects.filter(
        room=OuterRef('pk'), sender_type='agent'
    ).order_by('timestamp').values('timestamp')[:1]
    response_times = chats_qs.filter(
        agent__isnull=False
    ).annotate(
        first_agent_at=Subquery(first_agent_msg)
    ).exclude(first_agent_at__isnull=True)

    avg_response_seconds = 0
    rt_count = 0
    for chat in response_times[:100]:
        if chat.first_agent_at and chat.created_at:
            diff = (chat.first_agent_at - chat.created_at).total_seconds()
            if 0 < diff < 86400:
                avg_response_seconds += diff
                rt_count += 1
    if rt_count > 0:
        avg_response_seconds = int(avg_response_seconds / rt_count)
    avg_response_min = avg_response_seconds // 60
    avg_response_sec = avg_response_seconds % 60

    # Chat completion rate
    closed_chats = chats_qs.filter(status='closed').count()
    completion_rate = round((closed_chats / total_chats * 100), 1) if total_chats > 0 else 0

    context = {
        'has_websites': True,
        'total_visitors': total_visitors,
        'online_visitors': online_visitors,
        'period_visitor_count': period_visitor_count,
        'period_pageview_count': period_pageview_count,
        'bounce_rate': bounce_rate,
        'avg_dur_min': avg_dur_min,
        'avg_dur_sec': avg_dur_sec,
        'visitor_change': visitor_change,
        'pageview_change': pageview_change,
        'bounce_change': bounce_change,
        'chat_change': chat_change,
        'range_key': range_key,
        'daily_data': daily_data,
        'total_chats': total_chats,
        'active_chats': active_chats,
        'today_visitors': today_visitors,
        'today_chats': today_chats,
        'today_page_views': today_page_views,
        'unread_offline': unread_offline,
        'waiting_chats': waiting_chats,
        'recent_visitors': recent_visitors,
        'browser_stats': list(browser_stats),
        'device_stats': list(device_stats),
        'os_stats': list(os_stats),
        'referrer_stats': list(referrer_stats),
        'country_stats': list(country_stats),
        'city_stats': list(city_stats),
        'top_pages': list(top_pages),
        'entry_pages': list(entry_pages),
        'exit_pages': list(exit_pages),
        'hourly_data': hourly_data,
        'recent_chats': recent_chats,
        'avg_rating': avg_rating,
        'rating_counts': rating_counts,
        'total_rated': total_rated,
        'avg_response_min': avg_response_min,
        'avg_response_sec': avg_response_sec,
        'completion_rate': completion_rate,
        'closed_chats': closed_chats,
        'sla_cutoff': sla_cutoff,
        'sla_minutes': sla_minutes,
        'org': org,
        'is_owner': is_owner,
        # Agent leaderboard — owners only
        'agent_leaderboard': (
            User.objects.filter(
                agent_profile__organization=org
            ).annotate(
                chats_handled=Count('chat_rooms', filter=Q(chat_rooms__status='closed', chat_rooms__organization=org)),
            ).order_by('-chats_handled')[:5]
            if is_owner else None
        ),
    }
    return render(request, 'dashboard/home.html', context)


@login_required
def chat_list(request):
    org = get_user_org(request.user)
    close_stale_chats()
    now = timezone.now()
    sla_minutes = int(getattr(settings, 'CHAT_SLA_MINUTES', 5))
    sla_cutoff = now - timedelta(minutes=sla_minutes)
    # If the agent didn't ask for a specific tab, default to whichever bucket
    # has rows that need attention: Waiting first, then Active, else All.
    status_filter = request.GET.get('status') or ''
    if not status_filter:
        ws_filter_default = get_website_filter(request, org)
        base_for_default = ChatRoom.objects.filter(organization=org, **ws_filter_default)
        if base_for_default.filter(status='waiting').exists():
            status_filter = 'waiting'
        elif base_for_default.filter(status='active').exists():
            status_filter = 'active'
        else:
            status_filter = 'all'
    search_q = request.GET.get('q', '').strip()
    tag_filter = request.GET.get('tag', '').strip()
    priority_filter = request.GET.get('priority', 'all').strip()
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()
    selected_room_id = request.GET.get('room', '').strip()
    agent_filter = request.GET.get('agent', 'all').strip()
    rating_filter = request.GET.get('rating', 'all').strip()
    unread_only = request.GET.get('unread', '').strip() == '1'
    min_messages = request.GET.get('min_messages', '').strip()
    visitor_name_filter = request.GET.get('visitor_name', '').strip()
    visitor_email_filter = request.GET.get('visitor_email', '').strip()

    from django.db.models import Exists, OuterRef, Subquery
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    ws_filter = get_website_filter(request, org)
    base_chats = ChatRoom.objects.filter(organization=org, **ws_filter)
    chats = base_chats.select_related('visitor', 'agent').annotate(
        unread_count=Count('messages', filter=Q(messages__sender_type='visitor', messages__is_read=False)),
        message_count_db=Count('messages'),
        notes_count=Count('internal_notes'),
        was_transferred=Exists(
            Message.objects.filter(room=OuterRef('pk'), sender_type='system', content__startswith='Chat transferred from')
        ),
    )

    if status_filter != 'all':
        chats = chats.filter(status=status_filter)
    if search_q:
        chats = chats.filter(
            Q(visitor_name__icontains=search_q) |
            Q(visitor_email__icontains=search_q) |
            Q(subject__icontains=search_q) |
            Q(room_id__icontains=search_q) |
            Q(messages__content__icontains=search_q)
        ).distinct()
    if tag_filter:
        chats = chats.filter(tags__icontains=tag_filter)
    if priority_filter in {'low', 'medium', 'high'}:
        chats = chats.filter(priority=priority_filter)
    if date_from:
        chats = chats.filter(created_at__date__gte=date_from)
    if date_to:
        chats = chats.filter(created_at__date__lte=date_to)
    if unread_only:
        chats = chats.filter(unread_count__gt=0)
    if rating_filter == 'good':
        chats = chats.filter(rating__gte=4)
    elif rating_filter == 'bad':
        chats = chats.filter(rating__lte=2)
    elif rating_filter == 'rated':
        chats = chats.filter(rating__isnull=False)
    elif rating_filter == 'unrated':
        chats = chats.filter(rating__isnull=True)
    if visitor_name_filter:
        chats = chats.filter(visitor_name__icontains=visitor_name_filter)
    if visitor_email_filter:
        chats = chats.filter(visitor_email__icontains=visitor_email_filter)
    if agent_filter == 'unassigned':
        chats = chats.filter(agent__isnull=True)
    elif agent_filter.isdigit():
        chats = chats.filter(agent_id=int(agent_filter))
    if min_messages.isdigit():
        chats = chats.filter(message_count_db__gte=int(min_messages))

    chats = chats.order_by('-updated_at')
    chats_all = chats

    # Pagination for heavy history lists
    page_obj = None
    if status_filter == 'closed':
        from django.core.paginator import Paginator
        paginator = Paginator(chats, 50)
        page_obj = paginator.get_page(request.GET.get('page') or 1)
        chats = page_obj.object_list

    # Prefetch participants for collaboration display + mark which chats current user is in
    from tracker.chat.models import ChatParticipant
    from django.db.models import Prefetch
    chats = chats.prefetch_related(
        Prefetch(
            'participants',
            queryset=ChatParticipant.objects.select_related('user', 'user__agent_profile').order_by('joined_at'),
        )
    )
    my_room_ids = set(
        ChatParticipant.objects.filter(user=request.user, room__in=chats).values_list('room__room_id', flat=True)
    )

    selected_chat = None
    if selected_room_id:
        selected_chat = chats_all.filter(room_id=selected_room_id).first()
    if not selected_chat:
        selected_chat = chats.first()

    selected_messages = []
    selected_pageviews = []
    selected_previous_chats = []
    selected_device_timeline = []
    if selected_chat:
        selected_messages = selected_chat.messages.order_by('timestamp')[:300]
        selected_pageviews = selected_chat.visitor.page_views.order_by('-timestamp')[:15]
        selected_previous_chats = selected_chat.visitor.chat_rooms.exclude(pk=selected_chat.pk).order_by('-created_at')[:10]
        selected_device_timeline = selected_chat.visitor.page_views.order_by('-timestamp').values(
            'timestamp', 'url', 'page_title'
        )[:30]

    tab_counts = {
        'all': base_chats.count(),
        'waiting': base_chats.filter(status='waiting').count(),
        'active': base_chats.filter(status='active').count(),
        'closed': base_chats.filter(status='closed').count(),
    }
    agent_options = User.objects.filter(agent_profile__organization=org).order_by('first_name', 'username').distinct()
    query_without_room = request.GET.copy()
    if 'room' in query_without_room:
        del query_without_room['room']
    base_query = query_without_room.urlencode()

    template_name = 'dashboard/chat_history.html' if status_filter == 'closed' else 'dashboard/chat_list.html'
    return render(request, template_name, {
        'chats': chats,
        'my_room_ids': my_room_ids,
        'current_filter': status_filter,
        'search_q': search_q,
        'tag_filter': tag_filter,
        'priority_filter': priority_filter,
        'date_from': date_from,
        'date_to': date_to,
        'agent_filter': agent_filter,
        'rating_filter': rating_filter,
        'unread_only': unread_only,
        'min_messages': min_messages,
        'visitor_name_filter': visitor_name_filter,
        'visitor_email_filter': visitor_email_filter,
        'selected_chat': selected_chat,
        'selected_messages': selected_messages,
        'selected_pageviews': selected_pageviews,
        'selected_previous_chats': selected_previous_chats,
        'selected_device_timeline': selected_device_timeline,
        'tab_counts': tab_counts,
        'agent_options': agent_options,
        'base_query': base_query,
        'page_obj': page_obj,
        'sla_cutoff': sla_cutoff,
        'sla_minutes': sla_minutes,
    })


@login_required
def chat_room_view(request, room_id):
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    # Check agent has access to this website
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner and room.website:
        has_access = AgentWebsiteAccess.objects.filter(agent=profile, website=room.website).exists()
        if not has_access:
            # Check if agent has ANY access rows (if none, legacy agent = allow)
            if AgentWebsiteAccess.objects.filter(agent=profile).exists():
                return HttpResponse("You don't have access to this website's chats.", status=403)
    visitor = room.visitor
    visitor_pages = visitor.page_views.order_by('-timestamp')[:20]
    visitor_notes = visitor.agent_notes.order_by('-created_at')[:10]
    canned_responses = CannedResponse.objects.filter(Q(is_global=True) | Q(created_by=request.user))

    # Multi-agent collaboration tracking + join logic
    from tracker.chat.models import ChatParticipant
    manual_only = org and org.chat_assign_rule == 'manual'
    join_requested = request.GET.get('join') == '1'
    agent_name = request.user.get_full_name() or request.user.username
    channel_layer = get_channel_layer()

    def _broadcast_system(text):
        Message.objects.create(
            room=room, sender_type='system', sender_name='System', content=text,
        )
        async_to_sync(channel_layer.group_send)(
            f'chat_{room.room_id}',
            {
                'type': 'chat_message',
                'message': text,
                'sender_type': 'system',
                'sender_name': 'System',
                'msg_type': 'text',
                'file_url': '',
                'file_name': '',
                'timestamp': timezone.now().isoformat(),
            }
        )

    already_participant = (
        request.user.is_authenticated and
        ChatParticipant.objects.filter(room=room, user=request.user).exists()
    )

    if request.user.is_authenticated and not already_participant:
        # First joiner of a waiting chat → becomes primary
        if room.status == 'waiting' and (not manual_only or join_requested):
            room.agent = request.user
            room.status = 'active'
            room.save()
            ChatParticipant.objects.get_or_create(
                room=room, user=request.user, defaults={'is_primary': True}
            )
            _log_activity(org, request.user, 'agent.joined', f'{agent_name} joined chat #{room.room_id}', 'chat', room.room_id)
            _broadcast_system(f'{agent_name} joined the chat.')
        # Active chat + explicit Join click → collaborator
        elif room.status == 'active' and join_requested:
            # Backfill primary if missing, but avoid duplicate inserts under concurrent requests.
            if room.agent_id:
                ChatParticipant.objects.get_or_create(
                    room=room,
                    user_id=room.agent_id,
                    defaults={'is_primary': True},
                )
            ChatParticipant.objects.get_or_create(
                room=room,
                user=request.user,
                defaults={'is_primary': False},
            )
            _log_activity(org, request.user, 'agent.collab_joined', f'{agent_name} joined chat #{room.room_id} as collaborator', 'chat', room.room_id)
            _broadcast_system(f'{agent_name} joined as collaborator.')

    # Mark visitor messages as read when agent opens the chat.
    updated = Message.objects.filter(room=room, sender_type='visitor', is_read=False).update(is_read=True)
    if updated:
        channel_layer = get_channel_layer()
        dashboard_group = f'dashboard_updates_{org.id}' if org else 'dashboard_updates'
        async_to_sync(channel_layer.group_send)(
            dashboard_group,
            {
                'type': 'dashboard_update',
                'reason': 'messages_read',
                'room_id': room.room_id,
            }
        )
    messages_list = room.messages.all()
    ws_token = create_ws_token(room.room_id, 'agent', request.user.id)
    available_agents = User.objects.filter(
        is_active=True, agent_profile__isnull=False, agent_profile__is_available=True,
        agent_profile__organization=org
    ).exclude(id=request.user.id)
    internal_notes_list = room.internal_notes.select_related('agent').all()

    # AI summary surface flags — used by chat_room.html to show a "Summarizing…"
    # badge while the background thread runs, then the actual summary once it lands.
    ai_summary_pending = False
    if room.status == 'closed' and not room.summary:
        ai_cfg = AIBotConfig.objects.filter(
            organization=org, auto_summarize=True, is_enabled=True, provider='anthropic',
        ).first()
        ai_summary_pending = bool(ai_cfg and ai_cfg.api_key)
    summary_topic_list = [t.strip() for t in (room.summary_topics or '').split(',') if t.strip()]

    return render(request, 'dashboard/chat_room.html', {
        'room': room,
        'messages': messages_list,
        'visitor': visitor,
        'visitor_pages': visitor_pages,
        'visitor_notes': visitor_notes,
        'canned_responses': canned_responses,
        'ws_token': ws_token,
        'available_agents': available_agents,
        'internal_notes': internal_notes_list,
        'sla_minutes': int(getattr(settings, 'CHAT_SLA_MINUTES', 5)),
        'manual_assign': manual_only,
        'ai_summary_pending': ai_summary_pending,
        'summary_topic_list': summary_topic_list,
    })


@login_required
def visitor_list(request):
    org = get_user_org(request.user)
    now = timezone.now()
    last_30_min = now - timedelta(minutes=30)
    filter_type = request.GET.get('filter', 'all')
    search_q = request.GET.get('q', '').strip()
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()
    group_by = request.GET.get('group_by', 'activity').strip().lower()

    group_options = [
        ('activity', 'Activity'),
        ('website', 'Website'),
        ('ip', 'IP Address'),
        ('page_title', 'Page title'),
        ('page_url', 'Page URL'),
        ('country', 'Country'),
        ('serving_agent', 'Serving agent'),
        ('department', 'Department'),
        ('browser', 'Browser'),
        ('search_engine', 'Search engine'),
        ('search_term', 'Search term'),
    ]
    allowed_group_by = {key for key, _ in group_options}
    if group_by not in allowed_group_by:
        group_by = 'activity'

    ws_filter = get_website_filter(request, org)
    latest_pageviews = PageView.objects.filter(visitor_id=OuterRef('pk')).order_by('-timestamp')
    latest_chats = ChatRoom.objects.filter(visitor_id=OuterRef('pk')).order_by('-created_at')

    visitors = Visitor.objects.filter(organization=org, **ws_filter).select_related('website').annotate(
        page_count=Count('page_views'),
        chat_count=Count('chat_rooms'),
        latest_page_title=Subquery(latest_pageviews.values('page_title')[:1]),
        latest_page_url=Subquery(latest_pageviews.values('url')[:1]),
        latest_agent_id=Subquery(latest_chats.values('agent_id')[:1]),
        latest_agent_username=Subquery(latest_chats.values('agent__username')[:1]),
    )

    if filter_type == 'online':
        visitors = visitors.filter(last_seen__gte=last_30_min)
    elif filter_type == 'today':
        # "Today" = anyone *active* today. Using first_visit__gte hid returning
        # visitors who came back today but first arrived on an earlier day,
        # which made the tab look empty even on busy days.
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        visitors = visitors.filter(last_seen__gte=today_start)

    if search_q:
        visitors = visitors.filter(
            Q(ip_address__icontains=search_q) |
            Q(browser__icontains=search_q) |
            Q(os__icontains=search_q) |
            Q(referrer_source__icontains=search_q)
        )
    if date_from:
        visitors = visitors.filter(first_visit__date__gte=date_from)
    if date_to:
        visitors = visitors.filter(first_visit__date__lte=date_to)

    visitors = visitors.order_by('-last_seen')[:200]
    visitor_list_data = list(visitors)

    # Map latest serving agent -> first department name for grouping.
    latest_agent_user_ids = {
        v.latest_agent_id for v in visitor_list_data if getattr(v, 'latest_agent_id', None)
    }
    user_to_profile = dict(
        AgentProfile.objects.filter(user_id__in=latest_agent_user_ids)
        .values_list('user_id', 'id')
    ) if latest_agent_user_ids else {}

    profile_to_department = {}
    profile_ids = list(user_to_profile.values())
    if profile_ids:
        for member in (
            DepartmentMember.objects.filter(agent_id__in=profile_ids)
            .select_related('department')
            .order_by('joined_at')
        ):
            if member.agent_id not in profile_to_department:
                profile_to_department[member.agent_id] = member.department.name

    for v in visitor_list_data:
        if group_by == 'ip':
            v.group_value = v.ip_address or 'Unknown IP'
        elif group_by == 'website':
            v.group_value = v.website.domain if v.website else 'No Website'
        elif group_by == 'activity':
            v.group_value = 'Online' if v.last_seen >= last_30_min else 'Inactive'
        elif group_by == 'page_title':
            v.group_value = (v.latest_page_title or '').strip() or 'Unknown page title'
        elif group_by == 'page_url':
            v.group_value = (v.latest_page_url or '').strip() or 'Unknown page URL'
        elif group_by == 'country':
            v.group_value = (v.country or '').strip() or 'Unknown country'
        elif group_by == 'serving_agent':
            v.group_value = (v.latest_agent_username or '').strip() or 'Unassigned'
        elif group_by == 'department':
            profile_id = user_to_profile.get(v.latest_agent_id)
            v.group_value = profile_to_department.get(profile_id, 'No Department')
        elif group_by == 'browser':
            v.group_value = (v.browser or '').strip() or 'Unknown browser'
        elif group_by == 'search_engine':
            v.group_value = (v.referrer_source or '').strip() or 'Direct'
        elif group_by == 'search_term':
            v.group_value = (v.utm_term or '').strip() or '(none)'
        else:
            v.group_value = 'Other'

    visitor_list_data.sort(
        key=lambda x: ((x.group_value or '').lower(), -x.last_seen.timestamp())
    )

    group_by_label = dict(group_options).get(group_by, 'Activity')

    # Tracking-health diagnostics — gives the empty state real signal instead
    # of always saying "No visitors yet" even when historical data exists or
    # the widget is being actively blocked by configuration.
    org_total_visitors = Visitor.objects.filter(organization=org).count()
    has_other_filter_match = bool(visitor_list_data) and filter_type != 'all'
    free_limit_state = _monthly_visitor_limit_state(org) if org else {'allowed': True, 'limit': None, 'count': 0, 'plan': 'free'}
    visitor_limit_reached = (
        free_limit_state.get('plan') == 'free'
        and free_limit_state.get('count', 0) >= (free_limit_state.get('limit') or 100)
    )
    allowed_domains_blocking = bool(
        org and getattr(org, 'allowed_domains_enabled', False) and (org.allowed_domains or '').strip()
    )

    return render(request, 'dashboard/visitor_list.html', {
        'visitors': visitor_list_data[:100],
        'current_filter': filter_type,
        'last_30_min': last_30_min,
        'search_q': search_q,
        'date_from': date_from,
        'date_to': date_to,
        'group_by': group_by,
        'group_by_label': group_by_label,
        'group_options': group_options,
        # Empty-state context — used by the template to show the right message:
        'org_total_visitors': org_total_visitors,
        'has_any_filter_active': filter_type != 'all' or bool(search_q or date_from or date_to),
        'has_other_filter_match': has_other_filter_match,
        'visitor_limit_reached': visitor_limit_reached,
        'visitor_limit_count': free_limit_state.get('count', 0),
        'visitor_limit_max': free_limit_state.get('limit') or 100,
        'allowed_domains_blocking': allowed_domains_blocking,
    })


@login_required
def visitor_detail(request, visitor_id):
    org = get_user_org(request.user)
    visitor = get_object_or_404(Visitor, id=visitor_id, organization=org)
    # Check agent has access to this visitor's website
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner and visitor.website:
        has_access = AgentWebsiteAccess.objects.filter(agent=profile, website=visitor.website).exists()
        if not has_access and AgentWebsiteAccess.objects.filter(agent=profile).exists():
            return HttpResponse("You don't have access to this website's visitors.", status=403)
    page_views_qs = visitor.page_views.order_by('-timestamp')
    total_page_views = page_views_qs.count()
    page_views = page_views_qs[:50]
    chat_rooms = visitor.chat_rooms.order_by('-created_at')
    notes = visitor.agent_notes.order_by('-created_at')
    events_count = visitor.events.count()
    recordings = visitor.recordings.order_by('-created_at')[:20]
    timeline = _build_visitor_timeline(visitor)
    # Format visit duration
    dur = visitor.session_duration or 0
    if dur >= 3600:
        visit_duration = f"{dur // 3600}h {(dur % 3600) // 60}m"
    elif dur >= 60:
        visit_duration = f"{dur // 60}m {dur % 60}s"
    else:
        visit_duration = f"{dur}s"
    return render(request, 'dashboard/visitor_detail.html', {
        'visitor': visitor,
        'page_views': page_views,
        'total_page_views': total_page_views,
        'chat_rooms': chat_rooms,
        'notes': notes,
        'events_count': events_count,
        'recordings': recordings,
        'visit_duration': visit_duration,
        'timeline': timeline,
    })


def _build_visitor_timeline(visitor, limit_per_kind=80):
    """Merge every visitor-event source into one chronological feed.

    Each item is a dict with: ts (datetime), kind, title, detail, icon, color.
    Keeps query cost bounded by capping each source at `limit_per_kind`.
    Sorted newest first; template renders the first ~120 entries.
    """
    items = []

    # Page views
    for pv in visitor.page_views.order_by('-timestamp')[:limit_per_kind]:
        title = pv.page_title or pv.url[:60]
        items.append({
            'ts': pv.timestamp, 'kind': 'pageview',
            'title': title, 'detail': pv.url,
            'icon': 'fa-link', 'color': '#3b82f6',
            'is_entry': pv.is_entry, 'is_exit': pv.is_exit,
        })

    # Chats
    for cr in visitor.chat_rooms.order_by('-created_at')[:limit_per_kind].select_related('agent'):
        agent_label = cr.agent.get_full_name() if cr.agent else (cr.agent.username if cr.agent else 'Unassigned')
        items.append({
            'ts': cr.created_at, 'kind': 'chat_started',
            'title': f'Chat started — {cr.subject or "No subject"}',
            'detail': f'Agent: {agent_label} · Status: {cr.get_status_display()}',
            'icon': 'fa-comments', 'color': '#7c3aed',
            'room_id': cr.room_id,
        })
        if cr.closed_at:
            extra = []
            if cr.rating:
                extra.append(f'⭐ {cr.rating}/5')
            if cr.duration_display:
                extra.append(cr.duration_display)
            items.append({
                'ts': cr.closed_at, 'kind': 'chat_closed',
                'title': 'Chat closed',
                'detail': ' · '.join(extra) or f'Agent: {agent_label}',
                'icon': 'fa-check-circle', 'color': '#10b981',
                'room_id': cr.room_id,
            })

    # Agent notes
    for n in visitor.agent_notes.order_by('-created_at')[:limit_per_kind].select_related('agent'):
        items.append({
            'ts': n.created_at, 'kind': 'note',
            'title': f'Note by {n.agent.get_full_name() or n.agent.username}',
            'detail': (n.content or '')[:200],
            'icon': 'fa-sticky-note', 'color': '#f59e0b',
        })

    # Survey responses
    try:
        for resp in visitor.survey_responses.order_by('-created_at')[:limit_per_kind].select_related('survey'):
            items.append({
                'ts': resp.created_at, 'kind': 'survey',
                'title': f'Submitted survey: {resp.survey.title}',
                'detail': f'Score: {resp.score}' if resp.score is not None else '',
                'icon': 'fa-clipboard-check', 'color': '#06b6d4',
            })
    except Exception:
        pass

    # Recordings
    try:
        for rec in visitor.recordings.order_by('-created_at')[:limit_per_kind]:
            items.append({
                'ts': rec.created_at, 'kind': 'recording',
                'title': 'Session recording captured',
                'detail': f'{getattr(rec, "duration_seconds", 0)}s' if hasattr(rec, 'duration_seconds') else '',
                'icon': 'fa-video', 'color': '#ec4899',
            })
    except Exception:
        pass

    # Ban / unban (one terminal event if currently banned)
    if visitor.is_banned:
        items.append({
            'ts': visitor.updated_at if hasattr(visitor, 'updated_at') and visitor.updated_at else visitor.last_seen,
            'kind': 'banned',
            'title': 'Visitor banned',
            'detail': 'Cannot start new chats',
            'icon': 'fa-ban', 'color': '#ef4444',
        })

    # First-ever visit anchor
    if visitor.first_visit:
        utm_bits = []
        if visitor.referrer_source:
            utm_bits.append(f'via {visitor.referrer_source}')
        if getattr(visitor, 'utm_campaign', ''):
            utm_bits.append(f'utm: {visitor.utm_campaign}')
        items.append({
            'ts': visitor.first_visit, 'kind': 'first_visit',
            'title': 'First visit',
            'detail': ' · '.join(utm_bits) or 'Direct traffic',
            'icon': 'fa-flag-checkered', 'color': '#0ea5e9',
        })

    items.sort(key=lambda x: x['ts'], reverse=True)
    return items[:120]


@login_required
def api_stats(request):
    org = get_user_org(request.user)
    from django.core.cache import cache
    from tracker.core import process_throttle
    # Throttle stale chat cleanup + SLA check (in-process gates — dashboards
    # poll this every 10s, N tabs × M workers used to push N×M Redis ops per
    # poll just to decide whether to skip the work).
    if process_throttle.should_run(f'stale_api:{org.id if org else 0}', 30):
        close_stale_chats()
    if org and process_throttle.should_run(f'sla_api:{org.id}', 30):
        check_sla_breaches(
            sla_minutes=int(getattr(settings, 'CHAT_SLA_MINUTES', 5)),
            org_id=org.id,
        )

    # Short-lived cache: with N dashboard tabs polling every 10s, this drops the
    # 7 count() queries per poll to one per ~5s window per (org, website filter).
    # Cache key must reflect the *exact* filter or two agents with different
    # website-access lists would share each other's counts.
    ws_filter = get_website_filter(request, org)
    if 'website_id' in ws_filter:
        ws_cache_key = f'w{ws_filter["website_id"]}'
    elif 'website_id__in' in ws_filter:
        ws_cache_key = 'wi' + ','.join(str(i) for i in sorted(ws_filter['website_id__in']))
    else:
        ws_cache_key = 'all'
    cache_key = f'api_stats:{org.id if org else 0}:{ws_cache_key}'
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)

    now = timezone.now()
    last_30_min = now - timedelta(minutes=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    payload = {
        'online_visitors': Visitor.objects.filter(organization=org, last_seen__gte=last_30_min, **ws_filter).count(),
        'active_chats': ChatRoom.objects.filter(organization=org, status__in=['waiting', 'active'], **ws_filter).count(),
        'active_only_chats': ChatRoom.objects.filter(organization=org, status='active', **ws_filter).count(),
        'waiting_chats': ChatRoom.objects.filter(organization=org, status='waiting', **ws_filter).count(),
        'unread_messages': Message.objects.filter(
            room__organization=org,
            room__status__in=['waiting', 'active'],
            sender_type='visitor',
            is_read=False,
        ).count(),
        'today_visitors': Visitor.objects.filter(organization=org, first_visit__gte=today_start, **ws_filter).count(),
        'today_page_views': PageView.objects.filter(visitor__organization=org, timestamp__gte=today_start).count(),
    }
    cache.set(cache_key, payload, 5)
    return JsonResponse(payload)


@login_required
def close_chat(request, room_id):
    org = get_user_org(request.user)
    if request.method == 'POST':
        room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
        room.status = 'closed'
        room.closed_at = timezone.now()
        room.save()
        # Fire webhook + log activity
        fire_webhook(org, 'chat.closed', {
            'event': 'chat.closed', 'room_id': room_id,
            'visitor_name': room.visitor_name, 'duration': str(room.duration),
        })
        _log_activity(org, request.user, 'chat.closed', f'Closed chat #{room_id} with {room.visitor_name}', 'chat', room_id)

        # AI auto-summary — async via thread so the agent UI doesn't block on
        # the LLM call. Org pays for their own usage via api_key on AIBotConfig.
        ai_cfg = AIBotConfig.objects.filter(organization=org, auto_summarize=True, is_enabled=True).first()
        if ai_cfg and ai_cfg.provider == 'anthropic' and ai_cfg.api_key:
            def _summarize(rid, cfg_id):
                from django.db import close_old_connections
                from tracker.chat.ai import summarize_chat
                from tracker.chat.models import ChatRoom as _CR, AIBotConfig as _AC
                try:
                    cfg = _AC.objects.get(id=cfg_id)
                    r = _CR.objects.get(id=rid)
                    out = summarize_chat(cfg, r)
                    if out and out.get('summary'):
                        _CR.objects.filter(id=rid).update(
                            summary=out['summary'],
                            summary_topics=out.get('topics', ''),
                            summary_at=timezone.now(),
                        )
                except Exception:
                    logger.warning('auto-summary thread failed', exc_info=True)
                finally:
                    close_old_connections()
            try:
                _AI_POOL.submit(_summarize, room.id, ai_cfg.id)
            except RuntimeError:
                pass

        # Push survey prompt to the visitor side via WebSocket so the widget
        # can render the post-chat survey modal without polling.
        survey = Survey.objects.filter(
            organization=org, is_active=True, show_after_chat=True,
        ).order_by('-created_at').first()
        if survey:
            try:
                from asgiref.sync import async_to_sync
                from channels.layers import get_channel_layer
                async_to_sync(get_channel_layer().group_send)(
                    f'chat_{room_id}',
                    {
                        'type': 'survey_prompt',
                        'survey_id': survey.id,
                        'survey_title': survey.title,
                    },
                )
            except Exception:
                logger.warning('failed to push survey prompt for room=%s', room_id, exc_info=True)
        return JsonResponse({
            'status': 'closed',
            'survey_id': survey.id if survey else None,
        })
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def transfer_chat(request, room_id):
    """Transfer chat to another available agent."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    data = _parse_json_body(request) or {}
    target_id = data.get('agent_id')
    if not target_id:
        return JsonResponse({'error': 'agent_id required'}, status=400)

    target_agent = User.objects.filter(
        id=target_id,
        is_active=True,
        agent_profile__isnull=False,
        agent_profile__is_available=True,
        agent_profile__organization=org,
    ).first()
    if not target_agent:
        return JsonResponse({'error': 'Agent not available'}, status=400)

    from_agent_name = request.user.get_full_name() or request.user.username
    to_agent_name = target_agent.get_full_name() or target_agent.username

    room.agent = target_agent
    room.status = 'active'
    room.save(update_fields=['agent', 'status', 'updated_at'])

    # Save system message in chat about the transfer
    transfer_msg = f'Chat transferred from {from_agent_name} to {to_agent_name}'
    Message.objects.create(
        room=room,
        sender_type='system',
        sender_name='System',
        content=transfer_msg,
        msg_type='text',
    )

    # Send real-time WebSocket notification to the chat room
    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'chat_{room_id}',
        {
            'type': 'chat_transferred',
            'message': transfer_msg,
            'from_agent': from_agent_name,
            'to_agent': to_agent_name,
            'to_agent_id': target_agent.id,
        }
    )

    # Notify dashboard to refresh badges/lists
    dashboard_group = f'dashboard_updates_{org.id}' if org else 'dashboard_updates'
    async_to_sync(channel_layer.group_send)(
        dashboard_group,
        {
            'type': 'dashboard_update',
            'reason': 'chat_transferred',
            'room_id': room_id,
        }
    )

    return JsonResponse({
        'status': 'ok',
        'agent_id': target_agent.id,
        'agent_name': to_agent_name,
    })


# ===== INTERNAL NOTES (AGENT COLLABORATION) =====

@login_required
def internal_notes(request, room_id):
    """Get or add internal notes for a chat room (agent-only, not visible to visitors)."""
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)

    if request.method == 'GET':
        notes = room.internal_notes.select_related('agent').all()
        return JsonResponse({
            'notes': [
                {
                    'id': n.id,
                    'agent_name': n.agent.get_full_name() or n.agent.username,
                    'agent_id': n.agent.id,
                    'content': n.content,
                    'created_at': n.created_at.isoformat(),
                }
                for n in notes
            ]
        })

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        content = data.get('content', '').strip()
        if not content:
            return JsonResponse({'error': 'Content required'}, status=400)

        note = InternalNote.objects.create(
            room=room,
            agent=request.user,
            content=content,
        )
        agent_name = request.user.get_full_name() or request.user.username

        # Notify other agents viewing this chat in real-time
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'chat_{room_id}',
            {
                'type': 'internal_note',
                'note_id': note.id,
                'agent_name': agent_name,
                'agent_id': request.user.id,
                'content': content,
                'created_at': note.created_at.isoformat(),
            }
        )

        return JsonResponse({
            'status': 'ok',
            'note_id': note.id,
            'agent_name': agent_name,
            'content': content,
            'created_at': note.created_at.isoformat(),
        })

    return JsonResponse({'error': 'GET or POST required'}, status=405)


# ===== NEW FEATURES =====

@login_required
def add_visitor_note(request, visitor_id):
    """Add a note about a visitor."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        visitor = get_object_or_404(Visitor, id=visitor_id, organization=org)
        data = _parse_json_body(request) or {}
        note = VisitorNote.objects.create(
            visitor=visitor,
            agent=request.user,
            content=data.get('content', ''),
        )
        return JsonResponse({
            'status': 'ok',
            'note_id': note.id,
            'content': note.content,
            'agent': request.user.get_full_name() or request.user.username,
            'created_at': note.created_at.strftime('%b %d, %H:%M'),
        })
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def update_chat_tags(request, room_id):
    """Update tags on a chat."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
        data = _parse_json_body(request) or {}
        room.tags = data.get('tags', '')
        room.save(update_fields=['tags'])
        return JsonResponse({'status': 'ok', 'tags': room.tags})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def update_chat_priority(request, room_id):
    """Update priority on a chat."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
        data = _parse_json_body(request) or {}
        room.priority = data.get('priority', 'medium')
        room.save(update_fields=['priority'])
        return JsonResponse({'status': 'ok', 'priority': room.priority})
    return JsonResponse({'error': 'POST required'}, status=405)


@csrf_exempt
def rate_chat(request, room_id):
    """Visitor rates a chat."""
    if request.method == 'POST':
        room = get_object_or_404(ChatRoom, room_id=room_id)
        data = _parse_json_body(request) or {}
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


@login_required
def export_visitors_csv(request):
    """Export all visitors as CSV (streamed)."""
    org = get_user_org(request.user)
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()

    ws_filter = get_website_filter(request, org)
    visitors_qs = Visitor.objects.filter(organization=org, **ws_filter)
    ids_param = request.GET.get('ids', '').strip()
    if ids_param:
        try:
            id_list = [int(x) for x in ids_param.split(',') if x.strip().isdigit()]
            if id_list:
                visitors_qs = visitors_qs.filter(id__in=id_list)
        except ValueError:
            pass
    if date_from:
        visitors_qs = visitors_qs.filter(first_visit__date__gte=date_from)
    if date_to:
        visitors_qs = visitors_qs.filter(first_visit__date__lte=date_to)

    header = ['ID', 'IP Address', 'Browser', 'OS', 'Device', 'Source',
              'First Visit', 'Last Seen', 'Total Visits', 'Online']

    def rows():
        yield header
        # iterator(chunk_size=...) keeps DB cursor + Python heap bounded
        for v in visitors_qs.iterator(chunk_size=2000):
            yield [
                v.id, v.ip_address, v.browser, v.os, v.device_type,
                v.referrer_source, v.first_visit.strftime('%Y-%m-%d %H:%M'),
                v.last_seen.strftime('%Y-%m-%d %H:%M'), v.total_visits, v.is_online,
            ]

    return _stream_csv(rows(), 'visitors_export.csv')


@login_required
def export_chats_csv(request):
    """Export currently filtered chats as CSV/PDF."""
    org = get_user_org(request.user)
    status_filter = request.GET.get('status', 'all')
    search_q = request.GET.get('q', '').strip()
    tag_filter = request.GET.get('tag', '').strip()
    priority_filter = request.GET.get('priority', 'all').strip()
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()
    agent_filter = request.GET.get('agent', 'all').strip()
    rating_filter = request.GET.get('rating', 'all').strip()
    unread_only = request.GET.get('unread', '').strip() == '1'
    min_messages = request.GET.get('min_messages', '').strip()
    visitor_name_filter = request.GET.get('visitor_name', '').strip()
    visitor_email_filter = request.GET.get('visitor_email', '').strip()
    export_format = (request.GET.get('format') or 'csv').strip().lower()

    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    ws_filter = get_website_filter(request, org)
    chats_qs = ChatRoom.objects.filter(organization=org, **ws_filter).select_related('agent').annotate(
        unread_count=Count('messages', filter=Q(messages__sender_type='visitor', messages__is_read=False)),
        message_count_db=Count('messages'),
    )
    if not is_owner:
        chats_qs = chats_qs.filter(Q(agent=request.user) | Q(agent__isnull=True))

    if status_filter != 'all':
        chats_qs = chats_qs.filter(status=status_filter)
    if search_q:
        chats_qs = chats_qs.filter(
            Q(visitor_name__icontains=search_q) |
            Q(visitor_email__icontains=search_q) |
            Q(subject__icontains=search_q) |
            Q(room_id__icontains=search_q) |
            Q(messages__content__icontains=search_q)
        ).distinct()
    if tag_filter:
        chats_qs = chats_qs.filter(tags__icontains=tag_filter)
    if priority_filter in {'low', 'medium', 'high'}:
        chats_qs = chats_qs.filter(priority=priority_filter)
    if date_from:
        chats_qs = chats_qs.filter(created_at__date__gte=date_from)
    if date_to:
        chats_qs = chats_qs.filter(created_at__date__lte=date_to)
    if unread_only:
        chats_qs = chats_qs.filter(unread_count__gt=0)
    if rating_filter == 'good':
        chats_qs = chats_qs.filter(rating__gte=4)
    elif rating_filter == 'bad':
        chats_qs = chats_qs.filter(rating__lte=2)
    elif rating_filter == 'rated':
        chats_qs = chats_qs.filter(rating__isnull=False)
    elif rating_filter == 'unrated':
        chats_qs = chats_qs.filter(rating__isnull=True)
    if visitor_name_filter:
        chats_qs = chats_qs.filter(visitor_name__icontains=visitor_name_filter)
    if visitor_email_filter:
        chats_qs = chats_qs.filter(visitor_email__icontains=visitor_email_filter)
    if agent_filter == 'unassigned':
        chats_qs = chats_qs.filter(agent__isnull=True)
    elif agent_filter.isdigit():
        chats_qs = chats_qs.filter(agent_id=int(agent_filter))
    if min_messages.isdigit():
        chats_qs = chats_qs.filter(message_count_db__gte=int(min_messages))
    chats_qs = chats_qs.order_by('-updated_at')

    if export_format == 'pdf':
        from io import BytesIO
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=20, rightMargin=20, topMargin=20, bottomMargin=20)
        styles = getSampleStyleSheet()
        elements = [
            Paragraph("Chat History Export", styles['Heading2']),
            Paragraph(f"Organization: {org.name if org else '-'}", styles['Normal']),
            Paragraph(f"Rows: {chats_qs.count()}", styles['Normal']),
            Spacer(1, 10),
        ]
        rows = [['Room', 'Visitor', 'Agent', 'Status', 'Priority', 'Msgs', 'Created', 'Closed']]
        for c in chats_qs[:1000]:
            rows.append([
                c.room_id,
                (c.visitor_name or '')[:24],
                (c.agent.get_full_name() if c.agent else '-')[:20],
                c.status,
                c.priority,
                str(c.message_count_db),
                c.created_at.strftime('%Y-%m-%d %H:%M'),
                c.closed_at.strftime('%Y-%m-%d %H:%M') if c.closed_at else '-',
            ])
        table = Table(rows, repeatRows=1)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#ede9fe')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#1e1b4b')),
            ('GRID', (0, 0), (-1, -1), 0.3, colors.HexColor('#d4d4d8')),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        elements.append(table)
        doc.build(elements)
        pdf = buffer.getvalue()
        buffer.close()
        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = 'attachment; filename="chats_export.pdf"'
        return response

    header = ['Room ID', 'Visitor', 'Email', 'Agent', 'Status', 'Priority',
              'Subject', 'Rating', 'Tags', 'Messages', 'Unread', 'Created', 'Closed']

    def rows():
        yield header
        for c in chats_qs.iterator(chunk_size=1000):
            yield [
                c.room_id, c.visitor_name, c.visitor_email,
                c.agent.get_full_name() if c.agent else '-',
                c.status, c.priority, c.subject, c.rating or '-', c.tags,
                c.message_count_db, c.unread_count, c.created_at.strftime('%Y-%m-%d %H:%M'),
                c.closed_at.strftime('%Y-%m-%d %H:%M') if c.closed_at else '-',
            ]

    return _stream_csv(rows(), 'chats_export.csv')


@login_required
def offline_messages_view(request):
    """View offline messages. Owner only."""
    profile = getattr(request.user, 'agent_profile', None)
    if not request.user.is_superuser and (not profile or profile.role not in ('owner', 'admin')):
        return HttpResponse("Forbidden — owners only.", status=403)
    org = get_user_org(request.user)
    ws_filter = get_website_filter(request, org)
    messages_list = OfflineMessage.objects.filter(organization=org, **ws_filter)
    return render(request, 'dashboard/offline_messages.html', {
        'messages': messages_list,
    })


@login_required
def mark_offline_read(request, msg_id):
    """Mark offline message as read."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        msg = get_object_or_404(OfflineMessage, id=msg_id, organization=org)
        msg.is_read = True
        msg.save()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def ban_visitor(request, visitor_id):
    """Ban or unban a visitor from starting new chats."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    visitor = get_object_or_404(Visitor, id=visitor_id, organization=org)
    data = _parse_json_body(request) or {}
    action = (data.get('action') or 'ban').strip().lower()
    visitor.is_banned = action == 'ban'
    visitor.save(update_fields=['is_banned'])
    return JsonResponse({'status': 'ok', 'is_banned': visitor.is_banned})


@login_required
def agent_stats(request):
    """Agent performance stats page."""
    org = get_user_org(request.user)
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_7_days = now - timedelta(days=7)
    last_30_days = now - timedelta(days=30)

    agents = User.objects.filter(agent_profile__isnull=False, agent_profile__organization=org).annotate(
        total_chats=Count('chat_rooms'),
        active_chats=Count('chat_rooms', filter=Q(chat_rooms__status='active')),
        closed_chats=Count('chat_rooms', filter=Q(chat_rooms__status='closed')),
        today_chats=Count('chat_rooms', filter=Q(chat_rooms__created_at__gte=today_start)),
        week_chats=Count('chat_rooms', filter=Q(chat_rooms__created_at__gte=last_7_days)),
        avg_rating=Avg('chat_rooms__rating', filter=Q(chat_rooms__rating__isnull=False)),
        total_messages=Count('chat_rooms__messages', filter=Q(chat_rooms__messages__sender_type='agent')),
    )

    # Overall stats — collapse 4 sequential queries into one aggregate.
    chats_qs = ChatRoom.objects.filter(organization=org)
    overall = chats_qs.aggregate(
        total=Count('id'),
        avg_rating=Avg('rating', filter=Q(rating__isnull=False)),
        total_closed=Count('id', filter=Q(status='closed')),
        today_total=Count('id', filter=Q(created_at__gte=today_start)),
    )
    total_chats = overall['total'] or 0
    avg_rating = overall['avg_rating']
    total_closed = overall['total_closed'] or 0
    today_total = overall['today_total'] or 0

    # Chats per day (last 7 days) — group at the DB instead of looping 7 counts.
    week_start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
    by_date = {
        row['d']: row['c']
        for row in (
            chats_qs.filter(created_at__gte=week_start)
            .annotate(d=TruncDate('created_at'))
            .values('d')
            .annotate(c=Count('id'))
        )
    }
    daily_chats = []
    for i in range(7):
        day_start = (now - timedelta(days=6 - i)).replace(hour=0, minute=0, second=0, microsecond=0)
        daily_chats.append({
            'day': day_start.strftime('%a'),
            'count': by_date.get(day_start.date(), 0),
        })

    # Rating distribution — single GROUP BY rating instead of 5 counts.
    rating_rows = (
        chats_qs.filter(rating__isnull=False, rating__gte=1, rating__lte=5)
        .values('rating')
        .annotate(c=Count('id'))
    )
    by_rating = {row['rating']: row['c'] for row in rating_rows}
    rating_dist = [{'rating': r, 'count': by_rating.get(r, 0)} for r in range(1, 6)]

    return render(request, 'dashboard/agent_stats.html', {
        'agents': agents,
        'total_chats': total_chats,
        'avg_rating': avg_rating,
        'total_closed': total_closed,
        'today_total': today_total,
        'daily_chats': daily_chats,
        'rating_dist': rating_dist,
    })


@login_required
@requires_feature('advanced_analytics', plan_label='Pro')
def agent_performance_view(request):
    """Premium per-agent performance dashboard.

    Adds depth that the free `agent_stats` view doesn't:
      - First-response time per agent (avg seconds)
      - Average resolution duration
      - SLA breaches per agent
      - Online/availability status
      - 30-day trend per agent (chats handled, CSAT)
    """
    org = get_user_org(request.user)
    now = timezone.now()
    last_30 = now - timedelta(days=30)

    # Single annotated query over ChatRoom — aggregate everything per agent in
    # the database. Previous version ran 5+ queries per agent in a Python loop,
    # which became O(agents²) when annotations also pulled chat counts.
    from django.db.models import (
        Avg, Count, ExpressionWrapper, F, IntegerField, Q, Subquery,
    )
    first_agent_msg = (
        Message.objects
        .filter(room=OuterRef('pk'), sender_type='agent')
        .order_by('timestamp')
        .values('timestamp')[:1]
    )
    # Per-chat duration in seconds (closed) and first-response delta in seconds.
    chats_30 = (
        ChatRoom.objects
        .filter(organization=org, agent__isnull=False, created_at__gte=last_30)
        .annotate(_first_resp_at=Subquery(first_agent_msg))
    )
    # Aggregate per agent in one go. We use AVG over the per-chat extracts.
    # Postgres EPOCH gives float seconds for an interval — that's what we want.
    from django.db.models.functions import Extract
    per_agent_qs = (
        chats_30
        .values('agent_id')
        .annotate(
            total_chats_30d=Count('id'),
            closed_30d=Count('id', filter=Q(status='closed')),
            avg_first_response_seconds=Avg(
                Extract(F('_first_resp_at') - F('created_at'), 'epoch'),
                filter=Q(_first_resp_at__isnull=False),
            ),
            avg_duration_seconds=Avg(
                Extract(F('closed_at') - F('created_at'), 'epoch'),
                filter=Q(closed_at__isnull=False),
            ),
            avg_rating=Avg('rating', filter=Q(rating__isnull=False)),
        )
    )
    by_agent = {row['agent_id']: row for row in per_agent_qs}

    # SLA breaches per agent (single query).
    breach_counts = dict(
        SLABreach.objects
        .filter(organization=org, chat__agent__isnull=False, breached_at__gte=last_30)
        .values_list('chat__agent_id')
        .annotate(c=Count('id'))
        .values_list('chat__agent_id', 'c')
    )

    agents_qs = (
        AgentProfile.objects
        .filter(organization=org)
        .select_related('user')
    )

    rows = []
    for profile in agents_qs:
        user = profile.user
        agg = by_agent.get(user.id, {})
        total_chats = agg.get('total_chats_30d', 0) or 0
        closed = agg.get('closed_30d', 0) or 0
        resolution_rate = (closed / total_chats * 100) if total_chats else 0
        rows.append({
            'agent': user,
            'profile': profile,
            'total_chats_30d': total_chats,
            'closed_30d': closed,
            'avg_first_response_seconds': agg.get('avg_first_response_seconds'),
            'avg_duration_seconds': agg.get('avg_duration_seconds'),
            'avg_rating': agg.get('avg_rating'),
            'sla_breach_count': breach_counts.get(user.id, 0),
            'resolution_rate': round(resolution_rate, 1),
            'is_online': profile.is_available,
            'active_chats': profile.active_chats_count,
        })

    rows.sort(key=lambda r: (r['total_chats_30d'] or 0), reverse=True)

    return render(request, 'dashboard/agent_performance.html', {
        'rows': rows,
        'period_label': 'Last 30 days',
    })


@csrf_exempt
def kb_suggest_api(request):
    """Public endpoint: given a query (visitor message), return matching KB articles.

    Used by the widget's auto-suggest panel to deflect repetitive questions
    without an agent reply. Scoped to the org behind the tracking_key.

    Rate-limited per (IP, tracking_key) to prevent KB scraping by bots.
    """
    from django.core.cache import cache
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    query = (data.get('query') or '').strip()
    tracking_key = (data.get('tracking_key') or '').strip()
    if not query or len(query) < 3:
        return JsonResponse({'results': []})
    if not tracking_key:
        return JsonResponse({'error': 'tracking_key required'}, status=400)

    # Cheap per-IP throttle: 30 requests / minute should comfortably cover
    # a real visitor typing into the widget but stop scraper bots cold.
    ip = request.META.get('HTTP_X_FORWARDED_FOR', '').split(',')[0].strip() or request.META.get('REMOTE_ADDR', 'unknown')
    rl_key = f'kb_suggest_rl:{ip}:{tracking_key[:32]}'
    count = cache.get(rl_key, 0)
    if count >= 30:
        return JsonResponse({'error': 'Too many requests', 'results': []}, status=429)
    cache.set(rl_key, count + 1, timeout=60)

    website = Website.objects.filter(tracking_key=tracking_key, is_active=True).select_related('organization').first()
    if not website:
        return JsonResponse({'error': 'invalid tracking_key'}, status=404)
    org = website.organization

    # Simple title/content match — good enough for v1, can swap for full-text
    # search later. Public articles only, capped to 5 results.
    matches = (
        KBArticle.objects
        .filter(organization=org, is_published=True)
        .filter(Q(title__icontains=query) | Q(content__icontains=query))
        .select_related('category')
        .order_by('-views_count', '-helpful_yes')[:5]
    )
    results = [{
        'id': a.id,
        'title': a.title,
        'snippet': a.content[:160],
        'category': a.category.name if a.category else '',
        'helpful_yes': a.helpful_yes,
        'helpful_no': a.helpful_no,
    } for a in matches]
    return JsonResponse({'results': results})


@csrf_exempt
def widget_survey_detail(request, survey_id):
    """Public endpoint: fetch a survey definition for the widget to render.

    Used after `survey_prompt` is pushed over WebSocket — the widget calls
    this to load the questions before rendering the modal.
    """
    survey = Survey.objects.filter(id=survey_id, is_active=True).prefetch_related('questions').first()
    if not survey:
        return JsonResponse({'error': 'survey not found'}, status=404)
    return JsonResponse({
        'id': survey.id,
        'title': survey.title,
        'description': survey.description,
        'type': survey.survey_type,
        'questions': [
            {
                'id': q.id,
                'text': q.question_text,
                'type': q.question_type,
                'choices': q.choices_list,
                'required': q.is_required,
            }
            for q in survey.questions.all().order_by('order')
        ],
    })


@login_required
def canned_responses_view(request):
    """Manage canned responses."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            CannedResponse.objects.create(
                title=data.get('title', ''),
                message=data.get('message', ''),
                shortcut=data.get('shortcut', ''),
                created_by=request.user,
                organization=org,
            )
            return JsonResponse({'status': 'created'})

        elif action == 'delete':
            CannedResponse.objects.filter(id=data.get('id'), created_by=request.user, organization=org).delete()
            return JsonResponse({'status': 'deleted'})

    responses = CannedResponse.objects.filter(organization=org).filter(Q(is_global=True) | Q(created_by=request.user))
    return render(request, 'dashboard/canned_responses.html', {
        'responses': responses,
    })


@login_required
def website_settings_view(request):
    """Create or update website/widget settings from dashboard. Owner only."""
    profile = getattr(request.user, 'agent_profile', None)
    if not request.user.is_superuser and (not profile or profile.role not in ('owner', 'admin')):
        return HttpResponse("Forbidden — owners only.", status=403)
    org = get_user_org(request.user)
    saved = False
    error = ''

    if request.method == 'POST':
        old_state = {
            'blocked_countries_enabled': org.blocked_countries_enabled,
            'blocked_countries': org.blocked_countries,
            'allowed_domains_enabled': org.allowed_domains_enabled,
            'allowed_domains': org.allowed_domains,
            'attack_mode_enabled': getattr(org, 'attack_mode_enabled', False),
            'attack_mode_message': getattr(org, 'attack_mode_message', ''),
            'chat_widget_hidden': getattr(org, 'chat_widget_hidden', False),
        }
        site_name = request.POST.get('site_name', '').strip()
        welcome_message = request.POST.get('welcome_message', '').strip()
        offline_message = request.POST.get('offline_message', '').strip()
        widget_color = request.POST.get('chat_widget_color', '').strip() or '#6366f1'
        auto_reply_enabled = request.POST.get('auto_reply_enabled') == 'on'
        auto_reply_message = request.POST.get('auto_reply_message', '').strip()
        require_email = request.POST.get('require_email') == 'on'
        widget_title = request.POST.get('widget_title', '').strip()
        widget_position = request.POST.get('widget_position', '').strip()
        chat_widget_hidden = request.POST.get('chat_widget_hidden') == 'on'

        if not site_name:
            error = 'Site name is required.'
        else:
            org.name = site_name
            org.widget_title = widget_title or org.widget_title
            org.widget_color = widget_color
            org.widget_position = widget_position or org.widget_position
            org.chat_widget_hidden = chat_widget_hidden
            org.welcome_message = welcome_message or 'Hi! How can we help you today?'
            org.offline_message = offline_message or 'We are currently offline. Please leave a message.'
            org.auto_reply_enabled = auto_reply_enabled
            org.auto_reply_message = auto_reply_message or 'Thanks for reaching out! An agent will be with you shortly.'
            org.require_email = require_email
            # Notifications
            org.notify_email = request.POST.get('notify_email', '').strip()
            org.notify_on_new_chat = request.POST.get('notify_on_new_chat') == 'on'
            # Business hours
            org.business_hours_enabled = request.POST.get('business_hours_enabled') == 'on'
            bh_start = request.POST.get('business_hours_start', '').strip()
            bh_end = request.POST.get('business_hours_end', '').strip()
            if bh_start:
                org.business_hours_start = bh_start
            if bh_end:
                org.business_hours_end = bh_end
            # Proactive chat
            org.proactive_enabled = request.POST.get('proactive_enabled') == 'on'
            try:
                org.proactive_delay = int(request.POST.get('proactive_delay', 30))
            except (ValueError, TypeError):
                org.proactive_delay = 30
            org.proactive_message = request.POST.get('proactive_message', '').strip() or 'Need help? Chat with us!'
            # Auto-responder
            org.auto_responder_enabled = request.POST.get('auto_responder_enabled') == 'on'
            try:
                org.auto_responder_delay = int(request.POST.get('auto_responder_delay', 2))
            except (ValueError, TypeError):
                org.auto_responder_delay = 2
            org.auto_responder_message = request.POST.get('auto_responder_message', '').strip() or 'Thanks for waiting!'
            # Assignment rule
            org.chat_assign_rule = request.POST.get('chat_assign_rule', 'least_busy')
            # Access control
            org.blocked_countries_enabled = request.POST.get('blocked_countries_enabled') == 'on'
            blocked_countries = request.POST.get('blocked_countries', '')
            org.blocked_countries = '\n'.join([x.strip() for x in blocked_countries.replace(',', '\n').splitlines() if x.strip()])
            org.allowed_domains_enabled = request.POST.get('allowed_domains_enabled') == 'on'
            allowed_domains = request.POST.get('allowed_domains', '')
            org.allowed_domains = '\n'.join([x.strip().lower() for x in allowed_domains.replace(',', '\n').splitlines() if x.strip()])
            org.attack_mode_enabled = request.POST.get('attack_mode_enabled') == 'on'
            org.attack_mode_message = (
                request.POST.get('attack_mode_message', '').strip()
                or 'High traffic detected. Please try again in a minute.'
            )
            org.save()
            changed = []
            for key, old_val in old_state.items():
                new_val = getattr(org, key, None)
                if (old_val or '') != (new_val or ''):
                    changed.append(f"{key}: '{old_val}' -> '{new_val}'")
            if changed:
                _log_activity(
                    org, request.user, 'settings.updated',
                    'Website settings updated. ' + '; '.join(changed[:8]),
                    target_type='organization', target_id=str(org.id),
                )
            saved = True

    script_url = request.build_absolute_uri('/api/widget/script.js')
    embed_code = f'<script src="{script_url}?key={org.widget_key}" defer></script>'

    return render(request, 'dashboard/website_settings.html', {
        'settings_obj': org,
        'org': org,
        'saved': saved,
        'error': error,
        'embed_code': embed_code,
        'widget_key': org.widget_key,
        'position_choices': [('bottom-right', 'Bottom Right'), ('bottom-left', 'Bottom Left')],
    })


@login_required
def add_agent_view(request):
    """Add new support agent from dashboard. Owner only."""
    org = get_user_org(request.user)
    if not org:
        return HttpResponse("No organization found", status=403)
    profile = getattr(request.user, 'agent_profile', None)
    if not request.user.is_superuser and (not profile or profile.role not in ('owner', 'admin')):
        return HttpResponse("Forbidden — owners only.", status=403)
    created = False
    error = ''

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '').strip()
        first_name = request.POST.get('first_name', '').strip()
        last_name = request.POST.get('last_name', '').strip()
        email = request.POST.get('email', '').strip()
        try:
            max_chats = int(request.POST.get('max_chats', 5) or 5)
        except (TypeError, ValueError):
            max_chats = 5
        is_available = request.POST.get('is_available') == 'on'

        # Check plan limit for agents
        from tracker.core.views import get_plan_limits
        limits = get_plan_limits(org)
        current_agents = AgentProfile.objects.filter(organization=org).count()

        if not username or not password:
            error = 'Username and password are required.'
        elif current_agents >= limits.get('max_agents', 1):
            error = f'Your {org.subscription.get_plan_display() if hasattr(org, "subscription") else "Free"} plan allows max {limits["max_agents"]} agent(s). Upgrade to add more.'
        elif User.objects.filter(username=username).exists():
            error = 'Username already exists.'
        else:
            user = User.objects.create_user(
                username=username,
                password=password,
                email=email,
                first_name=first_name,
                last_name=last_name,
            )
            agent_profile = AgentProfile.objects.create(
                user=user,
                max_chats=max(1, max_chats),
                is_available=is_available,
                organization=org,
                role='agent',
            )
            # Grant website access
            website_ids = request.POST.getlist('websites')
            if website_ids:
                for ws_id in website_ids:
                    try:
                        ws = Website.objects.get(id=int(ws_id), organization=org)
                        AgentWebsiteAccess.objects.get_or_create(agent=agent_profile, website=ws)
                    except (Website.DoesNotExist, ValueError):
                        pass
            else:
                # Grant access to all websites by default
                for ws in Website.objects.filter(organization=org):
                    AgentWebsiteAccess.objects.get_or_create(agent=agent_profile, website=ws)
            created = True

    agents = list(
        User.objects
        .filter(agent_profile__isnull=False, agent_profile__organization=org)
        .select_related('agent_profile')
        .prefetch_related('agent_profile__website_access__website')
        .order_by('username')
    )
    websites = Website.objects.filter(organization=org)
    # Build accessible_websites from the prefetched access objects (no extra queries)
    for agent in agents:
        agent.accessible_websites = [a.website.name for a in agent.agent_profile.website_access.all()]
    return render(request, 'dashboard/add_agent.html', {
        'created': created,
        'error': error,
        'agents': agents,
        'org': org,
        'websites': websites,
    })


@login_required
def remove_agent(request, agent_id):
    """Remove an agent from the organization."""
    org = get_user_org(request.user)
    if not org or request.method != 'POST':
        return JsonResponse({'error': 'Not allowed'}, status=403)
    agent_user = get_object_or_404(User, id=agent_id, agent_profile__organization=org)
    # Can't remove yourself or org owner
    if agent_user == request.user or agent_user == org.owner:
        return JsonResponse({'error': 'Cannot remove owner or yourself'}, status=400)
    agent_user.agent_profile.delete()
    agent_user.delete()
    return JsonResponse({'status': 'ok'})


@login_required
def toggle_agent_availability(request, agent_id):
    """Toggle agent availability."""
    org = get_user_org(request.user)
    if not org or request.method != 'POST':
        return JsonResponse({'error': 'Not allowed'}, status=403)
    profile = get_object_or_404(AgentProfile, user_id=agent_id, organization=org)
    profile.is_available = not profile.is_available
    profile.save(update_fields=['is_available'])
    return JsonResponse({'status': 'ok', 'is_available': profile.is_available})


@login_required
@requires_feature('advanced_analytics', plan_label='Pro')
def most_clicked_elements_view(request):
    """Sorted list of elements visitors interact with most — what people
    actually click, not what we *think* they click. Filterable by date range
    and click type (normal / rage / dead)."""
    org = get_user_org(request.user)
    days = int(request.GET.get('days') or 30)
    days = max(1, min(days, 180))
    click_type = (request.GET.get('type') or 'click').strip()
    if click_type not in ('click', 'rage', 'dead', 'all'):
        click_type = 'click'

    from tracker.visitors.models import ClickData
    qs = ClickData.objects.filter(
        organization=org,
        timestamp__gte=timezone.now() - timedelta(days=days),
    )
    if click_type != 'all':
        qs = qs.filter(click_type=click_type)

    # Group by element. We coalesce on selector first (most specific),
    # then text, then tag — gives the agent something readable.
    from django.db.models import Count
    rows = (
        qs.exclude(element_text='', element_selector='')
        .values('element_text', 'element_tag', 'element_selector', 'page_path')
        .annotate(clicks=Count('id'))
        .order_by('-clicks')[:100]
    )
    total = qs.count()

    return render(request, 'dashboard/most_clicked.html', {
        'rows': rows,
        'total': total,
        'days': days,
        'click_type': click_type,
    })


@login_required
@requires_feature('advanced_analytics', plan_label='Pro')
def page_engagement_view(request):
    """Per-page rollup: pageviews, avg time, bounce rate, scroll depth proxy,
    click density, rage clicks, engagement score — one row per URL. Replaces
    the disconnected slice views with a single sortable table."""
    org = get_user_org(request.user)
    days = int(request.GET.get('days') or 30)
    days = max(1, min(days, 180))
    since = timezone.now() - timedelta(days=days)

    from django.db.models import Count, Avg, Sum, Q
    from tracker.visitors.models import PageView, ClickData

    # Pageview aggregates per URL.
    pv_rows = (
        PageView.objects.filter(
            visitor__organization=org,
            timestamp__gte=since,
        )
        .values('url', 'page_title')
        .annotate(
            views=Count('id'),
            avg_time=Avg('time_spent'),
            avg_load_ms=Avg('load_time_ms'),
            entries=Count('id', filter=Q(is_entry=True)),
            exits=Count('id', filter=Q(is_exit=True)),
        )
        .order_by('-views')[:100]
    )

    # Click rollup keyed by page_path (ClickData uses page_path, PageView uses url).
    from urllib.parse import urlparse
    click_rows = (
        ClickData.objects.filter(
            organization=org,
            timestamp__gte=since,
        )
        .values('page_path')
        .annotate(
            clicks=Count('id'),
            rage=Count('id', filter=Q(click_type='rage')),
            dead=Count('id', filter=Q(click_type='dead')),
        )
    )
    by_path = {r['page_path']: r for r in click_rows}

    pages = []
    for r in pv_rows:
        path = urlparse(r['url']).path or '/'
        clicks = by_path.get(path, {})
        views = r['views'] or 0
        # Simple engagement score: time-weighted + click density - rage penalty.
        avg_time = r['avg_time'] or 0
        rage = clicks.get('rage', 0)
        dead = clicks.get('dead', 0)
        bounce_rate = round((r['exits'] / views * 100) if views else 0, 1)
        engagement = max(0, min(100, int(
            (min(avg_time, 120) / 1.2)              # up to 100 from time on page
            - (rage * 3)                              # rage hurts
            - (dead * 2)                              # dead hurts a bit less
            - (bounce_rate * 0.3)                     # bounce hurts
        )))
        pages.append({
            'url': r['url'], 'path': path,
            'title': r['page_title'] or path,
            'views': views,
            'avg_time': int(avg_time),
            'avg_load_ms': int(r['avg_load_ms'] or 0),
            'bounce_rate': bounce_rate,
            'clicks': clicks.get('clicks', 0),
            'rage': rage, 'dead': dead,
            'engagement': engagement,
        })

    return render(request, 'dashboard/page_engagement.html', {
        'pages': pages,
        'days': days,
    })


@login_required
@requires_feature('api_access', plan_label='Enterprise')
def email_mailboxes_view(request):
    """List + create EmailMailbox rows. Owner-only because credentials
    are sensitive even though encrypted at rest."""
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return HttpResponse('Forbidden — owners only.', status=403)
    org = get_user_org(request.user)
    from tracker.chat.models import EmailMailbox

    if request.method == 'POST':
        data = request.POST
        try:
            mb = EmailMailbox(
                organization=org,
                name=(data.get('name') or 'Support').strip()[:100],
                imap_host=data.get('imap_host', '').strip(),
                imap_port=int(data.get('imap_port') or 993),
                imap_use_ssl=data.get('imap_use_ssl') == 'on',
                imap_username=data.get('imap_username', '').strip(),
                imap_folder=(data.get('imap_folder') or 'INBOX').strip(),
                smtp_host=data.get('smtp_host', '').strip(),
                smtp_port=int(data.get('smtp_port') or 587),
                smtp_use_tls=data.get('smtp_use_tls') == 'on',
                smtp_username=data.get('smtp_username', '').strip(),
                from_email=data.get('from_email', '').strip(),
                from_name=data.get('from_name', '').strip()[:100],
                is_enabled=True,
            )
            mb.imap_password_plain = data.get('imap_password', '').strip()
            mb.smtp_password_plain = data.get('smtp_password', '').strip()
            mb.save()
            _log_activity(org, request.user, 'mailbox.created', f'Created mailbox: {mb.name}',
                          target_type='mailbox', target_id=str(mb.id))
        except (ValueError, TypeError) as e:
            return render(request, 'dashboard/email_mailboxes.html', {
                'mailboxes': EmailMailbox.objects.filter(organization=org),
                'error': str(e),
            })

    mailboxes = EmailMailbox.objects.filter(organization=org).order_by('name')
    return render(request, 'dashboard/email_mailboxes.html', {
        'mailboxes': mailboxes,
    })


@login_required
@requires_feature('api_access', plan_label='Enterprise')
def email_mailbox_delete(request, mailbox_id):
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner or request.method != 'POST':
        return JsonResponse({'error': 'Not allowed'}, status=403)
    org = get_user_org(request.user)
    from tracker.chat.models import EmailMailbox
    EmailMailbox.objects.filter(id=mailbox_id, organization=org).delete()
    return JsonResponse({'status': 'ok'})


# ═══════════════════════════════════════════════════════════
# AI Topic Clustering + KB Gap Detector + Help Article Generator
# ═══════════════════════════════════════════════════════════

@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_insights_view(request):
    """Combined admin view that surfaces three AI-derived insights:
    chat topic clusters, KB gaps, and a help-article composer."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return HttpResponse('Forbidden — owners only.', status=403)
    return render(request, 'dashboard/ai_insights.html', {})


@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_topic_clusters(request):
    """POST → analyse last 30 days of chats → cluster into themes."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({'error': 'AI not configured'}, status=400)

    from tracker.core.throttle import check as throttle_check
    if throttle_check(request, action='ai_clusters', limit=5, window=300,
                       key=f'org:{org.id}').blocked:
        return JsonResponse({'error': 'Too many cluster requests — please wait 5 min'}, status=429)

    since = timezone.now() - timedelta(days=30)
    # Sample first message per chat — represents the intent.
    msgs = (Message.objects
            .filter(room__organization=org, sender_type='visitor',
                    timestamp__gte=since)
            .order_by('room_id', 'timestamp')
            .values_list('content', flat=True)[:200])
    snippets = [m[:400] for m in msgs if m and len(m.strip()) > 10]
    if not snippets:
        return JsonResponse({'error': 'Not enough chats in the last 30 days'}, status=400)

    from tracker.chat.ai import cluster_chat_topics
    result = cluster_chat_topics(config, snippets)
    if not result:
        return JsonResponse({'error': 'AI did not return a valid response'}, status=502)
    return JsonResponse({'ok': True, 'result': result, 'sample_size': len(snippets)})


@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_kb_gaps(request):
    """POST → analyse recent chats → suggest KB articles."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({'error': 'AI not configured'}, status=400)

    from tracker.core.throttle import check as throttle_check
    if throttle_check(request, action='ai_kb_gaps', limit=5, window=300,
                       key=f'org:{org.id}').blocked:
        return JsonResponse({'error': 'Slow down'}, status=429)

    since = timezone.now() - timedelta(days=14)
    # Take first visitor message per recent chat.
    chats = (ChatRoom.objects.filter(organization=org, created_at__gte=since)
             .values_list('id', flat=True)[:150])
    snippets = []
    for cid in chats:
        first = Message.objects.filter(room_id=cid, sender_type='visitor').order_by('timestamp').first()
        if first and first.content:
            snippets.append(first.content[:300])

    from tracker.chat.ai import detect_kb_gaps
    gaps = detect_kb_gaps(config, snippets) or []
    return JsonResponse({'ok': True, 'gaps': gaps, 'sample_size': len(snippets)})


@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_article_generator(request):
    """POST → raw text → polished article. Optionally saves directly to KB."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({'error': 'AI not configured'}, status=400)

    from tracker.core.throttle import check as throttle_check
    if throttle_check(request, action='ai_article', limit=20, window=60,
                       key=f'user:{request.user.id}').blocked:
        return JsonResponse({'error': 'Slow down'}, status=429)

    data = _parse_json_body(request) or {}
    raw = (data.get('raw') or '').strip()
    if not raw or len(raw) < 30:
        return JsonResponse({'error': 'Need at least 30 chars of raw input'}, status=400)

    from tracker.chat.ai import generate_help_article
    article = generate_help_article(config, raw)
    if not article:
        return JsonResponse({'error': 'AI did not return a valid article'}, status=502)

    # Optionally save straight to KB if `publish` is true + category supplied.
    if data.get('publish'):
        from tracker.chat.models import KBCategory, KBArticle
        from django.utils.text import slugify
        cat_name = (data.get('category') or 'General').strip()[:50]
        cat, _ = KBCategory.objects.get_or_create(
            organization=org, name=cat_name,
            defaults={'slug': slugify(cat_name)},
        )
        article_obj = KBArticle.objects.create(
            organization=org, category=cat,
            title=article.get('title', '')[:300],
            slug=slugify(article.get('title', ''))[:300] or uuid.uuid4().hex[:12],
            content=article.get('content_markdown', ''),
            author=request.user,
            is_published=True,
        )
        return JsonResponse({'ok': True, 'article': article, 'article_id': article_obj.id, 'published': True})
    return JsonResponse({'ok': True, 'article': article})


@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_quick_replies(request, room_id):
    """Mid-typing predictions: while a visitor is typing, return 3 short
    reply options the agent can click-to-send. Lighter than `ai_snippet`
    (one-liners, no slash command needed)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)

    from tracker.core.throttle import check as throttle_check
    state = throttle_check(request, action='ai_quick_reply', limit=40, window=60,
                            key=f'user:{request.user.id}')
    if state.blocked:
        return JsonResponse({'error': 'Slow down'}, status=429)

    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({'options': []})

    # Use last 6 messages for context — quick replies care about the freshest
    # context, not the full history.
    msgs = (Message.objects.filter(room=room).exclude(sender_type='system')
            .order_by('-timestamp')[:6])
    convo = []
    for m in reversed(list(msgs)):
        who = 'Visitor' if m.sender_type == 'visitor' else 'Agent'
        convo.append(f'{who}: {m.content[:200]}')

    from tracker.chat.ai import _call_llm
    system = (
        "Suggest THREE very short (max 8 words each) reply options for a "
        "support agent. They should be distinct in tone/intent: e.g., "
        "one acknowledging, one asking a clarifying question, one proposing "
        "an action. Return ONLY a JSON array of 3 strings, no preamble."
    )
    user = "Conversation so far:\n" + ('\n'.join(convo) if convo else '(empty)') + "\n\nThree quick-reply options:"
    out = _call_llm(config, system=system, messages=[{'role': 'user', 'content': user}], max_tokens=120)
    if not out:
        return JsonResponse({'options': []})
    import json as _json, re as _re
    cleaned = _re.sub(r'^```(?:json)?|```$', '', out, flags=_re.MULTILINE).strip()
    try:
        parsed = _json.loads(cleaned)
        if isinstance(parsed, list):
            options = [str(x)[:120] for x in parsed[:3]]
            return JsonResponse({'options': options})
    except Exception:
        pass
    # Fallback: split lines.
    lines = [l.strip(' -•"\'') for l in out.split('\n') if l.strip()][:3]
    return JsonResponse({'options': lines})


@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def voice_transcribe(request):
    """Accept an audio blob from the agent's push-to-talk mic, ship it to
    Gemini multimodal for transcription, return the text.

    Body: multipart/form-data with `audio` file. Anthropic doesn't have a
    public audio-in endpoint yet, so we route through Gemini regardless of
    the org's configured provider — but we still require an API key on
    AIBotConfig so it's an opt-in feature.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({'error': 'AI not configured'}, status=400)
    api_key = config.api_key_plain
    if not api_key:
        return JsonResponse({'error': 'AI key empty'}, status=400)

    from tracker.core.throttle import check as throttle_check
    state = throttle_check(request, action='voice_transcribe', limit=30, window=60,
                            key=f'user:{request.user.id}')
    if state.blocked:
        return JsonResponse({'error': 'Slow down'}, status=429)

    audio = request.FILES.get('audio')
    if not audio:
        return JsonResponse({'error': 'audio file missing'}, status=400)
    raw = audio.read()
    if len(raw) > 8 * 1024 * 1024:
        return JsonResponse({'error': 'audio too large (8MB max)'}, status=413)
    mime = audio.content_type or 'audio/webm'

    # Gemini accepts inline base64 audio in the `parts` array.
    import base64, json as _json, urllib.request, urllib.error
    payload = {
        'contents': [{
            'role': 'user',
            'parts': [
                {'text': 'Transcribe this audio exactly. Return ONLY the transcript text, no preamble.'},
                {'inlineData': {'mimeType': mime, 'data': base64.b64encode(raw).decode('ascii')}},
            ],
        }],
        'generationConfig': {'maxOutputTokens': 800, 'temperature': 0.1},
    }
    model = 'gemini-2.0-flash'
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                  headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        logger.warning('gemini transcribe HTTP %s', e.code)
        return JsonResponse({'error': f'transcribe failed ({e.code})'}, status=502)
    except Exception:
        logger.warning('gemini transcribe failed', exc_info=True)
        return JsonResponse({'error': 'transcribe failed'}, status=502)
    try:
        text = ''.join(p.get('text', '') for p in data['candidates'][0]['content']['parts']).strip()
    except Exception:
        text = ''
    if not text:
        return JsonResponse({'error': 'empty transcript'}, status=502)
    return JsonResponse({'ok': True, 'text': text})


@login_required
def notes_broadcast(request, room_id):
    """Multi-agent collaboration on internal notes — agent A's keystrokes
    appear in agent B's note panel in real-time. Backed by the same
    Channels group as the chat itself."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    data = _parse_json_body(request) or {}
    text = (data.get('text') or '')[:5000]
    agent_name = request.user.get_full_name() or request.user.username

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'chat_{room.room_id}',
        {
            'type': 'notes_typing',
            'agent_name': agent_name,
            'agent_id': request.user.id,
            'text': text,
            'timestamp': timezone.now().isoformat(),
        }
    )
    return JsonResponse({'ok': True})


@login_required
@requires_feature('email_notifications', plan_label='Pro')
def chat_send_reopen_link(request, room_id):
    """Agent → visitor: email a one-time link to resume this chat later.
    POST body: {"email": "...", "days": 7 (optional)}."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    data = _parse_json_body(request) or {}
    email = (data.get('email') or room.visitor_email or '').strip()
    if not email:
        return JsonResponse({'error': 'email required (no visitor email on file)'}, status=400)
    try:
        days = int(data.get('days', 7) or 7)
    except (ValueError, TypeError):
        days = 7
    days = max(1, min(days, 30))

    from tracker.chat.models import ChatReopenToken
    import secrets as py_secrets
    token = py_secrets.token_urlsafe(32)
    tok = ChatReopenToken.objects.create(
        room=room, token=token, created_by=request.user,
        sent_to_email=email,
        expires_at=timezone.now() + timedelta(days=days),
    )
    link = request.build_absolute_uri(f'/chat/reopen/{token}/')
    try:
        from django.core.mail import send_mail
        send_mail(
            subject=f'Continue your chat with {org.name}',
            message=(
                f"Hi {room.visitor_name or 'there'},\n\n"
                f"Tap the link below to pick up where we left off — your "
                f"agent will be reassigned and the full transcript stays "
                f"intact:\n\n{link}\n\n"
                f"The link expires in {days} day{'s' if days != 1 else ''}."
            ),
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=[email],
            fail_silently=True,
        )
    except Exception:
        logger.warning('reopen-link email failed', exc_info=True)
    _log_activity(org, request.user, 'chat.reopen_sent',
                  f'Sent reopen link for chat #{room_id} to {email}',
                  target_type='chat', target_id=room_id)
    return JsonResponse({'ok': True, 'expires_at': tok.expires_at.isoformat()})


@login_required
def toggle_agent_dnd(request):
    """Toggle DND for the *current* user. DND is stronger than 'unavailable':
    auto-assign skips entirely and (optionally) shows a custom away message."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    profile = getattr(request.user, 'agent_profile', None)
    if not profile:
        return JsonResponse({'error': 'No agent profile'}, status=400)
    data = _parse_json_body(request) or {}
    profile.do_not_disturb = bool(data.get('enabled', not profile.do_not_disturb))
    msg = (data.get('message') or '').strip()
    if msg:
        profile.dnd_message = msg[:200]
    profile.save(update_fields=['do_not_disturb', 'dnd_message'])
    return JsonResponse({
        'status': 'ok',
        'do_not_disturb': profile.do_not_disturb,
        'dnd_message': profile.dnd_message,
    })


@login_required
def chat_takeover(request, room_id):
    """Owner/supervisor takes over a chat from another agent."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    prev_agent = room.agent
    prev_name = prev_agent.get_full_name() if prev_agent else 'Unassigned'
    new_name = request.user.get_full_name() or request.user.username

    room.agent = request.user
    room.status = 'active'
    room.save(update_fields=['agent', 'status', 'updated_at'])

    msg = f'{new_name} took over from {prev_name}'
    Message.objects.create(room=room, sender_type='system', sender_name='System', content=msg)

    channel_layer = get_channel_layer()
    async_to_sync(channel_layer.group_send)(
        f'chat_{room_id}',
        {'type': 'chat_message', 'message': msg, 'sender_type': 'system', 'sender_name': 'System',
         'msg_type': 'text', 'file_url': '', 'file_name': '', 'timestamp': timezone.now().isoformat()}
    )
    _log_activity(org, request.user, 'chat.takeover', f'{new_name} took over chat #{room_id} from {prev_name}', 'chat', room_id)
    return JsonResponse({'status': 'ok', 'agent_name': new_name})


@login_required
def chat_transcript(request, room_id):
    """Download chat transcript as text file."""
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    messages_list = room.messages.all()

    lines = [
        f'Chat Transcript - {room.visitor_name}',
        f'Room: {room.room_id}',
        f'Date: {room.created_at.strftime("%Y-%m-%d %H:%M")}',
        f'Agent: {room.agent.get_full_name() if room.agent else "Unassigned"}',
        f'Status: {room.status}',
        '-' * 50,
        '',
    ]
    for msg in messages_list:
        time_str = msg.timestamp.strftime('%H:%M')
        lines.append(f'[{time_str}] {msg.sender_name} ({msg.sender_type}): {msg.content}')
        if msg.file:
            lines.append(f'  [File: {msg.file_name}]')

    response = HttpResponse('\n'.join(lines), content_type='text/plain; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="transcript_{room.room_id}.txt"'
    return response


@login_required
def email_transcript(request, room_id):
    """Email chat transcript to visitor or custom email."""
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        email = data.get('email', '').strip() or room.visitor_email
        if not email:
            return JsonResponse({'error': 'No email address provided'}, status=400)

        messages_list = room.messages.all()
        try:
            from tracker.core.email_utils import send_chat_transcript
            send_chat_transcript(org, room, messages_list, email)
            _log_activity(org, request.user, 'transcript.sent', f'Transcript emailed to {email} for chat #{room_id}')
            return JsonResponse({'status': 'ok', 'email': email})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def export_chat_html(request, room_id):
    """Export chat as styled HTML file."""
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    messages_list = room.messages.all()

    html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Chat #{room.room_id}</title>
<style>
body{{font-family:Inter,Arial,sans-serif;max-width:600px;margin:40px auto;padding:20px;background:#f8f9fa;}}
h1{{font-size:18px;color:#1e1b4b;}} .meta{{color:#6b7280;font-size:12px;margin-bottom:20px;}}
.msg{{margin:8px 0;padding:10px 14px;border-radius:12px;max-width:80%;font-size:14px;line-height:1.5;}}
.visitor{{background:#7c3aed;color:white;margin-left:auto;border-bottom-right-radius:4px;}}
.agent{{background:#eef2ff;color:#1f2937;border-bottom-left-radius:4px;}}
.system{{background:#fef3c7;color:#92400e;text-align:center;margin:4px auto;font-size:12px;border-radius:8px;}}
.time{{font-size:10px;color:#9ca3af;margin-top:3px;}} .sender{{font-size:11px;font-weight:600;color:#6b7280;margin-bottom:2px;}}
</style></head><body>
<h1>Chat with {room.visitor_name}</h1>
<div class="meta">Room: {room.room_id} | Date: {room.created_at.strftime("%Y-%m-%d %H:%M")} | Agent: {room.agent.get_full_name() if room.agent else "Unassigned"} | Status: {room.status}</div>
'''
    for msg in messages_list:
        t = msg.timestamp.strftime("%H:%M")
        html += f'<div class="msg {msg.sender_type}"><div class="sender">{msg.sender_name}</div>{msg.content}<div class="time">{t}</div></div>\n'
    html += '<div style="text-align:center;margin-top:30px;color:#9ca3af;font-size:11px;">Exported from LiveVisitorHub</div></body></html>'

    response = HttpResponse(html, content_type='text/html; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="chat_{room.room_id}.html"'
    return response


@login_required
def onboarding_view(request):
    """Post-registration onboarding - widget install guide."""
    org = get_user_org(request.user)
    script_url = request.build_absolute_uri('/api/widget/script.js')
    embed_code = f'<script src="{script_url}?key={org.widget_key}" defer></script>'
    return render(request, 'dashboard/onboarding.html', {
        'org': org,
        'embed_code': embed_code,
        'widget_key': org.widget_key,
    })


@login_required
def profile_view(request):
    """Agent profile - change name, password, avatar color."""
    org = get_user_org(request.user)
    profile = request.user.agent_profile
    saved = False
    error = ''

    if request.method == 'POST':
        action = request.POST.get('action', '')

        if action == 'update_profile':
            request.user.first_name = request.POST.get('first_name', '').strip()
            request.user.last_name = request.POST.get('last_name', '').strip()
            request.user.email = request.POST.get('email', '').strip()
            request.user.save(update_fields=['first_name', 'last_name', 'email'])
            avatar_color = request.POST.get('avatar_color', '').strip()
            if avatar_color:
                profile.avatar_color = avatar_color
                profile.save(update_fields=['avatar_color'])
            saved = True

        elif action == 'change_password':
            from django.contrib.auth import update_session_auth_hash
            current = request.POST.get('current_password', '')
            new_pw = request.POST.get('new_password', '')
            confirm = request.POST.get('confirm_password', '')
            if not request.user.check_password(current):
                error = 'Current password is incorrect.'
            elif new_pw != confirm:
                error = 'New passwords do not match.'
            elif len(new_pw) < 6:
                error = 'Password must be at least 6 characters.'
            else:
                request.user.set_password(new_pw)
                request.user.save()
                update_session_auth_hash(request, request.user)
                saved = True

    return render(request, 'dashboard/profile.html', {
        'profile': profile,
        'org': org,
        'saved': saved,
        'error': error,
    })


@login_required
def chat_search_view(request):
    """Search across all chat messages."""
    org = get_user_org(request.user)
    query = request.GET.get('q', '').strip()
    results = []
    if query and len(query) >= 2:
        results = Message.objects.filter(
            room__organization=org,
            content__icontains=query,
        ).select_related('room').order_by('-timestamp')[:50]
    return render(request, 'dashboard/chat_search.html', {
        'query': query,
        'results': results,
    })


# ===== WEBHOOK MANAGEMENT =====

@login_required
@requires_feature('api_access', plan_label='Enterprise')
def webhook_list(request):
    """Manage webhooks for chat events."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        url = request.POST.get('url', '').strip()
        provider = request.POST.get('provider', 'generic').strip()
        events = ','.join(request.POST.getlist('events'))
        secret = request.POST.get('secret', '').strip()
        if url:
            allowed_providers = {p[0] for p in Webhook.PROVIDER_CHOICES}
            if provider not in allowed_providers:
                provider = 'generic'
            wh = Webhook(organization=org, url=url, provider=provider, events=events)
            # Round-trip the HMAC secret through the encrypted setter.
            wh.secret_plain = secret
            wh.save()
            _log_activity(org, request.user, 'webhook.created', f'Webhook created: {url[:50]} ({provider})')
    webhooks = Webhook.objects.filter(organization=org)
    event_choices = Webhook.EVENT_CHOICES
    provider_choices = Webhook.PROVIDER_CHOICES
    return render(request, 'dashboard/webhooks.html', {
        'webhooks': webhooks,
        'event_choices': event_choices,
        'provider_choices': provider_choices,
    })


@login_required
def webhook_delete(request, webhook_id):
    if request.method == 'POST':
        org = get_user_org(request.user)
        wh = get_object_or_404(Webhook, id=webhook_id, organization=org)
        wh.delete()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def webhook_toggle(request, webhook_id):
    if request.method == 'POST':
        org = get_user_org(request.user)
        wh = get_object_or_404(Webhook, id=webhook_id, organization=org)
        wh.is_active = not wh.is_active
        wh.save(update_fields=['is_active'])
        return JsonResponse({'status': 'ok', 'is_active': wh.is_active})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def webhook_delivery_log(request, webhook_id):
    """Per-webhook delivery audit trail — last 100 attempts with status + retry button."""
    org = get_user_org(request.user)
    wh = get_object_or_404(Webhook, id=webhook_id, organization=org)
    deliveries = wh.deliveries.order_by('-created_at')[:100]
    # Single GROUP BY pass instead of 4 separate counts (each was hitting the DB).
    agg = wh.deliveries.aggregate(
        total=Count('id'),
        success=Count('id', filter=Q(status='success')),
        pending=Count('id', filter=Q(status='pending')),
        failed=Count('id', filter=Q(status='failed')),
    )
    counts = {
        'total': agg['total'] or 0,
        'success': agg['success'] or 0,
        'pending': agg['pending'] or 0,
        'failed': agg['failed'] or 0,
    }
    return render(request, 'dashboard/webhook_log.html', {
        'webhook': wh,
        'deliveries': deliveries,
        'counts': counts,
    })


@login_required
def webhook_delivery_retry(request, delivery_id):
    """Manually re-fire a single failed/pending delivery from the dashboard."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    delivery = get_object_or_404(
        WebhookDelivery.objects.select_related('webhook'),
        id=delivery_id, webhook__organization=org,
    )
    # Reset for a fresh attempt — the existing attempt_count is preserved as
    # history; we just clear the next_retry gate so process_webhook_retries
    # (or this synchronous call) picks it up.
    delivery.status = 'pending'
    delivery.next_retry_at = None
    delivery.save(update_fields=['status', 'next_retry_at'])
    ok = _attempt_webhook_delivery(delivery)
    return JsonResponse({'status': 'ok' if ok else 'failed', 'response_status': delivery.response_status})


def _format_slack_payload(event, payload, org_name):
    """Render a chat event as a Slack/Discord-compatible message block."""
    title_map = {
        'chat.created': ':speech_balloon: New chat started',
        'chat.assigned': ':bust_in_silhouette: Chat assigned',
        'chat.transferred': ':twisted_rightwards_arrows: Chat transferred',
        'chat.closed': ':white_check_mark: Chat closed',
        'chat.rated': ':star: Chat rated',
        'message.new': ':envelope: New message',
        'visitor.new': ':wave: New visitor',
        'agent.joined': ':office: Agent joined',
        'offline.message': ':inbox_tray: Offline message received',
        'sla.breached': ':rotating_light: SLA breached',
    }
    title = title_map.get(event, f':bell: {event}')
    fields = []
    for label, key in (
        ('Visitor', 'visitor_name'),
        ('Email', 'visitor_email'),
        ('Room', 'room_id'),
        ('Agent', 'agent_name'),
        ('Duration', 'duration'),
        ('Rating', 'rating'),
    ):
        val = payload.get(key)
        if val:
            fields.append({'title': label, 'value': str(val), 'short': True})
    text_lines = [f"*{title}* — _{org_name}_"]
    for f in fields:
        text_lines.append(f"• *{f['title']}*: {f['value']}")
    return {
        'text': '\n'.join(text_lines),
        'username': 'LiveTrack',
        'attachments': [{
            'color': '#7c3aed',
            'fields': fields,
            'footer': 'LiveTrack',
        }] if fields else [],
    }


def _attempt_webhook_delivery(delivery):
    """Single delivery attempt — returns True on success, False otherwise.

    Updates the delivery row in place with status/response data so the admin
    UI and retry command can track lifecycle without extra round-trips.

    Designed to be safe to call from background threads: any DB connection it
    opens is closed before returning so the pool doesn't leak.
    """
    import hashlib
    import hmac
    import urllib.request
    from django.db import close_old_connections

    wh = delivery.webhook
    payload = delivery.payload or {}
    org_name = wh.organization.name if wh.organization_id else ''
    if wh.provider in ('slack', 'discord'):
        body = json.dumps(_format_slack_payload(delivery.event, payload, org_name)).encode()
        headers = {'Content-Type': 'application/json'}
    else:
        body = json.dumps(payload).encode()
        headers = {
            'Content-Type': 'application/json',
            'X-LiveTrack-Event': delivery.event,
        }
    secret_plain = wh.secret_plain
    if secret_plain:
        sig = hmac.new(secret_plain.encode(), body, hashlib.sha256).hexdigest()
        headers['X-LiveTrack-Signature'] = sig

    delivery.attempt_count = (delivery.attempt_count or 0) + 1
    try:
        try:
            req = urllib.request.Request(wh.url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                delivery.response_status = resp.status
                delivery.status = 'success'
                delivery.completed_at = timezone.now()
                delivery.last_error = ''
                delivery.next_retry_at = None
                delivery.save(update_fields=['attempt_count', 'response_status', 'status', 'completed_at', 'last_error', 'next_retry_at'])
                return True
        except urllib.request.HTTPError as exc:
            # 4xx is a permanent client error — don't waste retries on it.
            permanent = 400 <= exc.code < 500
            delivery.response_status = exc.code
            delivery.last_error = f'HTTP {exc.code}'
            if permanent or delivery.attempt_count >= 4:
                delivery.status = 'failed'
                delivery.completed_at = timezone.now()
                delivery.next_retry_at = None
            else:
                delays = [60, 300, 1500]
                delivery.next_retry_at = timezone.now() + timedelta(seconds=delays[min(delivery.attempt_count - 1, len(delays) - 1)])
                delivery.status = 'pending'
            delivery.save(update_fields=['attempt_count', 'response_status', 'last_error', 'next_retry_at', 'status', 'completed_at'])
            return False
        except Exception as exc:
            # Network / timeout / DNS — transient, retry.
            delivery.last_error = str(exc)[:500]
            delays = [60, 300, 1500]
            if delivery.attempt_count < 4:
                delivery.next_retry_at = timezone.now() + timedelta(seconds=delays[min(delivery.attempt_count - 1, len(delays) - 1)])
                delivery.status = 'pending'
            else:
                delivery.status = 'failed'
                delivery.completed_at = timezone.now()
                delivery.next_retry_at = None
            delivery.save(update_fields=['attempt_count', 'last_error', 'next_retry_at', 'status', 'completed_at'])
            return False
    finally:
        # Critical for thread-pool safety: hand the connection back so we
        # don't exhaust Postgres' max_connections under load.
        close_old_connections()


# Process-wide bounded thread pools for background work. Without this, a
# spike in chat closes (each firing N webhooks) would spawn unbounded threads
# and exhaust Postgres' connection limit + the OS thread cap. 16 workers is
# enough headroom for normal traffic without hammering the DB.
import concurrent.futures as _cf
_WEBHOOK_POOL = _cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix='wh')
_AI_POOL = _cf.ThreadPoolExecutor(max_workers=4, thread_name_prefix='ai')


def fire_webhook(org, event, payload):
    """Enqueue webhooks for an event (non-blocking, via bounded thread pool).

    Each subscribing webhook gets a WebhookDelivery row. First attempt is
    submitted to a 16-thread pool; if the pool is saturated the row stays
    'pending' and the `process_webhook_retries` cron picks it up next minute.
    """
    if not org:
        return
    enriched = dict(payload)
    enriched.setdefault('event', event)
    webhooks = Webhook.objects.filter(organization=org, is_active=True)
    for wh in webhooks:
        subscribed = [e.strip() for e in wh.events.split(',') if e.strip()]
        if event not in subscribed:
            continue
        delivery = WebhookDelivery.objects.create(webhook=wh, event=event, payload=enriched)
        try:
            _WEBHOOK_POOL.submit(_attempt_webhook_delivery, delivery)
        except RuntimeError:
            # Pool shutting down (interpreter teardown) — leave the row as
            # pending so the retry cron handles it.
            pass


# ===== ACTIVITY LOG =====

def _log_activity(org, user, action, description, target_type='', target_id=''):
    """Helper to log an activity."""
    ActivityLog.objects.create(
        organization=org, user=user, action=action,
        description=description, target_type=target_type, target_id=target_id,
    )


@login_required
def activity_log_view(request):
    """View activity log for the organization."""
    org = get_user_org(request.user)
    logs = ActivityLog.objects.filter(organization=org).select_related('user')[:100]
    return render(request, 'dashboard/activity_log.html', {'logs': logs})


# ===== CHAT LABELS =====

@login_required
def chat_labels_view(request):
    """Manage chat labels/categories."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        color = request.POST.get('color', '#6366f1').strip()
        if name:
            ChatLabel.objects.get_or_create(organization=org, name=name, defaults={'color': color})
    labels = ChatLabel.objects.filter(organization=org)
    return render(request, 'dashboard/chat_labels.html', {'labels': labels})


@login_required
def delete_label(request, label_id):
    if request.method == 'POST':
        org = get_user_org(request.user)
        label = get_object_or_404(ChatLabel, id=label_id, organization=org)
        label.delete()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


# ===== VISITOR BAN LIST =====

@login_required
def ban_list_view(request):
    """View all banned visitors."""
    org = get_user_org(request.user)
    search = request.GET.get('q', '').strip()
    banned = Visitor.objects.filter(organization=org, is_banned=True)
    if search:
        banned = banned.filter(
            Q(ip_address__icontains=search) | Q(country__icontains=search) | Q(city__icontains=search)
        )
    return render(request, 'dashboard/ban_list.html', {'banned': banned, 'search': search})


@login_required
def analytics_view(request):
    """Chat analytics with charts - response time, CSAT trends, volume."""
    org = get_user_org(request.user)
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    ws_filter = get_website_filter(request, org)
    chats_qs = ChatRoom.objects.filter(organization=org, **ws_filter)
    visitors_qs = Visitor.objects.filter(organization=org, **ws_filter)

    # Last 7 days chat volume
    daily_chats = []
    for i in range(6, -1, -1):
        day = today_start - timedelta(days=i)
        next_day = day + timedelta(days=1)
        count = chats_qs.filter(created_at__gte=day, created_at__lt=next_day).count()
        daily_chats.append({'day': day.strftime('%a'), 'date': day.strftime('%d %b'), 'count': count})

    # Last 7 days CSAT
    daily_csat = []
    for i in range(6, -1, -1):
        day = today_start - timedelta(days=i)
        next_day = day + timedelta(days=1)
        avg = chats_qs.filter(created_at__gte=day, created_at__lt=next_day, rating__isnull=False).aggregate(a=Avg('rating'))['a']
        daily_csat.append({'day': day.strftime('%a'), 'avg': round(avg, 1) if avg else 0})

    # Chat status breakdown
    status_counts = {
        'waiting': chats_qs.filter(status='waiting').count(),
        'active': chats_qs.filter(status='active').count(),
        'closed': chats_qs.filter(status='closed').count(),
    }

    # Top visitor countries
    country_stats = list(visitors_qs.exclude(country='').values('country').annotate(count=Count('id')).order_by('-count')[:10])

    # Busiest hours
    from django.db.models.functions import ExtractHour
    hourly_chats = list(chats_qs.annotate(hour=ExtractHour('created_at')).values('hour').annotate(count=Count('id')).order_by('hour'))

    total_chats = chats_qs.count()
    avg_rating = chats_qs.filter(rating__isnull=False).aggregate(a=Avg('rating'))['a']
    total_messages = Message.objects.filter(room__organization=org, **{k.replace('website_id', 'room__website_id'): v for k, v in ws_filter.items()}).count()
    total_visitors = visitors_qs.count()
    closed_chats = status_counts['closed']
    completion_rate = round((closed_chats / total_chats * 100), 1) if total_chats > 0 else 0

    # Top agents by chats
    top_agents = User.objects.filter(
        agent_profile__organization=org
    ).annotate(
        chats_count=Count('chat_rooms', filter=Q(chat_rooms__organization=org, **{f'chat_rooms__{k}': v for k, v in ws_filter.items()})),
        avg_agent_rating=Avg('chat_rooms__rating', filter=Q(chat_rooms__rating__isnull=False)),
    ).order_by('-chats_count')[:5]

    # Browser breakdown
    browser_stats = list(visitors_qs.values('browser').annotate(count=Count('id')).order_by('-count')[:6])
    total_browser = sum(b['count'] for b in browser_stats) or 1

    # Device breakdown
    device_stats = list(visitors_qs.values('device_type').annotate(count=Count('id')).order_by('-count'))
    total_device = sum(d['count'] for d in device_stats) or 1

    # Rating distribution
    rating_dist = []
    for i in range(5, 0, -1):
        c = chats_qs.filter(rating=i).count()
        rating_dist.append({'stars': i, 'count': c})

    return render(request, 'dashboard/analytics.html', {
        'daily_chats': daily_chats,
        'daily_csat': daily_csat,
        'status_counts': status_counts,
        'country_stats': country_stats,
        'hourly_chats': hourly_chats,
        'total_chats': total_chats,
        'avg_rating': avg_rating,
        'total_messages': total_messages,
        'total_visitors': total_visitors,
        'completion_rate': completion_rate,
        'top_agents': top_agents,
        'browser_stats': browser_stats,
        'total_browser': total_browser,
        'device_stats': device_stats,
        'total_device': total_device,
        'rating_dist': rating_dist,
    })


@login_required
def live_visitors_api(request):
    """API: Real-time visitor activity with current pages."""
    org = get_user_org(request.user)
    last_5_min = timezone.now() - timedelta(minutes=5)
    ws_filter = get_website_filter(request, org)
    visitors = Visitor.objects.filter(
        organization=org, last_seen__gte=last_5_min, **ws_filter
    ).order_by('-last_seen')[:20]

    data = []
    for v in visitors:
        last_page = v.page_views.order_by('-timestamp').first()
        data.append({
            'id': v.id,
            'ip': v.ip_address,
            'browser': v.browser,
            'os': v.os,
            'device': v.device_type,
            'country': v.country or '-',
            'city': v.city or '-',
            'score': v.score,
            'score_label': v.score_label,
            'current_page': last_page.url if last_page else '-',
            'page_title': last_page.page_title if last_page else '-',
            'last_seen': v.last_seen.isoformat(),
            'total_pages': v.page_views.count(),
            'is_chatting': v.chat_rooms.filter(status__in=['waiting', 'active']).exists(),
        })
    return JsonResponse({'visitors': data})


@login_required
def notification_center_view(request):
    """Notification center - recent activity."""
    org = get_user_org(request.user)
    logs = ActivityLog.objects.filter(organization=org).select_related('user')[:50]
    return render(request, 'dashboard/notification_center.html', {'logs': logs})


# ===== CHAT SNOOZE =====

@login_required
def chat_snooze(request, room_id):
    """Snooze a chat - hide for X minutes then remind."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    data = _parse_json_body(request) or {}
    minutes = int(data.get('minutes', 15))
    room.is_snoozed = True
    room.snooze_until = timezone.now() + timedelta(minutes=minutes)
    room.save(update_fields=['is_snoozed', 'snooze_until'])
    _log_activity(org, request.user, 'chat.snoozed', f'Snoozed chat #{room_id} for {minutes} minutes', 'chat', room_id)
    return JsonResponse({'status': 'ok', 'snooze_until': room.snooze_until.isoformat()})


# ===== CHAT BOOKMARK =====

@login_required
def chat_bookmark(request, room_id):
    """Toggle bookmark on a chat."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    room.is_bookmarked = not room.is_bookmarked
    room.save(update_fields=['is_bookmarked'])
    return JsonResponse({'status': 'ok', 'is_bookmarked': room.is_bookmarked})


# ===== BULK ACTIONS =====

@login_required
def chat_bulk_action(request):
    """Perform bulk action on multiple chats."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    action = data.get('action', '')
    room_ids = data.get('room_ids', [])
    if not room_ids:
        return JsonResponse({'error': 'No chats selected'}, status=400)

    rooms = ChatRoom.objects.filter(room_id__in=room_ids, organization=org)
    selected_count = len(room_ids)
    matched_count = rooms.count()
    count = 0

    if action == 'close':
        count = rooms.filter(status__in=['waiting', 'active']).update(status='closed', closed_at=timezone.now())
    elif action == 'assign':
        agent_id = data.get('agent_id')
        if agent_id:
            count = rooms.update(agent_id=agent_id, status='active')
    elif action == 'bookmark':
        count = rooms.update(is_bookmarked=True)
    elif action == 'delete_bookmark':
        count = rooms.update(is_bookmarked=False)
    elif action == 'high_priority':
        count = rooms.update(priority='high')

    failed_count = max(selected_count - count, 0)
    _log_activity(org, request.user, f'bulk.{action}', f'Bulk {action} on {count} chats')
    return JsonResponse({
        'status': 'ok',
        'action': action,
        'selected': selected_count,
        'matched': matched_count,
        'affected': count,
        'failed': failed_count,
    })


# ===== SAVED REPLIES =====

@login_required
def saved_replies_view(request):
    """Personal saved replies for agent."""
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        title = data.get('title', '').strip()
        message = data.get('message', '').strip()
        if title and message:
            SavedReply.objects.create(agent=request.user, title=title, message=message)
            return JsonResponse({'status': 'ok'})
        return JsonResponse({'error': 'Title and message required'}, status=400)

    replies = SavedReply.objects.filter(agent=request.user)
    return render(request, 'dashboard/saved_replies.html', {'replies': replies})


@login_required
def delete_saved_reply(request, reply_id):
    if request.method == 'POST':
        reply = get_object_or_404(SavedReply, id=reply_id, agent=request.user)
        reply.delete()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


# ═══════════════════════════════════════════════════════════
# FEATURE 7: DEPARTMENTS
# ═══════════════════════════════════════════════════════════

@login_required
def departments_view(request):
    """Manage agent departments."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            name = data.get('name', '').strip()
            if not name:
                return JsonResponse({'error': 'Name required'}, status=400)
            desc = data.get('description', '')
            color = data.get('color', '#6366f1')
            dept, created = Department.objects.get_or_create(
                organization=org, name=name,
                defaults={'description': desc, 'color': color}
            )
            if not created:
                return JsonResponse({'error': 'Department already exists'}, status=400)
            _log_activity(org, request.user, 'dept.created', f'Created department: {name}')
            return JsonResponse({'status': 'ok', 'id': dept.id})

        elif action == 'add_member':
            dept_id = data.get('department_id')
            agent_id = data.get('agent_id')
            is_lead = data.get('is_lead', False)
            dept = get_object_or_404(Department, id=dept_id, organization=org)
            profile = get_object_or_404(AgentProfile, id=agent_id, organization=org)
            DepartmentMember.objects.get_or_create(
                department=dept, agent=profile, defaults={'is_lead': is_lead}
            )
            return JsonResponse({'status': 'ok'})

        elif action == 'remove_member':
            dept_id = data.get('department_id')
            agent_id = data.get('agent_id')
            DepartmentMember.objects.filter(
                department_id=dept_id, agent_id=agent_id, department__organization=org
            ).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'delete':
            dept_id = data.get('department_id')
            Department.objects.filter(id=dept_id, organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'toggle':
            dept_id = data.get('department_id')
            dept = get_object_or_404(Department, id=dept_id, organization=org)
            dept.is_active = not dept.is_active
            dept.save(update_fields=['is_active'])
            return JsonResponse({'status': 'ok', 'is_active': dept.is_active})

    departments = Department.objects.filter(organization=org).prefetch_related('members__agent__user')
    agents = AgentProfile.objects.filter(organization=org).select_related('user')
    return render(request, 'dashboard/departments.html', {
        'departments': departments,
        'agents': agents,
    })


# ═══════════════════════════════════════════════════════════
# FEATURE 8: SLA MANAGEMENT
# ═══════════════════════════════════════════════════════════

@login_required
def sla_policies_view(request):
    """Manage SLA policies."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            SLAPolicy.objects.create(
                organization=org,
                name=data.get('name', 'Default SLA'),
                priority=data.get('priority', 'medium'),
                first_response_minutes=int(data.get('first_response_minutes', 5)),
                resolution_minutes=int(data.get('resolution_minutes', 60)),
            )
            _log_activity(org, request.user, 'sla.created', f'Created SLA policy: {data.get("name")}')
            return JsonResponse({'status': 'ok'})

        elif action == 'delete':
            SLAPolicy.objects.filter(id=data.get('policy_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'toggle':
            policy = get_object_or_404(SLAPolicy, id=data.get('policy_id'), organization=org)
            policy.is_active = not policy.is_active
            policy.save(update_fields=['is_active'])
            return JsonResponse({'status': 'ok', 'is_active': policy.is_active})

    policies = SLAPolicy.objects.filter(organization=org)
    now = timezone.now()
    sla_minutes = int(getattr(settings, 'CHAT_SLA_MINUTES', 5))

    # Check for SLA breaches on active chats
    active_chats = ChatRoom.objects.filter(organization=org, status__in=['waiting', 'active'])
    breaches_today = SLABreach.objects.filter(
        organization=org,
        breached_at__date=now.date()
    ).select_related('chat', 'policy')

    # Calculate at-risk chats
    at_risk = []
    for chat in active_chats:
        elapsed = (now - chat.created_at).total_seconds() / 60
        first_agent_msg = chat.messages.filter(sender_type='agent').first()
        if not first_agent_msg and elapsed > sla_minutes:
            at_risk.append({
                'chat': chat,
                'elapsed_minutes': int(elapsed),
                'target_minutes': sla_minutes,
            })

    return render(request, 'dashboard/sla_policies.html', {
        'policies': policies,
        'breaches_today': breaches_today,
        'at_risk': at_risk,
        'sla_minutes': sla_minutes,
    })


# ═══════════════════════════════════════════════════════════
# FEATURE 9: SURVEYS / NPS
# ═══════════════════════════════════════════════════════════

@login_required
def surveys_view(request):
    """Manage surveys and NPS."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            survey = Survey.objects.create(
                organization=org,
                title=data.get('title', 'Customer Survey'),
                description=data.get('description', ''),
                survey_type=data.get('survey_type', 'nps'),
                show_after_chat=data.get('show_after_chat', True),
            )
            # Add questions
            for i, q in enumerate(data.get('questions', [])):
                SurveyQuestion.objects.create(
                    survey=survey,
                    question_text=q.get('text', ''),
                    question_type=q.get('type', 'rating'),
                    choices=q.get('choices', ''),
                    order=i,
                    is_required=q.get('required', True),
                )
            _log_activity(org, request.user, 'survey.created', f'Created survey: {survey.title}')
            return JsonResponse({'status': 'ok', 'id': survey.id})

        elif action == 'delete':
            Survey.objects.filter(id=data.get('survey_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'toggle':
            survey = get_object_or_404(Survey, id=data.get('survey_id'), organization=org)
            survey.is_active = not survey.is_active
            survey.save(update_fields=['is_active'])
            return JsonResponse({'status': 'ok', 'is_active': survey.is_active})

    surveys = Survey.objects.filter(organization=org).prefetch_related('questions', 'responses')

    # NPS calculation
    nps_data = None
    nps_survey = surveys.filter(survey_type='nps', is_active=True).first()
    if nps_survey:
        responses = nps_survey.responses.all()
        total = responses.count()
        if total > 0:
            promoters = responses.filter(score__gte=9).count()
            detractors = responses.filter(score__lte=6).count()
            nps_score = round(((promoters - detractors) / total) * 100)
            nps_data = {
                'score': nps_score,
                'total': total,
                'promoters': promoters,
                'passives': total - promoters - detractors,
                'detractors': detractors,
            }

    return render(request, 'dashboard/surveys.html', {
        'surveys': surveys,
        'nps_data': nps_data,
    })


@login_required
def survey_detail_view(request, survey_id):
    """View survey responses and analytics."""
    org = get_user_org(request.user)
    survey = get_object_or_404(Survey, id=survey_id, organization=org)
    responses = survey.responses.select_related('visitor', 'chat').prefetch_related('answers__question')

    # Score distribution
    score_dist = {}
    for r in responses:
        if r.score is not None:
            score_dist[r.score] = score_dist.get(r.score, 0) + 1

    return render(request, 'dashboard/survey_detail.html', {
        'survey': survey,
        'responses': responses,
        'score_dist': score_dist,
    })


@csrf_exempt
def submit_survey_response(request, survey_id):
    """Public API: Visitor submits survey response."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    # 1 submission per visitor per minute is plenty — surveys are once per chat.
    from tracker.core.throttle import check as throttle_check
    ts = throttle_check(request, action=f'survey_submit_{survey_id}', limit=2, window=60)
    if ts.blocked:
        return JsonResponse({'error': 'Too many submissions. Please wait.'}, status=429)

    data = _parse_json_body(request) or {}
    # Cross-origin embed: cookies are blocked by SameSite, so the widget passes
    # session_key in the body. Cookie session_key is the fallback for same-origin.
    session_key = (data.get('session_key') or '').strip() or request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    survey = get_object_or_404(Survey, id=survey_id, is_active=True)
    org = survey.organization

    from tracker.visitors.models import Visitor
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    room_id = data.get('room_id')
    chat = ChatRoom.objects.filter(room_id=room_id).first() if room_id else None

    response = SurveyResponse.objects.create(
        survey=survey,
        visitor=visitor,
        chat=chat,
        score=data.get('score'),
    )

    for ans in data.get('answers', []):
        question_id = ans.get('question_id')
        question = SurveyQuestion.objects.filter(id=question_id, survey=survey).first()
        if question:
            SurveyAnswer.objects.create(
                response=response,
                question=question,
                answer_text=ans.get('text', ''),
                answer_rating=ans.get('rating'),
            )

    return JsonResponse({'status': 'ok'})


# ═══════════════════════════════════════════════════════════
# FEATURE 1: AI AUTO-REPLY BOT
# ═══════════════════════════════════════════════════════════

@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_bot_config_view(request):
    """Configure AI auto-reply bot."""
    org = get_user_org(request.user)
    config, _ = AIBotConfig.objects.get_or_create(organization=org)

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'update')

        if action == 'update':
            config.is_enabled = data.get('is_enabled', config.is_enabled)
            config.bot_name = data.get('bot_name', config.bot_name)
            config.greeting_message = data.get('greeting_message', config.greeting_message)
            config.fallback_message = data.get('fallback_message', config.fallback_message)
            config.handoff_keywords = data.get('handoff_keywords', config.handoff_keywords)
            config.max_auto_replies = int(data.get('max_auto_replies', config.max_auto_replies))
            config.response_delay_seconds = int(data.get('response_delay_seconds', config.response_delay_seconds))
            config.save()
            _log_activity(org, request.user, 'ai_bot.updated', 'Updated AI bot configuration')
            return JsonResponse({'status': 'ok'})

        elif action == 'add_knowledge':
            AIBotKnowledge.objects.create(
                organization=org,
                question=data.get('question', ''),
                answer=data.get('answer', ''),
                keywords=data.get('keywords', ''),
                priority=int(data.get('priority', 0)),
            )
            return JsonResponse({'status': 'ok'})

        elif action == 'delete_knowledge':
            AIBotKnowledge.objects.filter(id=data.get('knowledge_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

    knowledge = AIBotKnowledge.objects.filter(organization=org)
    return render(request, 'dashboard/ai_bot_config.html', {
        'config': config,
        'knowledge': knowledge,
    })


# ═══════════════════════════════════════════════════════════
# FEATURE 2: CHATBOT FLOW BUILDER
# ═══════════════════════════════════════════════════════════

@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def chatbot_flows_view(request):
    """Manage chatbot flows."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            flow = ChatbotFlow.objects.create(
                organization=org,
                name=data.get('name', 'New Flow'),
                description=data.get('description', ''),
                trigger_type=data.get('trigger_type', 'greeting'),
                trigger_value=data.get('trigger_value', ''),
                flow_data=data.get('flow_data', {
                    'nodes': [
                        {'id': 'start', 'type': 'message', 'text': 'Hello! How can I help you?', 'next': []},
                    ]
                }),
            )
            _log_activity(org, request.user, 'flow.created', f'Created chatbot flow: {flow.name}')
            return JsonResponse({'status': 'ok', 'id': flow.id})

        elif action == 'save':
            flow_id = data.get('flow_id')
            flow = get_object_or_404(ChatbotFlow, id=flow_id, organization=org)
            flow.name = data.get('name', flow.name)
            flow.description = data.get('description', flow.description)
            flow.trigger_type = data.get('trigger_type', flow.trigger_type)
            flow.trigger_value = data.get('trigger_value', flow.trigger_value)
            flow.flow_data = data.get('flow_data', flow.flow_data)
            flow.save()
            return JsonResponse({'status': 'ok'})

        elif action == 'delete':
            ChatbotFlow.objects.filter(id=data.get('flow_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'toggle':
            flow = get_object_or_404(ChatbotFlow, id=data.get('flow_id'), organization=org)
            flow.is_active = not flow.is_active
            flow.save(update_fields=['is_active'])
            return JsonResponse({'status': 'ok', 'is_active': flow.is_active})

    flows = ChatbotFlow.objects.filter(organization=org)
    return render(request, 'dashboard/chatbot_flows.html', {'flows': flows})


@login_required
def chatbot_flow_editor(request, flow_id):
    """Visual chatbot flow editor."""
    org = get_user_org(request.user)
    flow = get_object_or_404(ChatbotFlow, id=flow_id, organization=org)
    return render(request, 'dashboard/chatbot_flow_editor.html', {'flow': flow})


# ═══════════════════════════════════════════════════════════
# FEATURE 3: KNOWLEDGE BASE
# ═══════════════════════════════════════════════════════════

@login_required
def kb_manage_view(request):
    """Manage knowledge base categories and articles."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', '')

        if action == 'create_category':
            from django.utils.text import slugify
            name = data.get('name', '').strip()
            slug = slugify(name)
            if not name:
                return JsonResponse({'error': 'Name required'}, status=400)
            cat, created = KBCategory.objects.get_or_create(
                organization=org, slug=slug,
                defaults={'name': name, 'description': data.get('description', ''), 'icon': data.get('icon', 'fas fa-folder')}
            )
            return JsonResponse({'status': 'ok', 'id': cat.id})

        elif action == 'create_article':
            from django.utils.text import slugify
            title = data.get('title', '').strip()
            slug = slugify(title)
            cat_id = data.get('category_id')
            cat = get_object_or_404(KBCategory, id=cat_id, organization=org)
            article = KBArticle.objects.create(
                organization=org, category=cat, title=title, slug=slug,
                content=data.get('content', ''), author=request.user,
            )
            _log_activity(org, request.user, 'kb.article_created', f'Created KB article: {title}')
            return JsonResponse({'status': 'ok', 'id': article.id})

        elif action == 'update_article':
            article = get_object_or_404(KBArticle, id=data.get('article_id'), organization=org)
            article.title = data.get('title', article.title)
            article.content = data.get('content', article.content)
            article.is_published = data.get('is_published', article.is_published)
            article.save()
            return JsonResponse({'status': 'ok'})

        elif action == 'delete_article':
            KBArticle.objects.filter(id=data.get('article_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'delete_category':
            KBCategory.objects.filter(id=data.get('category_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

    from django.db.models import Sum
    categories = KBCategory.objects.filter(organization=org).prefetch_related('articles')
    articles = KBArticle.objects.filter(organization=org).select_related('category', 'author').order_by('-updated_at')
    total_views = articles.aggregate(total=Sum('views_count'))['total'] or 0
    return render(request, 'dashboard/kb_manage.html', {
        'categories': categories,
        'articles': articles,
        'total_views': total_views,
    })


def kb_public_view(request, org_slug):
    """Public-facing knowledge base."""
    from tracker.core.models import Organization
    org = get_object_or_404(Organization, slug=org_slug)
    search = request.GET.get('q', '').strip()
    categories = KBCategory.objects.filter(organization=org, is_published=True).prefetch_related('articles')
    articles = KBArticle.objects.filter(organization=org, is_published=True)
    if search:
        articles = articles.filter(Q(title__icontains=search) | Q(content__icontains=search))
    return render(request, 'dashboard/kb_public.html', {
        'org': org,
        'categories': categories,
        'articles': articles,
        'search': search,
    })


def kb_article_view(request, org_slug, article_slug):
    """View a single KB article."""
    from tracker.core.models import Organization
    org = get_object_or_404(Organization, slug=org_slug)
    article = get_object_or_404(KBArticle, slug=article_slug, organization=org, is_published=True)
    article.views_count += 1
    article.save(update_fields=['views_count'])

    related = KBArticle.objects.filter(
        category=article.category, is_published=True, organization=org
    ).exclude(id=article.id)[:5]

    return render(request, 'dashboard/kb_article.html', {
        'org': org,
        'article': article,
        'related': related,
    })


@csrf_exempt
def kb_article_feedback(request, article_id):
    """Track helpful/not helpful feedback on KB articles."""
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        article = get_object_or_404(KBArticle, id=article_id)
        if data.get('helpful'):
            article.helpful_yes += 1
        else:
            article.helpful_no += 1
        article.save(update_fields=['helpful_yes', 'helpful_no'])
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


# ═══════════════════════════════════════════════════════════
# FEATURE 4: WHATSAPP INTEGRATION
# ═══════════════════════════════════════════════════════════

@login_required
@requires_feature('api_access', plan_label='Enterprise')
def whatsapp_config_view(request):
    """Configure WhatsApp Business API integration."""
    org = get_user_org(request.user)
    config, _ = WhatsAppConfig.objects.get_or_create(organization=org)

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        config.is_enabled = data.get('is_enabled', config.is_enabled)
        config.phone_number_id = data.get('phone_number_id', config.phone_number_id)
        # access_token round-trips through `_plain` so it lands encrypted.
        # Treat empty string as "no change" (form re-submit won't wipe the
        # stored token). To explicitly clear, send `null`.
        new_token = data.get('access_token')
        if new_token:
            config.access_token_plain = new_token
        elif new_token is None:
            config.access_token_plain = ''
        # verify_token stays plaintext (used as a Meta-handshake lookup key).
        config.verify_token = data.get('verify_token', config.verify_token)
        config.save()
        _log_activity(org, request.user, 'whatsapp.updated', 'Updated WhatsApp configuration')
        return JsonResponse({'status': 'ok'})

    messages_list = WhatsAppMessage.objects.filter(organization=org)[:50]
    return render(request, 'dashboard/whatsapp_config.html', {
        'config': config,
        'messages': messages_list,
    })


@csrf_exempt
def whatsapp_webhook(request):
    """WhatsApp webhook for receiving messages."""
    if request.method == 'GET':
        # Verification challenge
        mode = request.GET.get('hub.mode', '')
        token = request.GET.get('hub.verify_token', '')
        challenge = request.GET.get('hub.challenge', '')
        # Find matching config
        config = WhatsAppConfig.objects.filter(verify_token=token, is_enabled=True).first()
        if mode == 'subscribe' and config:
            return HttpResponse(challenge, content_type='text/plain')
        return HttpResponse('Forbidden', status=403)

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        # Process incoming WhatsApp messages
        for entry in data.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                for msg in value.get('messages', []):
                    phone = msg.get('from', '')
                    wa_id = msg.get('id', '')
                    text = msg.get('text', {}).get('body', '')
                    contact_name = ''
                    for contact in value.get('contacts', []):
                        if contact.get('wa_id') == phone:
                            contact_name = contact.get('profile', {}).get('name', '')

                    # Find org by phone number ID
                    metadata = value.get('metadata', {})
                    phone_number_id = metadata.get('phone_number_id', '')
                    config = WhatsAppConfig.objects.filter(
                        phone_number_id=phone_number_id, is_enabled=True
                    ).first()
                    if config and not WhatsAppMessage.objects.filter(wa_message_id=wa_id).exists():
                        WhatsAppMessage.objects.create(
                            organization=config.organization,
                            wa_message_id=wa_id,
                            phone_number=phone,
                            contact_name=contact_name,
                            direction='inbound',
                            content=text,
                        )

        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'GET or POST required'}, status=405)


# ═══════════════════════════════════════════════════════════
# FEATURE 5: VISITOR SEGMENTATION
# ═══════════════════════════════════════════════════════════

@login_required
def visitor_segments_view(request):
    """Manage visitor segments."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            segment = VisitorSegment.objects.create(
                organization=org,
                name=data.get('name', 'New Segment'),
                description=data.get('description', ''),
                color=data.get('color', '#6366f1'),
                conditions=data.get('conditions', []),
            )
            _log_activity(org, request.user, 'segment.created', f'Created segment: {segment.name}')
            return JsonResponse({'status': 'ok', 'id': segment.id})

        elif action == 'update':
            segment = get_object_or_404(VisitorSegment, id=data.get('segment_id'), organization=org)
            segment.name = data.get('name', segment.name)
            segment.description = data.get('description', segment.description)
            segment.color = data.get('color', segment.color)
            segment.conditions = data.get('conditions', segment.conditions)
            segment.save()
            return JsonResponse({'status': 'ok'})

        elif action == 'delete':
            VisitorSegment.objects.filter(id=data.get('segment_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'preview':
            segment = get_object_or_404(VisitorSegment, id=data.get('segment_id'), organization=org)
            count = segment.visitor_count
            return JsonResponse({'status': 'ok', 'count': count})

    segments = VisitorSegment.objects.filter(organization=org)
    return render(request, 'dashboard/visitor_segments.html', {
        'segments': segments,
    })


# ═══════════════════════════════════════════════════════════
# GOOGLE ANALYTICS FEATURES — ADVANCED ANALYTICS
# ═══════════════════════════════════════════════════════════

def _parse_date_range(request):
    """Parse date range from request, default to last 7 days."""
    from datetime import datetime
    now = timezone.now()
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()
    period = request.GET.get('period', '7d').strip()

    if date_from and date_to:
        try:
            start = timezone.make_aware(datetime.strptime(date_from, '%Y-%m-%d'))
            end = timezone.make_aware(datetime.strptime(date_to, '%Y-%m-%d').replace(hour=23, minute=59, second=59))
            return start, end, date_from, date_to, 'custom'
        except ValueError:
            pass

    period_map = {'1d': 1, '7d': 7, '14d': 14, '30d': 30, '90d': 90}
    days = period_map.get(period, 7)
    start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now
    return start, end, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'), period


def _get_prev_period(start, end):
    """Get the equivalent previous period for comparison."""
    duration = end - start
    prev_end = start
    prev_start = prev_end - duration
    return prev_start, prev_end


@login_required
@requires_feature('advanced_analytics', plan_label='Pro')
def advanced_analytics_view(request):
    """Google Analytics-style advanced analytics dashboard."""
    org = get_user_org(request.user)
    start, end, date_from, date_to, period = _parse_date_range(request)
    prev_start, prev_end = _get_prev_period(start, end)

    ws_filter = get_website_filter(request, org)
    visitors_qs = Visitor.objects.filter(organization=org, **ws_filter)
    pageviews_qs = PageView.objects.filter(visitor__organization=org, **{k.replace('website_id', 'visitor__website_id'): v for k, v in ws_filter.items()})
    chats_qs = ChatRoom.objects.filter(organization=org, **ws_filter)

    # Current period
    cur_visitors = visitors_qs.filter(first_visit__gte=start, first_visit__lte=end)
    cur_pageviews = pageviews_qs.filter(timestamp__gte=start, timestamp__lte=end)
    cur_chats = chats_qs.filter(created_at__gte=start, created_at__lte=end)

    # Previous period (for comparison)
    prev_visitors = visitors_qs.filter(first_visit__gte=prev_start, first_visit__lte=prev_end)
    prev_pageviews = pageviews_qs.filter(timestamp__gte=prev_start, timestamp__lte=prev_end)

    total_visitors = cur_visitors.count()
    total_pageviews = cur_pageviews.count()
    prev_total_visitors = prev_visitors.count()
    prev_total_pageviews = prev_pageviews.count()

    # Bounce rate
    bounced = cur_visitors.filter(is_bounced=True).count()
    bounce_rate = round((bounced / total_visitors * 100), 1) if total_visitors > 0 else 0

    # Avg session duration
    avg_duration = cur_visitors.filter(session_duration__gt=0).aggregate(avg=Avg('session_duration'))['avg'] or 0
    avg_duration_min = int(avg_duration) // 60
    avg_duration_sec = int(avg_duration) % 60

    # Avg pages per session
    avg_pages = cur_visitors.filter(pages_per_session__gt=0).aggregate(avg=Avg('pages_per_session'))['avg'] or 0

    # New vs Returning
    new_visitors = cur_visitors.filter(total_visits=1).count()
    returning_visitors = cur_visitors.filter(total_visits__gte=2).count()

    # Period change percentages
    visitor_change = round(((total_visitors - prev_total_visitors) / max(prev_total_visitors, 1)) * 100, 1)
    pv_change = round(((total_pageviews - prev_total_pageviews) / max(prev_total_pageviews, 1)) * 100, 1)

    # Daily trend
    days_count = max((end - start).days, 1)
    daily_data = []
    for i in range(min(days_count, 60)):
        day = start + timedelta(days=i)
        day_end = day + timedelta(days=1)
        v_count = cur_visitors.filter(first_visit__gte=day, first_visit__lt=day_end).count()
        pv_count = cur_pageviews.filter(timestamp__gte=day, timestamp__lt=day_end).count()
        daily_data.append({
            'date': day.strftime('%b %d'),
            'day': day.strftime('%a'),
            'visitors': v_count,
            'pageviews': pv_count,
        })

    # Top pages
    from django.db.models.functions import Replace
    from django.db.models import Value
    top_pages = (
        cur_pageviews.values('page_title')
        .annotate(views=Count('id'), avg_time=Avg('time_spent'))
        .order_by('-views')[:15]
    )

    # Landing pages (entry pages)
    landing_pages = (
        cur_pageviews.filter(is_entry=True)
        .values('page_title')
        .annotate(entries=Count('id'))
        .order_by('-entries')[:10]
    )

    # Exit pages
    exit_pages = (
        cur_pageviews.filter(is_exit=True)
        .values('page_title')
        .annotate(exits=Count('id'))
        .order_by('-exits')[:10]
    )

    # UTM Campaign data
    utm_sources = (
        cur_visitors.exclude(utm_source='')
        .values('utm_source')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )
    utm_mediums = (
        cur_visitors.exclude(utm_medium='')
        .values('utm_medium')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )
    utm_campaigns = (
        cur_visitors.exclude(utm_campaign='')
        .values('utm_campaign')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Channel grouping
    channel_data = _get_channel_data(cur_visitors)

    # Languages
    languages = (
        cur_visitors.exclude(language='')
        .values('language')
        .annotate(count=Count('id'))
        .order_by('-count')[:10]
    )

    # Countries (for map)
    country_data = list(
        cur_visitors.exclude(country='')
        .values('country')
        .annotate(count=Count('id'))
        .order_by('-count')[:30]
    )

    # Devices / Browsers
    device_data = list(cur_visitors.values('device_type').annotate(count=Count('id')).order_by('-count'))
    browser_data = list(cur_visitors.values('browser').annotate(count=Count('id')).order_by('-count')[:8])

    # Hourly distribution
    from django.db.models.functions import ExtractHour
    hourly = list(cur_pageviews.annotate(hour=ExtractHour('timestamp')).values('hour').annotate(count=Count('id')).order_by('hour'))

    # Page load performance
    avg_load_time = cur_pageviews.filter(load_time_ms__gt=0).aggregate(avg=Avg('load_time_ms'))['avg'] or 0
    slow_pages = (
        cur_pageviews.filter(load_time_ms__gt=0)
        .values('page_title')
        .annotate(avg_load=Avg('load_time_ms'), views=Count('id'))
        .order_by('-avg_load')[:10]
    )

    # Goals
    goals = Goal.objects.filter(organization=org, is_active=True)
    goal_completions = GoalCompletion.objects.filter(
        goal__organization=org, completed_at__gte=start, completed_at__lte=end
    )
    total_conversions = goal_completions.count()
    conversion_rate = round((total_conversions / max(total_visitors, 1)) * 100, 1)
    goal_breakdown = []
    for goal in goals:
        count = goal_completions.filter(goal=goal).count()
        goal_breakdown.append({'name': goal.name, 'count': count, 'value': count * goal.monetary_value})

    # Events
    events_qs = CustomEvent.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end)
    top_events = list(events_qs.values('event_name').annotate(count=Count('id'), total_value=Sum('event_value')).order_by('-count')[:10])
    total_events = events_qs.count()

    # Cohort / Retention (weekly cohorts for last 8 weeks)
    cohort_data = _build_cohort_data(org, 8)

    # User flow (top 10 page-to-page transitions)
    user_flow = _build_user_flow(org, start, end)

    context = {
        'date_from': date_from, 'date_to': date_to, 'period': period,
        'total_visitors': total_visitors, 'total_pageviews': total_pageviews,
        'visitor_change': visitor_change, 'pv_change': pv_change,
        'bounce_rate': bounce_rate,
        'avg_duration_min': avg_duration_min, 'avg_duration_sec': avg_duration_sec,
        'avg_pages': round(avg_pages, 1),
        'new_visitors': new_visitors, 'returning_visitors': returning_visitors,
        'daily_data': daily_data,
        'top_pages': top_pages, 'landing_pages': landing_pages, 'exit_pages': exit_pages,
        'utm_sources': utm_sources, 'utm_mediums': utm_mediums, 'utm_campaigns': utm_campaigns,
        'channel_data': channel_data, 'languages': languages,
        'country_data': country_data,
        'device_data': device_data, 'browser_data': browser_data,
        'hourly': hourly,
        'avg_load_time': round(avg_load_time), 'slow_pages': slow_pages,
        'goals': goals, 'total_conversions': total_conversions,
        'conversion_rate': conversion_rate, 'goal_breakdown': goal_breakdown,
        'top_events': top_events, 'total_events': total_events,
        'cohort_data': cohort_data, 'user_flow': user_flow,
    }
    return render(request, 'dashboard/advanced_analytics.html', context)


def _get_channel_data(visitors_qs):
    """Group visitors into marketing channels."""
    channels = {'Organic Search': 0, 'Paid Search': 0, 'Social': 0, 'Email': 0, 'Referral': 0, 'Direct': 0, 'Other': 0}
    for v in visitors_qs.values('utm_medium', 'utm_source', 'referrer_source'):
        medium = (v['utm_medium'] or '').lower()
        source = (v['utm_source'] or '').lower()
        ref = v['referrer_source'] or 'Direct'
        if medium in ('cpc', 'ppc', 'paid', 'paidsearch'):
            channels['Paid Search'] += 1
        elif medium == 'email' or source == 'email':
            channels['Email'] += 1
        elif medium in ('social', 'social-media') or ref in ('Facebook', 'Twitter', 'LinkedIn', 'Instagram', 'Reddit'):
            channels['Social'] += 1
        elif medium == 'organic' or ref in ('Google', 'Bing', 'Yahoo'):
            channels['Organic Search'] += 1
        elif ref == 'Direct':
            channels['Direct'] += 1
        elif ref != 'Direct' and ref != 'Other':
            channels['Referral'] += 1
        else:
            channels['Other'] += 1
    return [{'channel': k, 'count': v} for k, v in channels.items() if v > 0]


def _build_cohort_data(org, weeks):
    """Build weekly cohort retention data."""
    now = timezone.now()
    cohorts = []
    for w in range(weeks - 1, -1, -1):
        cohort_start = (now - timedelta(weeks=w + 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        cohort_end = cohort_start + timedelta(weeks=1)
        cohort_visitors = Visitor.objects.filter(
            organization=org, first_visit__gte=cohort_start, first_visit__lt=cohort_end
        )
        total = cohort_visitors.count()
        if total == 0:
            cohorts.append({'week': cohort_start.strftime('%b %d'), 'total': 0, 'retention': []})
            continue
        retention = []
        for rw in range(min(weeks - w, 4)):
            check_start = cohort_end + timedelta(weeks=rw)
            check_end = check_start + timedelta(weeks=1)
            returned = cohort_visitors.filter(last_seen__gte=check_start, last_seen__lt=check_end).count()
            retention.append(round((returned / total) * 100))
        cohorts.append({'week': cohort_start.strftime('%b %d'), 'total': total, 'retention': retention})
    return cohorts


def _build_user_flow(org, start, end):
    """Build page-to-page flow transitions."""
    flow = {}
    visitors = Visitor.objects.filter(organization=org, first_visit__gte=start).values_list('id', flat=True)[:200]
    for vid in visitors:
        pages = list(PageView.objects.filter(visitor_id=vid, timestamp__gte=start, timestamp__lte=end).order_by('timestamp').values_list('page_title', flat=True)[:10])
        for i in range(len(pages) - 1):
            key = f"{pages[i]} → {pages[i+1]}"
            flow[key] = flow.get(key, 0) + 1
    sorted_flow = sorted(flow.items(), key=lambda x: -x[1])[:15]
    return [{'from_page': k.split(' → ')[0], 'to_page': k.split(' → ')[1], 'count': v} for k, v in sorted_flow]


# ─── Goals Management ───

@login_required
def goals_view(request):
    """Manage conversion goals."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            Goal.objects.create(
                organization=org,
                name=data.get('name', 'New Goal'),
                description=data.get('description', ''),
                goal_type=data.get('goal_type', 'pageview'),
                target_url=data.get('target_url', ''),
                target_event=data.get('target_event', ''),
                target_value=float(data.get('target_value', 0)),
                monetary_value=float(data.get('monetary_value', 0)),
            )
            return JsonResponse({'status': 'ok'})
        elif action == 'delete':
            Goal.objects.filter(id=data.get('goal_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})
        elif action == 'toggle':
            goal = get_object_or_404(Goal, id=data.get('goal_id'), organization=org)
            goal.is_active = not goal.is_active
            goal.save(update_fields=['is_active'])
            return JsonResponse({'status': 'ok', 'is_active': goal.is_active})

    goals = Goal.objects.filter(organization=org)
    now = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for g in goals:
        g.today_count = g.completions.filter(completed_at__gte=today).count()
        g.week_count = g.completions.filter(completed_at__gte=now - timedelta(days=7)).count()
    return render(request, 'dashboard/goals.html', {'goals': goals})


# ─── Custom Events ───

@csrf_exempt
def track_event_api(request):
    """Public API: Track a custom event from visitor's browser."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    org, visitor, session_key = _resolve_tracking_visitor(request, data)
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    CustomEvent.objects.create(
        organization=org, visitor=visitor,
        event_name=data.get('name', 'unnamed')[:200],
        event_category=data.get('category', '')[:200],
        event_label=data.get('label', '')[:500],
        event_value=float(data.get('value', 0)),
        page_url=data.get('page_url', '')[:500],
        metadata=data.get('metadata', {}),
    )

    # Check event-based goals
    event_goals = Goal.objects.filter(organization=org, is_active=True, goal_type='event', target_event=data.get('name', ''))
    for goal in event_goals:
        recent = GoalCompletion.objects.filter(goal=goal, visitor=visitor, completed_at__gte=timezone.now() - timedelta(minutes=30)).exists()
        if not recent:
            GoalCompletion.objects.create(goal=goal, visitor=visitor, page_url=data.get('page_url', ''))

    return JsonResponse({'status': 'ok'})


# ─── Page Performance API ───

@csrf_exempt
def track_performance_api(request):
    """Public API: Track page load performance from browser."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    page_url = data.get('url', '')
    load_time = int(data.get('load_time_ms', 0))
    time_spent = int(data.get('time_spent', 0))

    if page_url and load_time > 0:
        pv = PageView.objects.filter(
            visitor__session_key=session_key, url__contains=page_url
        ).order_by('-timestamp').first()
        if pv:
            pv.load_time_ms = load_time
            if time_spent > 0:
                pv.time_spent = time_spent
            pv.save(update_fields=['load_time_ms', 'time_spent'])

    return JsonResponse({'status': 'ok'})


# ─── Scheduled Reports ───

@login_required
@requires_feature('email_notifications', plan_label='Pro')
def scheduled_reports_view(request):
    """Manage scheduled email reports."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            ScheduledReport.objects.create(
                organization=org,
                name=data.get('name', 'Weekly Report'),
                email=data.get('email', ''),
                frequency=data.get('frequency', 'weekly'),
                include_visitors=data.get('include_visitors', True),
                include_chats=data.get('include_chats', True),
                include_goals=data.get('include_goals', True),
            )
            return JsonResponse({'status': 'ok'})
        elif action == 'delete':
            ScheduledReport.objects.filter(id=data.get('report_id'), organization=org).delete()
            return JsonResponse({'status': 'ok'})
        elif action == 'toggle':
            report = get_object_or_404(ScheduledReport, id=data.get('report_id'), organization=org)
            report.is_active = not report.is_active
            report.save(update_fields=['is_active'])
            return JsonResponse({'status': 'ok', 'is_active': report.is_active})
        elif action == 'send_now':
            report = get_object_or_404(ScheduledReport, id=data.get('report_id'), organization=org)
            _send_scheduled_report(report, org)
            return JsonResponse({'status': 'ok'})

    reports = ScheduledReport.objects.filter(organization=org)
    return render(request, 'dashboard/scheduled_reports.html', {'reports': reports})


def _send_scheduled_report(report, org):
    """Send a scheduled report email."""
    from tracker.core.email_utils import send_scheduled_report
    now = timezone.now()
    last_7 = now - timedelta(days=7)

    stats = {
        'period_label': f"{last_7.strftime('%b %d')} - {now.strftime('%b %d, %Y')}",
        'dashboard_url': '/dashboard/advanced-analytics/',
    }

    if report.include_visitors:
        stats['visitors'] = Visitor.objects.filter(organization=org, first_visit__gte=last_7).count()
        stats['online'] = Visitor.objects.filter(organization=org, last_seen__gte=now - timedelta(minutes=30)).count()

    if report.include_chats:
        chats = ChatRoom.objects.filter(organization=org, created_at__gte=last_7)
        stats['chats_total'] = chats.count()
        stats['chats_closed'] = chats.filter(status='closed').count()
        stats['avg_rating'] = chats.filter(rating__isnull=False).aggregate(a=Avg('rating'))['a']

    if report.include_goals:
        stats['goal_completions'] = GoalCompletion.objects.filter(goal__organization=org, completed_at__gte=last_7).count()

    try:
        send_scheduled_report(report, org, stats)
        report.last_sent = now
        report.save(update_fields=['last_sent'])
    except Exception:
        logger.exception('Failed to send scheduled report id=%s to %s', report.id, report.email)


# ═══════════════════════════════════════════════════════════
# MICROSOFT CLARITY FEATURES
# ═══════════════════════════════════════════════════════════

# ─── Tracking APIs (called from visitor's browser JS) ───

def _resolve_tracking_visitor(request, data):
    """Resolve org + visitor for tracking APIs with session_key fallback.

    Prefers the body session_key (sent by the widget from localStorage) over
    the Django session cookie, because cross-origin requests often lack cookies
    and even same-origin the cookie may belong to a logged-in admin session
    rather than the widget visitor.
    """
    from tracker.core.views import _get_org_from_request

    org = _get_org_from_request(request)
    body_session_key = (data.get('session_key') or '').strip()
    cookie_session_key = request.session.session_key or ''

    # Prefer body key (widget localStorage), fall back to cookie
    session_key = body_session_key or cookie_session_key

    if not session_key:
        return org, None, ''

    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()

    # If body key didn't match, try cookie key as fallback
    if not visitor and body_session_key and cookie_session_key and body_session_key != cookie_session_key:
        visitor = Visitor.objects.filter(session_key=cookie_session_key, organization=org).first()
        if visitor:
            session_key = cookie_session_key

    return org, visitor, session_key


@csrf_exempt
def track_clicks_api(request):
    """Batch receive click data for heatmaps."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    org, visitor, session_key = _resolve_tracking_visitor(request, data)
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    clicks = data.get('clicks', [])
    recording_id = data.get('session_id', '')
    recording = SessionRecording.objects.filter(session_id=recording_id).first() if recording_id else None

    objs = []
    rage_count = 0
    dead_count = 0
    for c in clicks[:50]:  # Max 50 clicks per batch
        click_type = c.get('type', 'click')
        if click_type == 'rage':
            rage_count += 1
        elif click_type == 'dead':
            dead_count += 1
        objs.append(ClickData(
            organization=org, visitor=visitor, recording=recording,
            page_url=c.get('url', '')[:500], page_path=c.get('path', '')[:500],
            x_percent=float(c.get('x_pct', 0)), y_percent=float(c.get('y_pct', 0)),
            x_px=int(c.get('x_px', 0)), y_px=int(c.get('y_px', 0)),
            element_tag=c.get('tag', '')[:50], element_text=c.get('text', '')[:200],
            element_selector=c.get('selector', '')[:500],
            click_type=click_type,
            device_type=c.get('device', 'desktop')[:20],
            viewport_width=int(c.get('vw', 0)), viewport_height=int(c.get('vh', 0)),
        ))
    if objs:
        ClickData.objects.bulk_create(objs)

    # Create frustration signals
    if rage_count > 0:
        FrustrationSignal.objects.create(
            organization=org, visitor=visitor, recording=recording,
            signal_type='rage_click', page_url=clicks[0].get('url', '') if clicks else '',
            page_path=clicks[0].get('path', '') if clicks else '',
            details={'count': rage_count},
        )
    if dead_count > 0:
        FrustrationSignal.objects.create(
            organization=org, visitor=visitor, recording=recording,
            signal_type='dead_click', page_url=clicks[0].get('url', '') if clicks else '',
            page_path=clicks[0].get('path', '') if clicks else '',
            details={'count': dead_count},
        )

    return JsonResponse({'status': 'ok', 'saved': len(objs)})


@csrf_exempt
def track_scroll_api(request):
    """Track scroll depth."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    org, visitor, session_key = _resolve_tracking_visitor(request, data)
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    ScrollData.objects.create(
        organization=org, visitor=visitor,
        page_url=data.get('url', '')[:500], page_path=data.get('path', '')[:500],
        max_scroll_percent=min(100, int(data.get('scroll_pct', 0))),
        page_height=int(data.get('page_height', 0)),
        viewport_height=int(data.get('viewport_height', 0)),
        device_type=data.get('device', 'desktop')[:20],
    )
    return JsonResponse({'status': 'ok'})


@csrf_exempt
def track_js_error_api(request):
    """Track JavaScript errors."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    org, visitor, session_key = _resolve_tracking_visitor(request, data)
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    recording_id = data.get('session_id', '')
    recording = SessionRecording.objects.filter(session_id=recording_id).first() if recording_id else None

    JSError.objects.create(
        organization=org, visitor=visitor, recording=recording,
        error_message=data.get('message', '')[:1000],
        error_source=data.get('source', '')[:500],
        error_line=int(data.get('line', 0)),
        error_col=int(data.get('col', 0)),
        stack_trace=data.get('stack', '')[:2000],
        page_url=data.get('url', '')[:500],
        browser=visitor.browser,
    )
    return JsonResponse({'status': 'ok'})


@csrf_exempt
def track_session_api(request):
    """Create/update session recording data."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    org, visitor, session_key = _resolve_tracking_visitor(request, data)
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    import uuid as _uuid
    session_id = data.get('session_id', '') or _uuid.uuid4().hex[:16]
    action = data.get('action', 'create')

    if action == 'create':
        rec, created = SessionRecording.objects.get_or_create(
            session_id=session_id,
            defaults={
                'organization': org, 'visitor': visitor,
                'website': visitor.website,
                'start_url': data.get('url', '')[:500],
                'device_type': visitor.device_type,
                'screen_width': int(data.get('screen_w', 0)),
                'screen_height': int(data.get('screen_h', 0)),
            }
        )
        return JsonResponse({'status': 'ok', 'session_id': session_id})

    elif action == 'append':
        rec = SessionRecording.objects.filter(session_id=session_id).first()
        if rec:
            if not rec.website_id and visitor.website_id:
                rec.website_id = visitor.website_id
            events = rec.events_data or []
            new_events = data.get('events', [])
            events.extend(new_events[:100])  # Max 100 events per batch
            rec.events_data = events[-2000:]  # Keep last 2000 events
            rec.duration = int(data.get('duration', rec.duration))
            rec.pages_visited = int(data.get('pages', rec.pages_visited))
            rec.has_rage_clicks = data.get('has_rage', rec.has_rage_clicks)
            rec.has_dead_clicks = data.get('has_dead', rec.has_dead_clicks)
            rec.has_quick_back = data.get('has_quick_back', rec.has_quick_back)
            rec.has_errors = data.get('has_errors', rec.has_errors)
            # Calculate frustration score
            score = 0
            if rec.has_rage_clicks:
                score += 30
            if rec.has_dead_clicks:
                score += 25
            if rec.has_quick_back:
                score += 20
            if rec.has_errors:
                score += 25
            rec.frustration_score = min(100, score)
            rec.save()
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'Invalid action'}, status=400)


@csrf_exempt
def track_frustration_api(request):
    """Track frustration signals (quick-back, excessive scroll)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    org, visitor, session_key = _resolve_tracking_visitor(request, data)
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)
    if not visitor:
        return JsonResponse({'error': 'Visitor not found'}, status=404)

    FrustrationSignal.objects.create(
        organization=org, visitor=visitor,
        signal_type=data.get('type', 'rage_click')[:20],
        page_url=data.get('url', '')[:500],
        page_path=data.get('path', '')[:500],
        element_selector=data.get('selector', '')[:500],
        element_text=data.get('text', '')[:200],
        details=data.get('details', {}),
    )
    return JsonResponse({'status': 'ok'})


# ─── Dashboard Views ───

@login_required
def heatmaps_view(request):
    """Click & scroll heatmaps dashboard."""
    org = get_user_org(request.user)
    start, end, date_from, date_to, period = _parse_date_range(request)
    page_filter = request.GET.get('page', '').strip()
    device_filter = request.GET.get('device', 'all').strip()

    ws_filter = get_website_filter(request, org)
    clicks_qs = ClickData.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end, **ws_filter)
    scroll_qs = ScrollData.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end, **ws_filter)

    if page_filter:
        clicks_qs = clicks_qs.filter(page_path=page_filter)
        scroll_qs = scroll_qs.filter(page_path=page_filter)
    if device_filter != 'all':
        clicks_qs = clicks_qs.filter(device_type=device_filter)
        scroll_qs = scroll_qs.filter(device_type=device_filter)

    # Click heatmap data (as JSON for rendering)
    click_points = list(clicks_qs.values('x_percent', 'y_percent', 'click_type')[:500])

    # Scroll depth distribution
    scroll_depths = list(scroll_qs.values('max_scroll_percent').annotate(count=Count('id')).order_by('max_scroll_percent'))
    avg_scroll = scroll_qs.aggregate(avg=Avg('max_scroll_percent'))['avg'] or 0

    # Top clicked pages
    top_click_pages = list(clicks_qs.values('page_path').annotate(
        total=Count('id'),
        rage=Count('id', filter=Q(click_type='rage')),
        dead=Count('id', filter=Q(click_type='dead')),
    ).order_by('-total')[:10])

    # All page paths for filter dropdown
    all_pages = list(clicks_qs.values_list('page_path', flat=True).distinct()[:50])

    # Click type breakdown
    total_clicks = clicks_qs.count()
    rage_clicks = clicks_qs.filter(click_type='rage').count()
    dead_clicks = clicks_qs.filter(click_type='dead').count()
    normal_clicks = total_clicks - rage_clicks - dead_clicks

    # Most clicked elements
    top_elements = list(clicks_qs.exclude(element_text='').values('element_tag', 'element_text').annotate(count=Count('id')).order_by('-count')[:10])

    return render(request, 'dashboard/heatmaps.html', {
        'date_from': date_from, 'date_to': date_to, 'period': period,
        'page_filter': page_filter, 'device_filter': device_filter,
        'click_points': json.dumps(click_points),
        'scroll_depths': scroll_depths, 'avg_scroll': round(avg_scroll),
        'top_click_pages': top_click_pages, 'all_pages': all_pages,
        'total_clicks': total_clicks, 'rage_clicks': rage_clicks,
        'dead_clicks': dead_clicks, 'normal_clicks': normal_clicks,
        'top_elements': top_elements,
    })


@login_required
def session_recordings_view(request):
    """List session recordings."""
    org = get_user_org(request.user)
    ws_filter = get_website_filter(request, org)
    recordings = SessionRecording.objects.filter(organization=org, **ws_filter).select_related('visitor')

    # Filters
    device = request.GET.get('device', '').strip()
    has_rage = request.GET.get('rage', '').strip()
    has_dead = request.GET.get('dead', '').strip()
    has_errors = request.GET.get('errors', '').strip()
    min_duration = request.GET.get('min_dur', '').strip()

    if device:
        recordings = recordings.filter(device_type=device)
    if has_rage == '1':
        recordings = recordings.filter(has_rage_clicks=True)
    if has_dead == '1':
        recordings = recordings.filter(has_dead_clicks=True)
    if has_errors == '1':
        recordings = recordings.filter(has_errors=True)
    if min_duration:
        recordings = recordings.filter(duration__gte=int(min_duration))

    recordings = recordings[:50]

    return render(request, 'dashboard/session_recordings.html', {
        'recordings': recordings,
        'device': device, 'has_rage': has_rage, 'has_dead': has_dead,
        'has_errors': has_errors, 'min_duration': min_duration,
    })


@login_required
def session_replay_view(request, session_id):
    """Replay a single session recording."""
    org = get_user_org(request.user)
    recording = get_object_or_404(SessionRecording, session_id=session_id, organization=org)
    clicks = recording.clicks.all()[:200]
    errors = recording.errors.all()[:20]
    frustrations = recording.frustration_signals.all()[:20]
    return render(request, 'dashboard/session_replay.html', {
        'recording': recording,
        'clicks': clicks, 'errors': errors, 'frustrations': frustrations,
        'events_json': json.dumps(recording.events_data),
    })


@login_required
def js_errors_view(request):
    """JavaScript error tracking dashboard."""
    org = get_user_org(request.user)
    start, end, date_from, date_to, period = _parse_date_range(request)
    ws_filter = get_website_filter(request, org)
    errors = JSError.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end, **ws_filter)

    # Group by error message
    error_groups = list(errors.values('error_message').annotate(
        count=Count('id'),
        browsers=Count('browser', distinct=True),
        last_seen=Max('timestamp'),
    ).order_by('-count')[:20])

    total_errors = errors.count()
    unique_errors = errors.values('error_message').distinct().count()
    affected_visitors = errors.values('visitor').distinct().count()

    return render(request, 'dashboard/js_errors.html', {
        'error_groups': error_groups,
        'total_errors': total_errors, 'unique_errors': unique_errors,
        'affected_visitors': affected_visitors,
        'date_from': date_from, 'date_to': date_to, 'period': period,
        'recent_errors': errors[:20],
    })


@login_required
def frustration_dashboard_view(request):
    """Frustration signals overview — Clarity-style insights."""
    org = get_user_org(request.user)
    start, end, date_from, date_to, period = _parse_date_range(request)
    ws_filter = get_website_filter(request, org)
    signals = FrustrationSignal.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end, **ws_filter)

    # Signal breakdown
    signal_counts = {}
    for s_type, s_label in FrustrationSignal.SIGNAL_TYPES:
        signal_counts[s_label] = signals.filter(signal_type=s_type).count()

    total_signals = signals.count()

    # Most frustrated pages
    frustrated_pages = list(signals.values('page_path').annotate(
        count=Count('id'),
        rage=Count('id', filter=Q(signal_type='rage_click')),
        dead=Count('id', filter=Q(signal_type='dead_click')),
    ).order_by('-count')[:10])

    # Frustrated sessions
    frustrated_recordings = SessionRecording.objects.filter(
        organization=org, frustration_score__gt=0, created_at__gte=start, **ws_filter
    ).order_by('-frustration_score')[:10]

    # Per-page insights
    page_insights = PageInsight.objects.filter(organization=org, **ws_filter).order_by('-frustration_score')[:15]

    return render(request, 'dashboard/frustration_dashboard.html', {
        'signal_counts': signal_counts, 'total_signals': total_signals,
        'frustrated_pages': frustrated_pages,
        'frustrated_recordings': frustrated_recordings,
        'page_insights': page_insights,
        'date_from': date_from, 'date_to': date_to, 'period': period,
    })


@login_required
def tour_guide_view(request):
    """Interactive dashboard tour guide page."""
    return render(request, 'dashboard/tour_guide.html')


# ═══════════════════════════════════════════════════════════
# BILLING & SUBSCRIPTION
# ═══════════════════════════════════════════════════════════

@login_required
def billing_view(request):
    """Billing page — plan selection, payment history, usage. Owner only."""
    from tracker.core.models import Subscription, PaymentHistory
    from django.db.models import Sum
    profile = getattr(request.user, 'agent_profile', None)
    if not request.user.is_superuser and (not profile or profile.role not in ('owner', 'admin')):
        return HttpResponse("Forbidden — owners only.", status=403)
    org = get_user_org(request.user)
    sub, _ = Subscription.objects.get_or_create(organization=org, defaults={'plan': 'free', 'status': 'active'})
    payments = PaymentHistory.objects.filter(organization=org)[:20]

    # Usage stats
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_visitors = Visitor.objects.filter(organization=org, first_visit__gte=month_start).count()
    agent_count = AgentProfile.objects.filter(organization=org).count()
    total_chats = ChatRoom.objects.filter(organization=org, created_at__gte=month_start).count()
    limits = sub.plan_limits

    # Billing summary
    total_spent = payments.aggregate(total=Sum('amount'))['total'] or 0
    last_payment = payments.first()

    # Next payment date
    next_payment = None
    if sub.current_period_end and sub.plan != 'free' and not sub.cancel_at_period_end:
        next_payment = sub.current_period_end
    elif sub.plan != 'free' and last_payment:
        fallback_days = 365 if sub.billing_interval == 'year' else 30
        next_payment = last_payment.created_at + timedelta(days=fallback_days)

    # Plan price
    plan_price = {'free': 0, 'pro': 19, 'enterprise': 79}.get(sub.plan, 0)

    # Auto-upgrade prompt
    upgrade_plan = request.GET.get('upgrade', '')

    return render(request, 'dashboard/billing.html', {
        'sub': sub,
        'payments': payments,
        'monthly_visitors': monthly_visitors,
        'agent_count': agent_count,
        'total_chats': total_chats,
        'limits': limits,
        'total_spent': total_spent,
        'last_payment': last_payment,
        'next_payment': next_payment,
        'plan_price': plan_price,
        'upgrade_plan': upgrade_plan,
    })


@login_required
def create_checkout_session(request):
    """Process card payment and activate plan (no Stripe — demo checkout)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    data = _parse_json_body(request) or {}
    plan = data.get('plan', 'pro')
    interval = data.get('interval', 'month')
    coupon_code = data.get('coupon', '').strip()
    card_number = data.get('card_number', '').replace(' ', '')
    card_expiry = data.get('card_expiry', '').strip()
    card_cvc = data.get('card_cvc', '').strip()
    card_name = data.get('card_name', '').strip()

    if interval not in ('month', 'year'):
        interval = 'month'

    PRICES = {
        'pro': {'month': 19, 'year': 190},
        'enterprise': {'month': 79, 'year': 790},
    }

    if plan not in PRICES:
        return JsonResponse({'error': 'Invalid plan'}, status=400)

    # Basic card validation
    if not card_number or len(card_number) < 13 or not card_number.isdigit():
        return JsonResponse({'error': 'Invalid card number'}, status=400)
    if not card_expiry or '/' not in card_expiry:
        return JsonResponse({'error': 'Invalid expiry date (MM/YY)'}, status=400)
    if not card_cvc or len(card_cvc) < 3:
        return JsonResponse({'error': 'Invalid CVC'}, status=400)

    amount = PRICES[plan][interval]
    interval_label = 'Yearly' if interval == 'year' else 'Monthly'

    # Apply coupon
    from tracker.core.models import Coupon
    discount = 0
    applied_coupon = None
    if coupon_code:
        coupon = Coupon.objects.filter(code__iexact=coupon_code, is_active=True).first()
        if not coupon:
            return JsonResponse({'error': 'Invalid coupon code'}, status=400)
        if not coupon.is_valid:
            return JsonResponse({'error': 'Coupon has expired or reached usage limit'}, status=400)
        if not coupon.applies_to(plan, interval):
            return JsonResponse({'error': f'This coupon is not valid for {plan.title()} {interval_label}'}, status=400)
        discount = coupon.calculate_discount(amount)
        applied_coupon = coupon

    final_amount = max(round(amount - discount, 2), 0)

    org = get_user_org(request.user)
    from tracker.core.models import Subscription, PaymentHistory
    sub, _ = Subscription.objects.get_or_create(organization=org, defaults={'plan': 'free'})

    # Activate plan
    now = timezone.now()
    if interval == 'year':
        period_end = now + timedelta(days=365)
    else:
        period_end = now + timedelta(days=30)

    sub.plan = plan
    sub.status = 'active'
    sub.billing_interval = interval
    sub.current_period_start = now
    sub.current_period_end = period_end
    sub.cancel_at_period_end = False
    sub.pending_plan = ''
    sub.pending_interval = ''
    if applied_coupon:
        sub.coupon_applied = applied_coupon
        sub.discount_percent = int(applied_coupon.discount_value) if applied_coupon.discount_type == 'percent' else 0
        applied_coupon.times_used += 1
        applied_coupon.save(update_fields=['times_used'])
    sub.save()

    # Record payment
    last4 = card_number[-4:]
    desc = f'Upgraded to {plan.title()} plan ({interval_label})'
    if coupon_code and discount > 0:
        desc += f' — Coupon: {coupon_code} (${discount:.2f} off)'

    PaymentHistory.objects.create(
        organization=org,
        amount=final_amount,
        plan=plan,
        stripe_payment_id=f'card_****{last4}_{now.strftime("%Y%m%d%H%M%S")}',
        description=desc,
    )

    _log_activity(org, request.user, 'plan.upgraded', f'Upgraded to {plan.title()} plan')

    return JsonResponse({'status': 'ok', 'plan': plan})


@login_required
def validate_coupon(request):
    """Validate a coupon code and return discount info."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}
    code = data.get('code', '').strip()
    plan = data.get('plan', 'pro')
    interval = data.get('interval', 'month')

    from tracker.core.models import Coupon
    coupon = Coupon.objects.filter(code__iexact=code, is_active=True).first()
    if not coupon:
        return JsonResponse({'valid': False, 'error': 'Invalid coupon code'})
    if not coupon.is_valid:
        return JsonResponse({'valid': False, 'error': 'Coupon expired or usage limit reached'})
    if not coupon.applies_to(plan, interval):
        return JsonResponse({'valid': False, 'error': f'Not valid for {plan.title()} {interval}'})

    PRICES = {'pro': {'month': 19, 'year': 190}, 'enterprise': {'month': 79, 'year': 790}}
    original = PRICES.get(plan, {}).get(interval, 0)
    discount = coupon.calculate_discount(original)

    return JsonResponse({
        'valid': True,
        'code': coupon.code,
        'name': coupon.name or coupon.code,
        'discount_type': coupon.discount_type,
        'discount_value': float(coupon.discount_value),
        'discount_amount': discount,
        'final_price': round(original - discount, 2),
        'original_price': original,
    })


@login_required
def manage_coupons_view(request):
    """Admin: manage coupons."""
    org = get_user_org(request.user)
    # Only owner can manage coupons
    profile = getattr(request.user, 'agent_profile', None)
    if not profile or profile.role != 'owner':
        return HttpResponse('Owner access required', status=403)

    from tracker.core.models import Coupon

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            code = data.get('code', '').strip().upper()
            if not code:
                return JsonResponse({'error': 'Code required'}, status=400)
            if Coupon.objects.filter(code=code).exists():
                return JsonResponse({'error': 'Code already exists'}, status=400)
            coupon = Coupon.objects.create(
                code=code,
                name=data.get('name', ''),
                discount_type=data.get('discount_type', 'percent'),
                discount_value=float(data.get('discount_value', 10)),
                applicable_plans=data.get('applicable_plans', 'pro,enterprise'),
                applicable_intervals=data.get('applicable_intervals', 'month,year'),
                max_uses=int(data.get('max_uses', 0)),
            )
            if data.get('valid_until'):
                from datetime import datetime
                coupon.valid_until = timezone.make_aware(datetime.strptime(data['valid_until'], '%Y-%m-%d'))
                coupon.save(update_fields=['valid_until'])
            return JsonResponse({'status': 'ok', 'id': coupon.id})

        elif action == 'delete':
            Coupon.objects.filter(id=data.get('coupon_id')).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'toggle':
            c = Coupon.objects.filter(id=data.get('coupon_id')).first()
            if c:
                c.is_active = not c.is_active
                c.save(update_fields=['is_active'])
                return JsonResponse({'status': 'ok', 'is_active': c.is_active})

    coupons = Coupon.objects.order_by('-id')[:200]
    return render(request, 'dashboard/coupons.html', {'coupons': coupons})


@login_required
def billing_success(request):
    """Show payment success page."""
    org = get_user_org(request.user)
    from tracker.core.models import Subscription
    sub = Subscription.objects.filter(organization=org).first()
    plan = sub.plan if sub and sub.plan != 'free' else 'pro'
    return render(request, 'dashboard/billing_success.html', {'plan': plan})


@login_required
def cancel_subscription(request):
    """Cancel subscription — downgrade to free."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    org = get_user_org(request.user)
    from tracker.core.models import Subscription
    sub = Subscription.objects.filter(organization=org).first()
    if not sub:
        return JsonResponse({'error': 'No subscription found'}, status=400)

    sub.plan = 'free'
    sub.status = 'active'
    sub.cancel_at_period_end = False
    sub.save(update_fields=['plan', 'status', 'cancel_at_period_end'])

    _log_activity(org, request.user, 'plan.cancelled', 'Subscription cancelled')
    return JsonResponse({'status': 'ok'})


# ═══════════════════════════════════════════════════════════
# SUPER ADMIN — All Organizations Overview
# ═══════════════════════════════════════════════════════════

@login_required
def super_admin_view(request):
    """Super admin: view all organizations, visitors, chats, plans."""
    if not request.user.is_superuser:
        return HttpResponse('Superuser access required', status=403)

    from tracker.core.models import Organization, Subscription
    from django.db.models import Count, Avg

    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_30_min = now - timedelta(minutes=30)

    # All orgs with stats — cap at 200 so a 10k-org instance doesn't blow up the page
    orgs = Organization.objects.all().annotate(
        total_visitors=Count('visitors', distinct=True),
        monthly_visitors=Count('visitors', filter=Q(visitors__first_visit__gte=month_start), distinct=True),
        online_visitors=Count('visitors', filter=Q(visitors__last_seen__gte=last_30_min), distinct=True),
        total_chats=Count('chat_rooms', distinct=True),
        active_chats=Count('chat_rooms', filter=Q(chat_rooms__status__in=['waiting', 'active']), distinct=True),
        total_agents=Count('agents', distinct=True),
        avg_rating=Avg('chat_rooms__rating', filter=Q(chat_rooms__rating__isnull=False)),
    ).order_by('-total_visitors')[:200]

    # Global stats
    total_orgs = Organization.objects.count()
    total_users = User.objects.count()
    total_visitors_global = Visitor.objects.count()
    total_chats_global = ChatRoom.objects.count()
    online_now = Visitor.objects.filter(last_seen__gte=last_30_min).count()

    # Plan distribution — single GROUP BY query instead of looping every Subscription row
    from django.db.models import Count as _Count
    plan_dist = dict(
        Subscription.objects.values_list('plan').annotate(c=_Count('id')).values_list('plan', 'c')
    )

    # Revenue
    from tracker.core.models import PaymentHistory, Coupon
    from django.db.models import Sum
    total_revenue = PaymentHistory.objects.aggregate(total=Sum('amount'))['total'] or 0
    monthly_revenue = PaymentHistory.objects.filter(created_at__gte=month_start).aggregate(total=Sum('amount'))['total'] or 0

    # All payments — already capped at 50 ✓
    all_payments = PaymentHistory.objects.select_related('organization').order_by('-created_at')[:50]

    # Recent signups — already capped at 10 ✓
    recent_users = User.objects.order_by('-date_joined')[:10]

    # Active coupons — cap at 100 most-recent
    coupons = Coupon.objects.order_by('-id')[:100]

    # Messages & offline stats
    total_messages = Message.objects.count()
    total_offline = OfflineMessage.objects.count()

    return render(request, 'dashboard/super_admin.html', {
        'orgs': orgs,
        'total_orgs': total_orgs,
        'total_users': total_users,
        'total_visitors_global': total_visitors_global,
        'total_chats_global': total_chats_global,
        'online_now': online_now,
        'plan_dist': plan_dist,
        'total_revenue': total_revenue,
        'monthly_revenue': monthly_revenue,
        'all_payments': all_payments,
        'recent_users': recent_users,
        'coupons': coupons,
        'total_messages': total_messages,
        'total_offline': total_offline,
    })


# ═══════ Website Management ═══════

@login_required
def set_active_website(request):
    """Set the active website filter in session (AJAX).

    Accepts any of:
      {"website_ids": [1, 2, 3]}  — multi-select (empty list = all)
      {"website_id": 1}           — single-select shortcut
      {"website_id": "all"}       — clear filter
    Old single-select callers keep working unchanged.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = _parse_json_body(request) or {}

    # Always clear the legacy single-int key so it can't shadow the list
    request.session.pop('selected_website_id', None)

    ids_raw = data.get('website_ids')
    if isinstance(ids_raw, list):
        ids = []
        for v in ids_raw:
            try:
                ids.append(int(v))
            except (TypeError, ValueError):
                continue
        if ids:
            request.session['selected_website_ids'] = ids
        else:
            request.session.pop('selected_website_ids', None)
        return JsonResponse({'status': 'ok', 'selected_count': len(ids)})

    # Single-id fallback (legacy callers + quick-switch UI)
    single = data.get('website_id')
    if single in (None, '', 'all'):
        request.session.pop('selected_website_ids', None)
        return JsonResponse({'status': 'ok', 'selected_count': 0})
    try:
        request.session['selected_website_ids'] = [int(single)]
        return JsonResponse({'status': 'ok', 'selected_count': 1})
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid website_id'}, status=400)


@login_required
def website_manage_view(request):
    """CRUD for websites - owner/admin only."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return redirect('dashboard:home')

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'add')

        if action == 'add':
            name = (data.get('name') or '').strip()
            domain = (data.get('domain') or '').strip().lower()
            if not name or not domain:
                return JsonResponse({'error': 'Name and domain are required'}, status=400)
            # Normalize domain
            domain = domain.replace('https://', '').replace('http://', '').split('/')[0].lstrip('www.')
            if Website.objects.filter(organization=org, domain=domain).exists():
                return JsonResponse({'error': 'This domain already exists'}, status=400)
            with transaction.atomic():
                ws = Website.objects.create(
                    organization=org, name=name, domain=domain,
                    widget_title=(data.get('widget_title') or '').strip(),
                    widget_color=(data.get('widget_color') or '').strip(),
                    widget_position=(data.get('widget_position') or '').strip(),
                    welcome_message=(data.get('welcome_message') or '').strip(),
                )
                # Grant all existing agents access to new website
                AgentWebsiteAccess.objects.bulk_create(
                    [AgentWebsiteAccess(agent=agent, website=ws)
                     for agent in AgentProfile.objects.filter(organization=org)],
                    ignore_conflicts=True,
                )
            return JsonResponse({
                'status': 'ok', 'id': ws.id, 'name': ws.name,
                'domain': ws.domain, 'tracking_key': ws.tracking_key,
            })

        elif action == 'edit':
            ws_id = data.get('id')
            ws = get_object_or_404(Website, id=ws_id, organization=org)
            ws.name = (data.get('name') or ws.name).strip()
            new_domain = (data.get('domain') or '').strip().lower()
            if new_domain:
                new_domain = new_domain.replace('https://', '').replace('http://', '').split('/')[0].lstrip('www.')
                if Website.objects.filter(organization=org, domain=new_domain).exclude(id=ws.id).exists():
                    return JsonResponse({'error': 'This domain already exists'}, status=400)
                ws.domain = new_domain
            # Widget customization
            if 'widget_title' in data:
                ws.widget_title = (data['widget_title'] or '').strip()
            if 'widget_color' in data:
                ws.widget_color = (data['widget_color'] or '').strip()
            if 'widget_position' in data:
                ws.widget_position = (data['widget_position'] or '').strip()
            if 'welcome_message' in data:
                ws.welcome_message = (data['welcome_message'] or '').strip()
            if 'is_active' in data:
                ws.is_active = bool(data['is_active'])
            ws.save()
            return JsonResponse({'status': 'ok'})

        return JsonResponse({'error': 'Invalid action'}, status=400)

    base_url = request.build_absolute_uri('/').rstrip('/')
    host = request.get_host().split(':')[0]
    if host not in ('localhost', '127.0.0.1') and base_url.startswith('http://'):
        base_url = 'https://' + base_url[len('http://'):]

    # Per-website stats — one annotated query instead of 6 per website (was N+1)
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_30_min = now - timedelta(minutes=30)
    last_7d = now - timedelta(days=7)

    websites = list(
        Website.objects.filter(organization=org)
        .select_related('group')
        .annotate(
            stat_visitors_total=Count('visitors', distinct=True),
            stat_visitors_today=Count('visitors', filter=Q(visitors__first_visit__gte=today_start), distinct=True),
            stat_visitors_online=Count('visitors', filter=Q(visitors__last_seen__gte=last_30_min), distinct=True),
            stat_pageviews_7d=Count('visitors__page_views', filter=Q(visitors__page_views__timestamp__gte=last_7d)),
            stat_chats_total=Count('chat_rooms', distinct=True),
            stat_chats_active=Count('chat_rooms', filter=Q(chat_rooms__status__in=['waiting', 'active']), distinct=True),
        )
    )

    return render(request, 'dashboard/website_manage.html', {
        'websites': websites,
        'base_url': base_url,
        'total_websites': len(websites),
        'org_widget_key': org.widget_key if org else '',
    })


@login_required
def website_delete(request, website_id):
    """Delete a website - owner/admin only."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return JsonResponse({'error': 'Permission denied'}, status=403)
    if request.method == 'POST':
        ws = get_object_or_404(Website, id=website_id, organization=org)
        # Don't allow deleting last website
        if Website.objects.filter(organization=org).count() <= 1:
            return JsonResponse({'error': 'Cannot delete the last website'}, status=400)
        ws.delete()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


# ═══════════════════════════════════════════════════════
# Feature: Script Installation Checker
# ═══════════════════════════════════════════════════════

@login_required
def website_verify_script(request, website_id):
    """Check if tracking script is installed on the website by fetching its homepage."""
    import urllib.request
    import ssl

    org = get_user_org(request.user)
    ws = get_object_or_404(Website, id=website_id, organization=org)

    try:
        url = f'https://{ws.domain}'
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={'User-Agent': 'LiveTrackBot/1.0 ScriptChecker'})
        resp = urllib.request.urlopen(req, timeout=10, context=ctx)
        html = resp.read(200000).decode('utf-8', errors='ignore')

        # Check for org widget_key or website tracking_key in the HTML
        found = ws.tracking_key in html or ws.organization.widget_key in html
        ws.script_verified = found
        ws.script_last_checked = timezone.now()
        ws.save(update_fields=['script_verified', 'script_last_checked'])

        return JsonResponse({
            'status': 'ok',
            'verified': found,
            'domain': ws.domain,
            'checked_at': ws.script_last_checked.isoformat(),
        })
    except Exception as e:
        ws.script_verified = False
        ws.script_last_checked = timezone.now()
        ws.save(update_fields=['script_verified', 'script_last_checked'])
        return JsonResponse({
            'status': 'ok',
            'verified': False,
            'error': str(e)[:200],
            'domain': ws.domain,
        })


# ═══════════════════════════════════════════════════════
# Feature: Per-Website Mini Dashboard
# ═══════════════════════════════════════════════════════

@login_required
def website_dashboard(request, website_id):
    """Dedicated analytics page for a single website."""
    org = get_user_org(request.user)
    ws = get_object_or_404(Website, id=website_id, organization=org)
    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_30_min = now - timedelta(minutes=30)
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)

    visitors_qs = Visitor.objects.filter(website=ws)
    pageviews_qs = PageView.objects.filter(visitor__website=ws)
    chats_qs = ChatRoom.objects.filter(website=ws)

    # Key metrics
    total_visitors = visitors_qs.count()
    online_now = visitors_qs.filter(last_seen__gte=last_30_min).count()
    today_visitors = visitors_qs.filter(first_visit__gte=today_start).count()
    week_visitors = visitors_qs.filter(first_visit__gte=last_7d).count()
    total_pageviews = pageviews_qs.count()
    week_pageviews = pageviews_qs.filter(timestamp__gte=last_7d).count()
    total_chats = chats_qs.count()
    active_chats = chats_qs.filter(status__in=['waiting', 'active']).count()

    # Bounce rate
    week_v = visitors_qs.filter(first_visit__gte=last_7d)
    bounced = week_v.filter(is_bounced=True).count()
    bounce_rate = round((bounced / week_v.count() * 100)) if week_v.count() > 0 else 0

    # Avg session duration
    avg_dur = week_v.filter(session_duration__gt=0).aggregate(avg=Avg('session_duration'))['avg'] or 0

    # Daily trend (last 14 days)
    daily_data = []
    for i in range(13, -1, -1):
        day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day + timedelta(days=1)
        daily_data.append({
            'date': day.strftime('%b %d'),
            'visitors': visitors_qs.filter(first_visit__gte=day, first_visit__lt=day_end).count(),
            'views': pageviews_qs.filter(timestamp__gte=day, timestamp__lt=day_end).count(),
        })

    # Top pages
    top_pages = list(pageviews_qs.filter(timestamp__gte=last_7d).values('url', 'page_title').annotate(
        count=Count('id'), visitors=Count('visitor', distinct=True)
    ).order_by('-count')[:10])

    # Live visitors
    live_visitors = list(visitors_qs.filter(last_seen__gte=last_30_min).values(
        'id', 'ip_address', 'browser', 'os', 'device_type', 'country', 'last_seen'
    )[:20])

    # Traffic sources
    sources = list(visitors_qs.filter(first_visit__gte=last_7d).values('referrer_source').annotate(
        count=Count('id')).order_by('-count')[:8])

    # Device breakdown
    devices = list(visitors_qs.filter(first_visit__gte=last_7d).values('device_type').annotate(
        count=Count('id')).order_by('-count'))

    # Country breakdown
    countries = list(visitors_qs.filter(first_visit__gte=last_7d).exclude(country='').values('country').annotate(
        count=Count('id')).order_by('-count')[:10])

    # Browser breakdown
    browsers = list(visitors_qs.filter(first_visit__gte=last_7d).exclude(browser='').values('browser').annotate(
        count=Count('id')).order_by('-count')[:8])

    base_url = request.build_absolute_uri('/').rstrip('/')
    host = request.get_host().split(':')[0]
    if host not in ('localhost', '127.0.0.1') and base_url.startswith('http://'):
        base_url = 'https://' + base_url[len('http://'):]

    return render(request, 'dashboard/website_dashboard.html', {
        'ws': ws,
        'total_visitors': total_visitors,
        'online_now': online_now,
        'today_visitors': today_visitors,
        'week_visitors': week_visitors,
        'total_pageviews': total_pageviews,
        'week_pageviews': week_pageviews,
        'total_chats': total_chats,
        'active_chats': active_chats,
        'bounce_rate': bounce_rate,
        'avg_dur_min': int(avg_dur) // 60,
        'avg_dur_sec': int(avg_dur) % 60,
        'daily_data': daily_data,
        'top_pages': top_pages,
        'live_visitors': live_visitors,
        'sources': sources,
        'devices': devices,
        'countries': countries,
        'browsers': browsers,
        'base_url': base_url,
    })


# ═══════════════════════════════════════════════════════
# Feature: Domain Comparison View
# ═══════════════════════════════════════════════════════

@login_required
def website_compare(request):
    """Compare 2-3 websites side by side."""
    org = get_user_org(request.user)
    ids_raw = request.GET.get('ids', '')
    ids = [int(x) for x in ids_raw.split(',') if x.strip().isdigit()][:3]

    all_websites = list(Website.objects.filter(organization=org))
    now = timezone.now()
    last_7d = now - timedelta(days=7)
    last_30d = now - timedelta(days=30)
    last_30_min = now - timedelta(minutes=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    compare_data = []
    for ws in Website.objects.filter(id__in=ids, organization=org):
        visitors_qs = Visitor.objects.filter(website=ws)
        pageviews_qs = PageView.objects.filter(visitor__website=ws)
        chats_qs = ChatRoom.objects.filter(website=ws)

        week_v = visitors_qs.filter(first_visit__gte=last_7d)
        bounced = week_v.filter(is_bounced=True).count()
        week_count = week_v.count()
        avg_dur = week_v.filter(session_duration__gt=0).aggregate(avg=Avg('session_duration'))['avg'] or 0

        # Daily trend (7 days)
        daily = []
        for i in range(6, -1, -1):
            day = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day + timedelta(days=1)
            daily.append({
                'date': day.strftime('%b %d'),
                'visitors': visitors_qs.filter(first_visit__gte=day, first_visit__lt=day_end).count(),
                'views': pageviews_qs.filter(timestamp__gte=day, timestamp__lt=day_end).count(),
            })

        compare_data.append({
            'ws': ws,
            'total_visitors': visitors_qs.count(),
            'week_visitors': week_count,
            'today_visitors': visitors_qs.filter(first_visit__gte=today_start).count(),
            'online_now': visitors_qs.filter(last_seen__gte=last_30_min).count(),
            'total_pageviews': pageviews_qs.count(),
            'week_pageviews': pageviews_qs.filter(timestamp__gte=last_7d).count(),
            'bounce_rate': round((bounced / week_count * 100)) if week_count > 0 else 0,
            'avg_dur_min': int(avg_dur) // 60,
            'avg_dur_sec': int(avg_dur) % 60,
            'total_chats': chats_qs.count(),
            'active_chats': chats_qs.filter(status__in=['waiting', 'active']).count(),
            'daily': daily,
        })

    return render(request, 'dashboard/website_compare.html', {
        'compare_data': compare_data,
        'all_websites': all_websites,
        'selected_ids': ids,
    })


# ═══════════════════════════════════════════════════════
# Feature: Auto-Detected Domain Approval
# ═══════════════════════════════════════════════════════

@login_required
def website_approve(request, website_id):
    """Approve or reject an auto-detected domain."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return JsonResponse({'error': 'Permission denied'}, status=403)

    if request.method == 'POST':
        ws = get_object_or_404(Website, id=website_id, organization=org)
        data = _parse_json_body(request) or {}
        action = data.get('action', 'approve')

        if action == 'approve':
            ws.approval_status = 'approved'
            ws.save(update_fields=['approval_status'])
            return JsonResponse({'status': 'ok', 'approval_status': 'approved'})
        elif action == 'reject':
            ws.approval_status = 'rejected'
            ws.is_active = False
            ws.save(update_fields=['approval_status', 'is_active'])
            return JsonResponse({'status': 'ok', 'approval_status': 'rejected'})
        elif action == 'delete':
            ws.delete()
            return JsonResponse({'status': 'ok', 'deleted': True})

    return JsonResponse({'error': 'POST required'}, status=405)


# ═══════════════════════════════════════════════════════
# Feature: Per-Website Notification Settings
# ═══════════════════════════════════════════════════════

@login_required
def website_notifications(request, website_id):
    """Update notification settings for a website."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return JsonResponse({'error': 'Permission denied'}, status=403)

    ws = get_object_or_404(Website, id=website_id, organization=org)

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        if 'notify_new_visitor' in data:
            ws.notify_new_visitor = bool(data['notify_new_visitor'])
        if 'notify_new_chat' in data:
            ws.notify_new_chat = bool(data['notify_new_chat'])
        if 'notify_offline_msg' in data:
            ws.notify_offline_msg = bool(data['notify_offline_msg'])
        if 'notify_error' in data:
            ws.notify_error = bool(data['notify_error'])
        if 'notify_email_override' in data:
            ws.notify_email_override = (data['notify_email_override'] or '').strip()
        ws.save()
        return JsonResponse({'status': 'ok'})

    return JsonResponse({
        'notify_new_visitor': ws.notify_new_visitor,
        'notify_new_chat': ws.notify_new_chat,
        'notify_offline_msg': ws.notify_offline_msg,
        'notify_error': ws.notify_error,
        'notify_email_override': ws.notify_email_override,
    })


# ═══════════════════════════════════════════════════════
# Feature: Real-Time Domain Activity Feed
# ═══════════════════════════════════════════════════════

@login_required
def website_activity_feed(request):
    """Real-time activity feed showing live traffic per domain."""
    org = get_user_org(request.user)
    now = timezone.now()
    last_5_min = now - timedelta(minutes=5)
    last_30_min = now - timedelta(minutes=30)

    # Single annotated query for the website list with online counts (was N+1)
    websites = list(
        Website.objects.filter(organization=org, is_active=True)
        .annotate(online_count=Count('visitors', filter=Q(visitors__last_seen__gte=last_30_min)))
    )
    ws_ids = [w.id for w in websites]

    # Batch-fetch recent visitors and pages across all websites in 2 queries instead of 2*N.
    # We over-fetch then trim to top 5/10 per website in Python — bounded by len(websites)*5/10.
    # Cap rows globally — previously each site limited to 5/10, so worst case was 5N/10N rows.
    n = max(1, len(ws_ids))
    visitors_by_ws = {wid: [] for wid in ws_ids}
    for v in (Visitor.objects
              .filter(website_id__in=ws_ids, last_seen__gte=last_5_min)
              .order_by('-last_seen')
              .values('id', 'website_id', 'ip_address', 'browser', 'country', 'last_seen', 'device_type')[:5 * n]):
        bucket = visitors_by_ws[v['website_id']]
        if len(bucket) < 5:
            v['last_seen'] = v['last_seen'].isoformat()
            bucket.append(v)

    pages_by_ws = {wid: [] for wid in ws_ids}
    for p in (PageView.objects
              .filter(visitor__website_id__in=ws_ids, timestamp__gte=last_5_min)
              .order_by('-timestamp')
              .values('url', 'page_title', 'timestamp', 'visitor__website_id', 'visitor__ip_address', 'visitor__country')[:10 * n]):
        bucket = pages_by_ws[p['visitor__website_id']]
        if len(bucket) < 10:
            p['timestamp'] = p['timestamp'].isoformat()
            bucket.append(p)

    feed = [{
        'id': ws.id,
        'name': ws.name,
        'domain': ws.domain,
        'online': ws.online_count,
        'recent_visitors': visitors_by_ws[ws.id],
        'recent_pages': pages_by_ws[ws.id],
    } for ws in websites]

    # Sort by online count descending
    feed.sort(key=lambda x: x['online'], reverse=True)
    return JsonResponse({'feed': feed})


# ═══════════════════════════════════════════════════════
# Feature: Website Groups/Tags
# ═══════════════════════════════════════════════════════

@login_required
def website_groups(request):
    """CRUD for website groups/tags."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return JsonResponse({'error': 'Permission denied'}, status=403)

    if request.method == 'POST':
        data = _parse_json_body(request) or {}
        action = data.get('action', 'create')

        if action == 'create':
            name = (data.get('name') or '').strip()
            color = (data.get('color') or '#6366f1').strip()
            if not name:
                return JsonResponse({'error': 'Name is required'}, status=400)
            if WebsiteGroup.objects.filter(organization=org, name=name).exists():
                return JsonResponse({'error': 'Group already exists'}, status=400)
            g = WebsiteGroup.objects.create(organization=org, name=name, color=color)
            return JsonResponse({'status': 'ok', 'id': g.id, 'name': g.name, 'color': g.color})

        elif action == 'delete':
            gid = data.get('group_id')
            WebsiteGroup.objects.filter(id=gid, organization=org).delete()
            return JsonResponse({'status': 'ok'})

        elif action == 'assign':
            ws_id = data.get('website_id')
            gid = data.get('group_id')  # None to unassign
            ws = get_object_or_404(Website, id=ws_id, organization=org)
            if gid:
                group = get_object_or_404(WebsiteGroup, id=gid, organization=org)
                ws.group = group
            else:
                ws.group = None
            ws.save(update_fields=['group'])
            return JsonResponse({'status': 'ok'})

    groups = list(WebsiteGroup.objects.filter(organization=org).values('id', 'name', 'color'))
    return JsonResponse({'groups': groups})


# ═══════════════════════════════════════════════════════
# Feature: Cross-Domain Visitor Linking
# ═══════════════════════════════════════════════════════

@login_required
def cross_domain_visitors(request):
    """Find visitors that appear across multiple domains (by fingerprint)."""
    org = get_user_org(request.user)

    # Find fingerprints that appear in multiple websites
    from django.db.models import Count as DbCount
    multi_fp = (
        Visitor.objects.filter(organization=org)
        .exclude(visitor_fingerprint='')
        .values('visitor_fingerprint')
        .annotate(
            site_count=DbCount('website', distinct=True),
            total_visits=Sum('total_visits'),
        )
        .filter(site_count__gte=2)
        .order_by('-site_count', '-total_visits')[:50]
    )

    linked_visitors = []
    for item in multi_fp:
        fp = item['visitor_fingerprint']
        visitors = list(
            Visitor.objects.filter(organization=org, visitor_fingerprint=fp)
            .select_related('website')
            .order_by('-last_seen')
            .values(
                'id', 'ip_address', 'browser', 'os', 'device_type', 'country',
                'first_visit', 'last_seen', 'total_visits',
                'website__name', 'website__domain', 'website_id',
            )
        )
        for v in visitors:
            v['first_visit'] = v['first_visit'].isoformat() if v['first_visit'] else ''
            v['last_seen'] = v['last_seen'].isoformat() if v['last_seen'] else ''
        domains = list({v['website__domain'] for v in visitors if v['website__domain']})
        linked_visitors.append({
            'fingerprint': fp[:16] + '...',
            'site_count': item['site_count'],
            'total_visits': item['total_visits'],
            'domains': domains,
            'visitors': visitors,
        })

    return render(request, 'dashboard/cross_domain_visitors.html', {
        'linked_visitors': linked_visitors,
        'total_linked': len(linked_visitors),
    })


# ═══════════════════════════════════════════════════════
# Feature: Embeddable Visitor Counter Badge
# ═══════════════════════════════════════════════════════

@csrf_exempt
def visitor_badge(request):
    """Public endpoint: Returns visitor count badge as SVG or JS snippet."""
    key = request.GET.get('key', '')
    fmt = request.GET.get('format', 'svg').lower()
    label = request.GET.get('label', 'visitors online')

    from tracker.core.models import Organization, Website
    website = Website.objects.select_related('organization').filter(tracking_key=key).first() if key else None
    org = website.organization if website else (Organization.objects.filter(widget_key=key).first() if key else None)

    if not org:
        if fmt == 'json':
            return JsonResponse({'count': 0, 'label': label})
        svg = _badge_svg(0, label, '#999')
        return HttpResponse(svg, content_type='image/svg+xml')

    last_30_min = timezone.now() - timedelta(minutes=30)
    if website:
        count = Visitor.objects.filter(website=website, last_seen__gte=last_30_min).count()
        color = website.widget_color or org.widget_color or '#7c3aed'
    else:
        count = Visitor.objects.filter(organization=org, last_seen__gte=last_30_min).count()
        color = org.widget_color or '#7c3aed'

    if fmt == 'json':
        return JsonResponse({'count': count, 'label': label})
    elif fmt == 'js':
        js = f'(function(){{var e=document.getElementById("livetrack-badge");if(e){{e.innerHTML="{count} {label}";}}else{{document.write("<span id=\\"livetrack-badge\\" style=\\"display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:12px;font-size:12px;font-weight:600;background:{color}22;color:{color};font-family:sans-serif;\\"><span style=\\"width:6px;height:6px;border-radius:50%;background:{color};\\"></span>{count} {label}</span>");}}}})();'
        return HttpResponse(js, content_type='application/javascript; charset=utf-8')

    svg = _badge_svg(count, label, color)
    return HttpResponse(svg, content_type='image/svg+xml')


def _badge_svg(count, label, color):
    """Generate a GitHub-style SVG badge."""
    text = f'{count} {label}'
    width = max(len(text) * 7 + 20, 80)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="24" viewBox="0 0 {width} 24">
  <rect width="{width}" height="24" rx="5" fill="{color}" opacity="0.15"/>
  <circle cx="12" cy="12" r="4" fill="{color}"/>
  <text x="22" y="16" font-family="Inter,Arial,sans-serif" font-size="12" font-weight="600" fill="{color}">{text}</text>
</svg>'''



@login_required
@csrf_exempt
def visitors_bulk_action(request):
    """Bulk action on selected visitors (ban/unban). Owner/admin only."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return JsonResponse({'error': 'permission denied'}, status=403)
    try:
        data = _parse_json_body(request) or {}
    except (ValueError, TypeError):
        return JsonResponse({'error': 'invalid JSON'}, status=400)
    action = (data.get('action') or '').strip().lower()
    ids = data.get('ids') or []
    if action not in ('ban', 'unban') or not isinstance(ids, list) or not ids:
        return JsonResponse({'error': 'action (ban|unban) and ids required'}, status=400)
    try:
        id_list = [int(x) for x in ids if str(x).isdigit()]
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid ids'}, status=400)
    org = get_user_org(request.user)
    qs = Visitor.objects.filter(organization=org, id__in=id_list)
    affected = qs.update(is_banned=(action == 'ban'))
    return JsonResponse({'ok': True, 'action': action, 'affected': affected})


# ═══════════════════════════════════════════════════════════
# AI Snippet completion (slash-command drafted replies)
# ═══════════════════════════════════════════════════════════

@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_snippet_view(request, room_id):
    """POST /dashboard/api/ai/snippet/<room_id>/ {"command": "refund"}
    Returns a drafted reply based on the chat context + visitor + KB. Agent
    pastes/edits before sending — this is suggestion, never auto-send."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)

    from tracker.core.throttle import check as throttle_check
    state = throttle_check(request, action='ai_snippet', limit=30, window=60,
                            key=f'user:{request.user.id}')
    if state.blocked:
        return JsonResponse({'error': 'Slow down — too many suggestions'}, status=429)

    data = _parse_json_body(request) or {}
    command = (data.get('command') or '').strip()
    if not command:
        return JsonResponse({'error': 'command required'}, status=400)

    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({
            'error': 'AI not configured for this organization. Set up an API key in AI Bot settings.',
            'code': 'AI_NOT_CONFIGURED',
        }, status=400)

    # Build transcript tail for grounding.
    msgs = (Message.objects
            .filter(room=room)
            .exclude(sender_type='system')
            .order_by('-timestamp')[:8])
    transcript = [
        {'role': 'visitor' if m.sender_type == 'visitor' else 'agent', 'content': m.content}
        for m in reversed(list(msgs))
    ]

    from tracker.chat.ai import suggest_snippet
    out = suggest_snippet(
        config, room, command, transcript,
        visitor_name=room.visitor_name or '',
    )
    if not out:
        return JsonResponse({'error': 'AI did not return a suggestion'}, status=502)
    return JsonResponse({'ok': True, 'text': out, 'command': command})


# ═══════════════════════════════════════════════════════════
# Per-message translation (on-demand)
# ═══════════════════════════════════════════════════════════

@login_required
@requires_feature('ai_bot', plan_label='Enterprise')
def ai_translate_view(request):
    """POST /dashboard/api/ai/translate/ {"text": "...", "target": "en"}
    Returns the translated text. Used by the per-message Translate button."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    org = get_user_org(request.user)
    data = _parse_json_body(request) or {}
    text = (data.get('text') or '').strip()
    target = (data.get('target') or 'en').strip()
    source = (data.get('source') or '').strip()
    if not text:
        return JsonResponse({'error': 'text required'}, status=400)
    if len(text) > 4000:
        return JsonResponse({'error': 'text too long (4000 char max)'}, status=400)

    from tracker.core.throttle import check as throttle_check
    state = throttle_check(request, action='ai_translate', limit=60, window=60,
                            key=f'user:{request.user.id}')
    if state.blocked:
        return JsonResponse({'error': 'Slow down — too many translate calls'}, status=429)

    config = AIBotConfig.objects.filter(organization=org).first()
    if not config or not config.api_key:
        return JsonResponse({
            'error': 'AI not configured. Set up Gemini / Claude key in AI Bot settings.',
            'code': 'AI_NOT_CONFIGURED',
        }, status=400)

    from tracker.chat.ai import translate_text
    translated = translate_text(config, text, target_language=target, source_language=source)
    if not translated:
        return JsonResponse({'error': 'Translation failed'}, status=502)
    return JsonResponse({'ok': True, 'translated': translated, 'target': target})


# ═══════════════════════════════════════════════════════════
# Widget funnel — bubble seen → opened → typed → sent
# ═══════════════════════════════════════════════════════════

@csrf_exempt
def widget_funnel_event(request):
    """POST /api/widget/funnel/ {"event": "bubble_seen|panel_opened|typed|sent",
    "key": "<tracking_key>", "session_key": "..."}.

    No auth — fired from the widget on any customer site. Idempotent per
    (session_key, event) so reloading a page doesn't double-count, and
    per-IP rate limited to stop abuse.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    from tracker.core.views import _parse_json_body as _parse, _get_website_from_request
    data = _parse(request) or {}
    event = (data.get('event') or '').strip()
    if event not in ('bubble_seen', 'panel_opened', 'typed', 'sent'):
        return JsonResponse({'error': 'invalid event'}, status=400)
    session_key = (data.get('session_key') or '').strip()
    if not session_key:
        return JsonResponse({'error': 'session_key required'}, status=400)

    org, website = _get_website_from_request(request)
    if not org:
        return JsonResponse({'error': 'org not found'}, status=404)

    # Idempotency: at most one row per (session, event, hour). Avoids one
    # visitor refreshing 50× from flooding the funnel.
    from tracker.core import process_throttle
    dedup_key = f'funnel:{event}:{session_key}'
    if not process_throttle.should_run(dedup_key, 3600):
        return JsonResponse({'ok': True, 'deduped': True})

    from tracker.chat.models import WidgetFunnelEvent
    WidgetFunnelEvent.objects.create(
        organization=org,
        website=website,
        event=event,
        session_key=session_key,
    )
    return JsonResponse({'ok': True})


@login_required
def widget_funnel_dashboard(request):
    """Dashboard view that shows the funnel for the last 7 / 30 days."""
    org = get_user_org(request.user)
    from tracker.chat.models import WidgetFunnelEvent
    from django.db.models import Count
    days = int(request.GET.get('days', 7) or 7)
    days = min(max(days, 1), 90)
    since = timezone.now() - timedelta(days=days)
    rows = (WidgetFunnelEvent.objects
            .filter(organization=org, created_at__gte=since)
            .values('event')
            .annotate(c=Count('session_key', distinct=True)))
    counts = {r['event']: r['c'] for r in rows}
    steps = [
        ('bubble_seen', 'Bubble seen'),
        ('panel_opened', 'Panel opened'),
        ('typed', 'Typed something'),
        ('sent', 'Message sent'),
    ]
    funnel = []
    prev = counts.get('bubble_seen', 0) or 0
    for key, label in steps:
        n = counts.get(key, 0)
        pct_of_top = (n / prev * 100) if (prev and key != 'bubble_seen') else 100.0
        # actual prev is the immediately previous step, not bubble_seen
        funnel.append({'key': key, 'label': label, 'count': n})
    # Compute step-to-step drop-off
    for i, item in enumerate(funnel):
        prev_count = funnel[i - 1]['count'] if i > 0 else (item['count'] or 1)
        item['pct_of_top'] = round((item['count'] / funnel[0]['count'] * 100) if funnel[0]['count'] else 0, 1)
        item['pct_of_prev'] = round((item['count'] / prev_count * 100) if prev_count else 0, 1)
        item['dropped'] = max(0, prev_count - item['count']) if i > 0 else 0
    return render(request, 'dashboard/widget_funnel.html', {
        'funnel': funnel,
        'days': days,
        'org': org,
    })
