import csv
import logging
from django.conf import settings
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db.models import Count, Q, Avg, Sum, F, Max, Subquery, OuterRef
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime, timedelta, timezone as dt_timezone
import json
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from tracker.visitors.models import (
    Visitor, PageView, CustomEvent, Goal, GoalCompletion, ScheduledReport,
    SessionRecording, ClickData, ScrollData, JSError, FrustrationSignal, PageInsight,
)
from tracker.chat.models import (
    ChatRoom, Message, AgentProfile, OfflineMessage, CannedResponse, VisitorNote,
    InternalNote, Webhook, ActivityLog, ChatLabel, SavedReply,
    Department, DepartmentMember, SLAPolicy, SLABreach,
    Survey, SurveyQuestion, SurveyResponse, SurveyAnswer,
    AIBotConfig, AIBotKnowledge, ChatbotFlow,
    KBCategory, KBArticle, WhatsAppConfig, WhatsAppMessage, VisitorSegment,
    AgentWebsiteAccess,
)
from tracker.chat.security import create_ws_token
from tracker.chat.utils import close_stale_chats
from tracker.core.models import WebsiteSettings, Organization, Website
from tracker.core.views import get_user_org

logger = logging.getLogger(__name__)


# ═══════ Website Filter Helper ═══════
def get_website_filter(request, org):
    """Return a dict filter for website-scoping dashboard queries.
    Owner/admin: selected website or all. Agent: only accessible websites."""
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))

    selected_id = request.session.get('selected_website_id')
    if selected_id:
        try:
            selected_id = int(selected_id)
        except (ValueError, TypeError):
            selected_id = None

    if is_owner:
        if selected_id:
            ws = Website.objects.filter(id=selected_id, organization=org).first()
            if ws:
                return {'website_id': ws.id}
        return {}  # All websites
    else:
        # Agent: only accessible websites
        accessible_ids = list(
            AgentWebsiteAccess.objects.filter(agent=profile).values_list('website_id', flat=True)
        )
        if not accessible_ids:
            # Backward compat: agent with no access rows sees all (legacy agents)
            return {}
        if selected_id and selected_id in accessible_ids:
            return {'website_id': selected_id}
        return {'website_id__in': accessible_ids}


def get_selected_website(request, org):
    """Return the currently selected Website object or None (all)."""
    selected_id = request.session.get('selected_website_id')
    if selected_id:
        return Website.objects.filter(id=selected_id, organization=org).first()
    return None


@login_required
def dashboard_home(request):
    org = get_user_org(request.user)
    # Close stale chats only once per minute (cached)
    from django.core.cache import cache
    if not cache.get(f'stale_check_{org.id if org else 0}'):
        close_stale_chats(inactive_minutes=30)
        cache.set(f'stale_check_{org.id if org else 0}', True, 60)
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
    close_stale_chats(inactive_minutes=30)
    now = timezone.now()
    sla_minutes = int(getattr(settings, 'CHAT_SLA_MINUTES', 5))
    sla_cutoff = now - timedelta(minutes=sla_minutes)
    status_filter = request.GET.get('status', 'all')
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

    visitors = Visitor.objects.filter(organization=org, **ws_filter).annotate(
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
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        visitors = visitors.filter(first_visit__gte=today_start)

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
        'visit_duration': visit_duration,
    })


@login_required
def api_stats(request):
    org = get_user_org(request.user)
    # Throttle stale chat cleanup
    from django.core.cache import cache
    if not cache.get(f'stale_api_{org.id if org else 0}'):
        close_stale_chats(inactive_minutes=30)
        cache.set(f'stale_api_{org.id if org else 0}', True, 30)
    now = timezone.now()
    last_30_min = now - timedelta(minutes=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    ws_filter = get_website_filter(request, org)
    return JsonResponse({
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
    })


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
        return JsonResponse({'status': 'closed'})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def transfer_chat(request, room_id):
    """Transfer chat to another available agent."""
    org = get_user_org(request.user)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body)
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
        data = json.loads(request.body)
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
        data = json.loads(request.body)
        room.priority = data.get('priority', 'medium')
        room.save(update_fields=['priority'])
        return JsonResponse({'status': 'ok', 'priority': room.priority})
    return JsonResponse({'error': 'POST required'}, status=405)


@csrf_exempt
def rate_chat(request, room_id):
    """Visitor rates a chat."""
    if request.method == 'POST':
        room = get_object_or_404(ChatRoom, room_id=room_id)
        data = json.loads(request.body)
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
    """Export all visitors as CSV."""
    org = get_user_org(request.user)
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="visitors_export.csv"'

    writer = csv.writer(response)
    writer.writerow(['ID', 'IP Address', 'Browser', 'OS', 'Device', 'Source', 'First Visit', 'Last Seen', 'Total Visits', 'Online'])

    visitors_qs = Visitor.objects.filter(organization=org)
    if date_from:
        visitors_qs = visitors_qs.filter(first_visit__date__gte=date_from)
    if date_to:
        visitors_qs = visitors_qs.filter(first_visit__date__lte=date_to)

    for v in visitors_qs:
        writer.writerow([
            v.id, v.ip_address, v.browser, v.os, v.device_type,
            v.referrer_source, v.first_visit.strftime('%Y-%m-%d %H:%M'),
            v.last_seen.strftime('%Y-%m-%d %H:%M'), v.total_visits, v.is_online,
        ])

    return response


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
    chats_qs = ChatRoom.objects.filter(organization=org).select_related('agent').annotate(
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

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="chats_export.csv"'
    writer = csv.writer(response)
    writer.writerow(['Room ID', 'Visitor', 'Email', 'Agent', 'Status', 'Priority', 'Subject', 'Rating', 'Tags', 'Messages', 'Unread', 'Created', 'Closed'])
    for c in chats_qs:
        writer.writerow([
            c.room_id, c.visitor_name, c.visitor_email,
            c.agent.get_full_name() if c.agent else '-',
            c.status, c.priority, c.subject, c.rating or '-', c.tags,
            c.message_count_db, c.unread_count, c.created_at.strftime('%Y-%m-%d %H:%M'),
            c.closed_at.strftime('%Y-%m-%d %H:%M') if c.closed_at else '-',
        ])
    return response


@login_required
def offline_messages_view(request):
    """View offline messages. Owner only."""
    profile = getattr(request.user, 'agent_profile', None)
    if not request.user.is_superuser and (not profile or profile.role not in ('owner', 'admin')):
        return HttpResponse("Forbidden — owners only.", status=403)
    org = get_user_org(request.user)
    messages_list = OfflineMessage.objects.filter(organization=org)
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
    data = json.loads(request.body) if request.body else {}
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

    # Overall stats
    chats_qs = ChatRoom.objects.filter(organization=org)
    total_chats = chats_qs.count()
    avg_rating = chats_qs.filter(rating__isnull=False).aggregate(avg=Avg('rating'))['avg']
    total_closed = chats_qs.filter(status='closed').count()
    today_total = chats_qs.filter(created_at__gte=today_start).count()

    # Chats per day (last 7 days)
    daily_chats = []
    for i in range(7):
        day = now - timedelta(days=6-i)
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        count = chats_qs.filter(created_at__gte=day_start, created_at__lt=day_end).count()
        daily_chats.append({'day': day_start.strftime('%a'), 'count': count})

    # Rating distribution
    rating_dist = []
    for r in range(1, 6):
        count = chats_qs.filter(rating=r).count()
        rating_dist.append({'rating': r, 'count': count})

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
def canned_responses_view(request):
    """Manage canned responses."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = json.loads(request.body)
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

        if not site_name:
            error = 'Site name is required.'
        else:
            org.name = site_name
            org.widget_title = widget_title or org.widget_title
            org.widget_color = widget_color
            org.widget_position = widget_position or org.widget_position
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

    agents = User.objects.filter(agent_profile__isnull=False, agent_profile__organization=org).select_related('agent_profile').order_by('username')
    websites = Website.objects.filter(organization=org)
    # Add website access info per agent
    for agent in agents:
        agent.accessible_websites = list(
            AgentWebsiteAccess.objects.filter(agent=agent.agent_profile).select_related('website').values_list('website__name', flat=True)
        )
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
        data = json.loads(request.body) if request.body else {}
        email = data.get('email', '').strip() or room.visitor_email
        if not email:
            return JsonResponse({'error': 'No email address provided'}, status=400)

        messages_list = room.messages.all()
        lines = [f'Chat Transcript - {room.visitor_name}', f'Room: {room.room_id}', f'Date: {room.created_at.strftime("%Y-%m-%d %H:%M")}', '']
        for msg in messages_list:
            lines.append(f'[{msg.timestamp.strftime("%H:%M")}] {msg.sender_name}: {msg.content}')

        try:
            from django.core.mail import send_mail
            send_mail(
                f'Chat Transcript - {room.visitor_name} - {org.name}',
                '\n'.join(lines),
                'noreply@livetrack.app',
                [email],
                fail_silently=False,
            )
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
def webhook_list(request):
    """Manage webhooks for chat events."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        url = request.POST.get('url', '').strip()
        events = ','.join(request.POST.getlist('events'))
        secret = request.POST.get('secret', '').strip()
        if url:
            Webhook.objects.create(organization=org, url=url, events=events, secret=secret)
            _log_activity(org, request.user, 'webhook.created', f'Webhook created: {url[:50]}')
    webhooks = Webhook.objects.filter(organization=org)
    event_choices = Webhook.EVENT_CHOICES
    return render(request, 'dashboard/webhooks.html', {
        'webhooks': webhooks,
        'event_choices': event_choices,
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


def fire_webhook(org, event, payload):
    """Fire webhooks for an event (non-blocking)."""
    import threading
    import urllib.request

    def _send(url, data, secret):
        try:
            import hashlib, hmac
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, headers={
                'Content-Type': 'application/json',
                'X-LiveTrack-Event': data.get('event', ''),
            })
            if secret:
                sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
                req.add_header('X-LiveTrack-Signature', sig)
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    if not org:
        return
    webhooks = Webhook.objects.filter(organization=org, is_active=True)
    for wh in webhooks:
        if event in wh.events:
            t = threading.Thread(target=_send, args=(wh.url, payload, wh.secret))
            t.daemon = True
            t.start()


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
    chats_qs = ChatRoom.objects.filter(organization=org)

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
    country_stats = list(Visitor.objects.filter(organization=org).exclude(country='').values('country').annotate(count=Count('id')).order_by('-count')[:10])

    # Busiest hours
    from django.db.models.functions import ExtractHour
    hourly_chats = list(chats_qs.annotate(hour=ExtractHour('created_at')).values('hour').annotate(count=Count('id')).order_by('hour'))

    total_chats = chats_qs.count()
    avg_rating = chats_qs.filter(rating__isnull=False).aggregate(a=Avg('rating'))['a']
    total_messages = Message.objects.filter(room__organization=org).count()
    total_visitors = Visitor.objects.filter(organization=org).count()
    closed_chats = status_counts['closed']
    completion_rate = round((closed_chats / total_chats * 100), 1) if total_chats > 0 else 0

    # Top agents by chats
    top_agents = User.objects.filter(
        agent_profile__organization=org
    ).annotate(
        chats_count=Count('chat_rooms', filter=Q(chat_rooms__organization=org)),
        avg_agent_rating=Avg('chat_rooms__rating', filter=Q(chat_rooms__rating__isnull=False)),
    ).order_by('-chats_count')[:5]

    # Browser breakdown
    browser_stats = list(Visitor.objects.filter(organization=org).values('browser').annotate(count=Count('id')).order_by('-count')[:6])
    total_browser = sum(b['count'] for b in browser_stats) or 1

    # Device breakdown
    device_stats = list(Visitor.objects.filter(organization=org).values('device_type').annotate(count=Count('id')).order_by('-count'))
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
    data = json.loads(request.body) if request.body else {}
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
    data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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

    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
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
def ai_bot_config_view(request):
    """Configure AI auto-reply bot."""
    org = get_user_org(request.user)
    # Plan check
    from tracker.core.views import check_plan_feature
    if not request.user.is_superuser and not check_plan_feature(org, 'ai_bot'):
        return render(request, 'dashboard/plan_required.html', {'feature': 'AI Auto-Reply Bot', 'required_plan': 'Enterprise'})

    config, _ = AIBotConfig.objects.get_or_create(organization=org)

    if request.method == 'POST':
        data = json.loads(request.body) if request.body else {}
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
def chatbot_flows_view(request):
    """Manage chatbot flows."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
def whatsapp_config_view(request):
    """Configure WhatsApp Business API integration."""
    org = get_user_org(request.user)
    config, _ = WhatsAppConfig.objects.get_or_create(organization=org)

    if request.method == 'POST':
        data = json.loads(request.body) if request.body else {}
        config.is_enabled = data.get('is_enabled', config.is_enabled)
        config.phone_number_id = data.get('phone_number_id', config.phone_number_id)
        config.access_token = data.get('access_token', config.access_token)
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
        data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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
def advanced_analytics_view(request):
    """Google Analytics-style advanced analytics dashboard."""
    org = get_user_org(request.user)
    start, end, date_from, date_to, period = _parse_date_range(request)
    prev_start, prev_end = _get_prev_period(start, end)

    visitors_qs = Visitor.objects.filter(organization=org)
    pageviews_qs = PageView.objects.filter(visitor__organization=org)
    chats_qs = ChatRoom.objects.filter(organization=org)

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
        data = json.loads(request.body) if request.body else {}
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
    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    from tracker.core.views import _get_org_from_request
    org = _get_org_from_request(request)
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
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
    data = json.loads(request.body) if request.body else {}
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
def scheduled_reports_view(request):
    """Manage scheduled email reports."""
    org = get_user_org(request.user)
    if request.method == 'POST':
        data = json.loads(request.body) if request.body else {}
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
    from django.core.mail import send_mail
    now = timezone.now()
    last_7 = now - timedelta(days=7)
    lines = [f"LiveVisitorHub — {report.name}", f"Period: {last_7.strftime('%b %d')} - {now.strftime('%b %d, %Y')}", ""]

    if report.include_visitors:
        total = Visitor.objects.filter(organization=org, first_visit__gte=last_7).count()
        online = Visitor.objects.filter(organization=org, last_seen__gte=now - timedelta(minutes=30)).count()
        lines += [f"VISITORS: {total} new, {online} currently online"]

    if report.include_chats:
        chats = ChatRoom.objects.filter(organization=org, created_at__gte=last_7)
        lines += [f"CHATS: {chats.count()} total, {chats.filter(status='closed').count()} closed"]
        avg_rating = chats.filter(rating__isnull=False).aggregate(a=Avg('rating'))['a']
        if avg_rating:
            lines.append(f"AVG RATING: {avg_rating:.1f}/5")

    if report.include_goals:
        completions = GoalCompletion.objects.filter(goal__organization=org, completed_at__gte=last_7).count()
        lines += [f"GOAL COMPLETIONS: {completions}"]

    lines += ["", f"View full analytics: /dashboard/advanced-analytics/", "", "— LiveVisitorHub"]

    try:
        send_mail(
            f"[LiveVisitorHub] {report.name} - {now.strftime('%b %d')}",
            '\n'.join(lines), settings.DEFAULT_FROM_EMAIL, [report.email],
            fail_silently=False,
        )
        report.last_sent = now
        report.save(update_fields=['last_sent'])
    except Exception:
        logger.exception('Failed to send scheduled report id=%s to %s', report.id, report.email)


# ═══════════════════════════════════════════════════════════
# MICROSOFT CLARITY FEATURES
# ═══════════════════════════════════════════════════════════

# ─── Tracking APIs (called from visitor's browser JS) ───

@csrf_exempt
def track_clicks_api(request):
    """Batch receive click data for heatmaps."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    from tracker.core.views import _get_org_from_request
    org = _get_org_from_request(request)
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
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
    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    from tracker.core.views import _get_org_from_request
    org = _get_org_from_request(request)
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
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
    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    from tracker.core.views import _get_org_from_request
    org = _get_org_from_request(request)
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
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
    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    from tracker.core.views import _get_org_from_request
    org = _get_org_from_request(request)
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
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
    data = json.loads(request.body) if request.body else {}
    session_key = request.session.session_key
    if not session_key:
        return JsonResponse({'error': 'No session'}, status=400)

    from tracker.core.views import _get_org_from_request
    org = _get_org_from_request(request)
    visitor = Visitor.objects.filter(session_key=session_key, organization=org).first()
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

    clicks_qs = ClickData.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end)
    scroll_qs = ScrollData.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end)

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
    recordings = SessionRecording.objects.filter(organization=org).select_related('visitor')

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
    errors = JSError.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end)

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
    signals = FrustrationSignal.objects.filter(organization=org, timestamp__gte=start, timestamp__lte=end)

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
        organization=org, frustration_score__gt=0, created_at__gte=start
    ).order_by('-frustration_score')[:10]

    # Per-page insights
    page_insights = PageInsight.objects.filter(organization=org).order_by('-frustration_score')[:15]

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
        'stripe_public_key': settings.STRIPE_PUBLIC_KEY,
        'stripe_configured': bool(settings.STRIPE_SECRET_KEY),
    })


@login_required
def create_checkout_session(request):
    """Create a Stripe Checkout session for plan upgrade."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    data = json.loads(request.body) if request.body else {}
    plan = data.get('plan', 'pro')
    interval = data.get('interval', 'month')  # 'month' or 'year'
    coupon_code = data.get('coupon', '').strip()

    if interval not in ('month', 'year'):
        interval = 'month'

    stripe_key = settings.STRIPE_SECRET_KEY
    if not stripe_key:
        return JsonResponse({'error': 'Payment gateway not configured.'}, status=400)

    # Prices in cents — yearly = 12 months with 2 months free (save ~17%)
    PRICES = {
        'pro': {'month': 1900, 'year': 19000},       # $19/mo or $190/yr (save $38)
        'enterprise': {'month': 7900, 'year': 79000}, # $79/mo or $790/yr (save $158)
    }
    PLAN_NAMES = {'pro': 'LiveVisitorHub', 'enterprise': 'LiveVisitorHub Enterprise'}

    if plan not in PRICES:
        return JsonResponse({'error': 'Invalid plan'}, status=400)

    amount = PRICES[plan][interval]
    interval_label = 'Monthly' if interval == 'month' else 'Yearly'

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
        discount = int(coupon.calculate_discount(amount / 100) * 100)  # cents
        applied_coupon = coupon

    final_amount = max(amount - discount, 0)

    org = get_user_org(request.user)
    from tracker.core.models import Subscription
    sub, _ = Subscription.objects.get_or_create(organization=org, defaults={'plan': 'free'})

    try:
        import stripe
        stripe.api_key = stripe_key

        if not sub.stripe_customer_id:
            customer = stripe.Customer.create(
                email=request.user.email or '',
                name=org.name,
                metadata={'org_id': str(org.id), 'org_name': org.name},
            )
            sub.stripe_customer_id = customer.id
            sub.save(update_fields=['stripe_customer_id'])

        line_items = [{
            'price_data': {
                'currency': 'usd',
                'product_data': {
                    'name': f'{PLAN_NAMES[plan]} — {interval_label}',
                    'description': f'{interval_label} {plan.title()} plan for {org.name}' + (f' (Coupon: {coupon_code})' if coupon_code else ''),
                },
                'unit_amount': final_amount,
                'recurring': {'interval': interval},
            },
            'quantity': 1,
        }]

        checkout = stripe.checkout.Session.create(
            customer=sub.stripe_customer_id,
            payment_method_types=['card'],
            line_items=line_items,
            mode='subscription',
            success_url=request.build_absolute_uri('/dashboard/billing/success/') + '?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.build_absolute_uri('/dashboard/billing/'),
            metadata={'org_id': str(org.id), 'plan': plan, 'interval': interval, 'coupon': coupon_code},
            subscription_data={'metadata': {'org_id': str(org.id), 'plan': plan, 'interval': interval}},
        )

        # Persist intent so billing_success can recover plan/interval reliably
        # (Stripe metadata can occasionally be empty when read back)
        sub.pending_plan = plan
        sub.pending_interval = interval
        sub.billing_interval = interval
        if applied_coupon:
            sub.coupon_applied = applied_coupon
            sub.discount_percent = int(applied_coupon.discount_value) if applied_coupon.discount_type == 'percent' else 0
            applied_coupon.times_used += 1
            applied_coupon.save(update_fields=['times_used'])
        sub.save()

        return JsonResponse({'checkout_url': checkout.url})

    except ImportError:
        return JsonResponse({'error': 'Stripe library not installed.'}, status=500)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def validate_coupon(request):
    """Validate a coupon code and return discount info."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    data = json.loads(request.body) if request.body else {}
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
        data = json.loads(request.body) if request.body else {}
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

    coupons = Coupon.objects.all()
    return render(request, 'dashboard/coupons.html', {'coupons': coupons})


@login_required
def billing_success(request):
    """Handle successful checkout — activate plan."""
    from urllib.parse import unquote
    session_id = unquote(request.GET.get('session_id', '')).strip()
    org = get_user_org(request.user)
    from tracker.core.models import Subscription, PaymentHistory
    import logging
    log = logging.getLogger('tracker')

    success = False
    plan = 'pro'
    error_msg = ''

    log.info(f'Billing success called. session_id={session_id[:40] if session_id else "empty"}')

    # Try to process with Stripe session
    if session_id and session_id != '{CHECKOUT_SESSION_ID}' and settings.STRIPE_SECRET_KEY:
        try:
            import stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY

            # Retrieve session WITHOUT expand (safer)
            session = stripe.checkout.Session.retrieve(session_id)
            log.info(f'Stripe session: status={session.status}, payment={session.payment_status}, sub={session.subscription}')

            if session.payment_status in ('paid', 'no_payment_required') or session.status == 'complete':
                # Read metadata safely (Stripe metadata access can be quirky across SDK versions)
                meta = {}
                try:
                    if session.metadata:
                        # StripeObject supports .get() and is iterable
                        for k in session.metadata:
                            meta[k] = session.metadata.get(k)
                except Exception as e:
                    log.warning(f'Could not read session metadata: {e}')

                # Source of truth priority: DB pending → metadata → amount-based detection → defaults
                sub, _ = Subscription.objects.get_or_create(organization=org, defaults={'plan': 'free'})
                amount_paid = (session.amount_total or 0) / 100

                # 1. DB pending fields (most reliable — set right before checkout)
                plan = sub.pending_plan or meta.get('plan') or ''
                interval = sub.pending_interval or meta.get('interval') or ''

                # 2. Amount-based detection if still missing
                if not plan or not interval:
                    AMOUNT_MAP = {
                        19: ('pro', 'month'),    190: ('pro', 'year'),
                        79: ('enterprise', 'month'), 790: ('enterprise', 'year'),
                    }
                    detected = AMOUNT_MAP.get(int(round(amount_paid)))
                    if detected:
                        plan, interval = detected
                        log.info(f'Plan detected from amount ${amount_paid}: {plan}/{interval}')

                # 3. Final fallback
                plan = plan or 'pro'
                interval = interval or 'month'

                sub.plan = plan
                sub.status = 'active'
                sub.billing_interval = interval
                # Clear pending intent — checkout consumed
                sub.pending_plan = ''
                sub.pending_interval = ''

                # Save subscription ID (it's a string)
                if session.subscription:
                    sub.stripe_subscription_id = str(session.subscription)
                    try:
                        stripe_sub = stripe.Subscription.retrieve(str(session.subscription))
                        sub.cancel_at_period_end = bool(getattr(stripe_sub, 'cancel_at_period_end', False))
                        try:
                            interval_from_stripe = stripe_sub['items']['data'][0]['price']['recurring']['interval']
                            if interval_from_stripe in ('month', 'year'):
                                sub.billing_interval = interval_from_stripe
                                interval = interval_from_stripe
                        except Exception:
                            pass

                        period_start = getattr(stripe_sub, 'current_period_start', None)
                        period_end = getattr(stripe_sub, 'current_period_end', None)
                        if period_start:
                            sub.current_period_start = datetime.fromtimestamp(period_start, tz=dt_timezone.utc)
                        if period_end:
                            sub.current_period_end = datetime.fromtimestamp(period_end, tz=dt_timezone.utc)
                    except Exception as e:
                        log.warning(f'Could not load Stripe subscription period dates: {e}')

                sub.save()
                log.info(f'Subscription updated: plan={plan}, interval={interval}')

                coupon_code = meta.get('coupon', '')

                # Calculate original price and discount
                ORIGINAL_PRICES = {'pro': {'month': 19, 'year': 190}, 'enterprise': {'month': 79, 'year': 790}}
                original_price = ORIGINAL_PRICES.get(plan, {}).get(interval, amount_paid)
                discount_amount = round(original_price - amount_paid, 2) if amount_paid < original_price else 0

                # Build description — human readable
                interval_word = 'Yearly' if interval == 'year' else 'Monthly'
                desc = f'Upgraded to {plan.title()} plan ({interval_word})'
                if coupon_code and discount_amount > 0:
                    desc += f' — Coupon: {coupon_code} (${discount_amount} off)'

                # Record payment
                payment_id = str(session.payment_intent or session.id)
                if not PaymentHistory.objects.filter(stripe_payment_id=payment_id).exists():
                    PaymentHistory.objects.create(
                        organization=org,
                        amount=amount_paid if amount_paid > 0 else original_price,
                        plan=plan,
                        stripe_payment_id=payment_id,
                        stripe_invoice_id=str(session.invoice or ''),
                        description=desc,
                    )
                    log.info(f'Payment recorded: ${amount_paid}')

                _log_activity(org, request.user, 'plan.upgraded', f'Upgraded to {plan.title()} plan')
                success = True
            else:
                error_msg = f'Payment not completed. Status: {session.payment_status}'
                log.warning(error_msg)

        except Exception as e:
            error_msg = str(e)
            log.error(f'Billing success error: {e}', exc_info=True)

    # Fallback: check if Stripe subscription was already activated (webhook may have fired first)
    if not success:
        sub = Subscription.objects.filter(organization=org).first()
        if sub and sub.plan != 'free' and sub.status == 'active':
            success = True
            plan = sub.plan

    if success:
        return render(request, 'dashboard/billing_success.html', {'plan': plan})

    # If still not success, show error on billing page
    if error_msg:
        from django.contrib import messages as django_messages
        django_messages.error(request, f'Payment processing issue: {error_msg}. If you were charged, your plan will activate shortly.')
    return redirect('dashboard:billing')


@login_required
def download_invoice(request):
    """Get the best PDF/receipt URL for an invoice.

    Priority for PAID invoices:
        1. Stripe receipt URL (clean — no "Pay online" link, marked as paid)
        2. Stripe invoice PDF
        3. Hosted invoice page
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    data = json.loads(request.body) if request.body else {}
    invoice_id = data.get('invoice_id', '')

    if not invoice_id or not settings.STRIPE_SECRET_KEY:
        return JsonResponse({'error': 'Invoice not available'}, status=400)

    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        invoice = stripe.Invoice.retrieve(invoice_id, expand=['payment_intent', 'charge'])

        # 1. Try the receipt URL — for paid invoices this is the cleanest doc (no Pay online link)
        if getattr(invoice, 'status', '') == 'paid':
            receipt_url = ''
            try:
                # Direct charge expansion
                charge = getattr(invoice, 'charge', None)
                if charge and not isinstance(charge, str):
                    receipt_url = getattr(charge, 'receipt_url', '') or ''
                # Via payment_intent → charges
                if not receipt_url:
                    pi = getattr(invoice, 'payment_intent', None)
                    if pi and not isinstance(pi, str):
                        charges = getattr(pi, 'charges', None)
                        if charges and getattr(charges, 'data', None):
                            receipt_url = charges.data[0].receipt_url or ''
            except Exception:
                pass
            if receipt_url:
                return JsonResponse({'pdf_url': receipt_url, 'kind': 'receipt'})

        # 2. Direct invoice PDF (still has "Pay online" link in Stripe's PDF for unpaid invoices)
        pdf_url = getattr(invoice, 'invoice_pdf', '')
        if pdf_url:
            return JsonResponse({'pdf_url': pdf_url, 'kind': 'invoice_pdf'})

        # 3. Hosted invoice page
        if getattr(invoice, 'hosted_invoice_url', ''):
            return JsonResponse({'pdf_url': invoice.hosted_invoice_url, 'kind': 'hosted'})

        return JsonResponse({'error': 'PDF not available for this invoice'}, status=400)

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
def cancel_subscription(request):
    """Cancel subscription — downgrade to free at period end."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    org = get_user_org(request.user)
    from tracker.core.models import Subscription
    sub = Subscription.objects.filter(organization=org).first()
    if not sub:
        return JsonResponse({'error': 'No subscription found'}, status=400)

    if sub.stripe_subscription_id and settings.STRIPE_SECRET_KEY:
        try:
            import stripe
            stripe.api_key = settings.STRIPE_SECRET_KEY
            stripe.Subscription.modify(sub.stripe_subscription_id, cancel_at_period_end=True)
            sub.cancel_at_period_end = True
            sub.save(update_fields=['cancel_at_period_end'])
        except Exception:
            pass
    else:
        # No Stripe — just downgrade immediately
        sub.plan = 'free'
        sub.cancel_at_period_end = False
        sub.save(update_fields=['plan', 'cancel_at_period_end'])

    _log_activity(org, request.user, 'plan.cancelled', 'Subscription cancelled')
    return JsonResponse({'status': 'ok'})


@csrf_exempt
def stripe_webhook(request):
    """Handle Stripe webhook events."""
    if request.method != 'POST':
        return HttpResponse(status=405)

    payload = request.body
    sig = request.META.get('HTTP_STRIPE_SIGNATURE', '')
    webhook_secret = settings.STRIPE_WEBHOOK_SECRET

    if not webhook_secret:
        return HttpResponse(status=400)

    try:
        import stripe
        stripe.api_key = settings.STRIPE_SECRET_KEY
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except (ImportError, Exception):
        return HttpResponse(status=400)

    from tracker.core.models import Subscription, PaymentHistory

    if event.type in ('customer.subscription.created', 'customer.subscription.updated'):
        sub_data = event.data.object
        sub = Subscription.objects.filter(stripe_subscription_id=sub_data.id).first()
        if not sub:
            sub = Subscription.objects.filter(stripe_customer_id=getattr(sub_data, 'customer', '')).first()
        if sub:
            status_map = {'active': 'active', 'past_due': 'past_due', 'canceled': 'cancelled', 'trialing': 'trialing'}
            sub.status = status_map.get(sub_data.status, sub.status)
            sub.stripe_subscription_id = str(sub_data.id or sub.stripe_subscription_id)
            sub.cancel_at_period_end = bool(getattr(sub_data, 'cancel_at_period_end', False))
            if getattr(sub_data, 'current_period_start', None):
                sub.current_period_start = datetime.fromtimestamp(sub_data.current_period_start, tz=dt_timezone.utc)
            if getattr(sub_data, 'current_period_end', None):
                sub.current_period_end = datetime.fromtimestamp(sub_data.current_period_end, tz=dt_timezone.utc)
            try:
                interval = sub_data['items']['data'][0]['price']['recurring']['interval']
                if interval in ('month', 'year'):
                    sub.billing_interval = interval
            except Exception:
                pass
            sub.save()

    elif event.type == 'customer.subscription.deleted':
        sub_data = event.data.object
        sub = Subscription.objects.filter(stripe_subscription_id=sub_data.id).first()
        if sub:
            sub.plan = 'free'
            sub.status = 'cancelled'
            sub.stripe_subscription_id = ''
            sub.cancel_at_period_end = False
            sub.save()

    elif event.type == 'invoice.payment_succeeded':
        invoice = event.data.object
        sub = Subscription.objects.filter(stripe_customer_id=invoice.customer).first()
        if sub:
            interval_word = 'Yearly' if sub.billing_interval == 'year' else 'Monthly'
            PaymentHistory.objects.create(
                organization=sub.organization,
                amount=invoice.amount_paid / 100,
                plan=sub.plan,
                stripe_invoice_id=invoice.id,
                stripe_payment_id=invoice.payment_intent or '',
                description=f'{interval_word} {sub.plan.title()} plan renewal',
            )

    return HttpResponse(status=200)


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

    # All orgs with stats
    orgs = Organization.objects.all().annotate(
        total_visitors=Count('visitors', distinct=True),
        monthly_visitors=Count('visitors', filter=Q(visitors__first_visit__gte=month_start), distinct=True),
        online_visitors=Count('visitors', filter=Q(visitors__last_seen__gte=last_30_min), distinct=True),
        total_chats=Count('chat_rooms', distinct=True),
        active_chats=Count('chat_rooms', filter=Q(chat_rooms__status__in=['waiting', 'active']), distinct=True),
        total_agents=Count('agents', distinct=True),
        avg_rating=Avg('chat_rooms__rating', filter=Q(chat_rooms__rating__isnull=False)),
    )

    # Global stats
    total_orgs = Organization.objects.count()
    total_users = User.objects.count()
    total_visitors_global = Visitor.objects.count()
    total_chats_global = ChatRoom.objects.count()
    online_now = Visitor.objects.filter(last_seen__gte=last_30_min).count()

    # Plan distribution
    plan_dist = {}
    for sub in Subscription.objects.all():
        plan_dist[sub.plan] = plan_dist.get(sub.plan, 0) + 1

    # Revenue
    from tracker.core.models import PaymentHistory, Coupon
    from django.db.models import Sum
    total_revenue = PaymentHistory.objects.aggregate(total=Sum('amount'))['total'] or 0
    monthly_revenue = PaymentHistory.objects.filter(created_at__gte=month_start).aggregate(total=Sum('amount'))['total'] or 0

    # All payments
    all_payments = PaymentHistory.objects.select_related('organization').order_by('-created_at')[:50]

    # Recent signups
    recent_users = User.objects.order_by('-date_joined')[:10]

    # Active coupons
    coupons = Coupon.objects.all()

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
    """Set the active website filter in session (AJAX)."""
    if request.method == 'POST':
        data = json.loads(request.body) if request.body else {}
        website_id = data.get('website_id')
        if website_id == 'all' or website_id is None:
            request.session.pop('selected_website_id', None)
        else:
            request.session['selected_website_id'] = int(website_id)
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'POST required'}, status=405)


@login_required
def website_manage_view(request):
    """CRUD for websites - owner/admin only."""
    org = get_user_org(request.user)
    profile = getattr(request.user, 'agent_profile', None)
    is_owner = bool(request.user.is_superuser or (profile and profile.role in ('owner', 'admin')))
    if not is_owner:
        return redirect('dashboard:home')

    if request.method == 'POST':
        data = json.loads(request.body) if request.body else {}
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
            ws = Website.objects.create(organization=org, name=name, domain=domain)
            # Grant all existing agents access to new website
            for agent in AgentProfile.objects.filter(organization=org):
                AgentWebsiteAccess.objects.get_or_create(agent=agent, website=ws)
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
            ws.save()
            return JsonResponse({'status': 'ok'})

        return JsonResponse({'error': 'Invalid action'}, status=400)

    websites = Website.objects.filter(organization=org)
    base_url = request.build_absolute_uri('/').rstrip('/')
    host = request.get_host().split(':')[0]
    if host not in ('localhost', '127.0.0.1') and base_url.startswith('http://'):
        base_url = 'https://' + base_url[len('http://'):]

    return render(request, 'dashboard/website_manage.html', {
        'websites': websites,
        'base_url': base_url,
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

