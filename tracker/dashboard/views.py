import csv
import logging
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db.models import Count, Q, Avg, Sum, F
from django.views.decorators.csrf import csrf_exempt
from datetime import timedelta
import json
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from tracker.visitors.models import Visitor, PageView
from tracker.chat.models import ChatRoom, Message, AgentProfile, OfflineMessage, CannedResponse, VisitorNote, InternalNote, Webhook, ActivityLog, ChatLabel, SavedReply
from tracker.chat.security import create_ws_token
from tracker.chat.utils import close_stale_chats
from tracker.core.models import WebsiteSettings, Organization
from tracker.core.views import get_user_org

logger = logging.getLogger(__name__)


@login_required
def dashboard_home(request):
    org = get_user_org(request.user)
    close_stale_chats(inactive_minutes=30)
    now = timezone.now()
    sla_minutes = int(getattr(settings, 'CHAT_SLA_MINUTES', 5))
    sla_cutoff = now - timedelta(minutes=sla_minutes)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    last_30_min = now - timedelta(minutes=30)

    visitors_qs = Visitor.objects.filter(organization=org)
    chats_qs = ChatRoom.objects.filter(organization=org)
    pageviews_qs = PageView.objects.filter(visitor__organization=org)

    total_visitors = visitors_qs.count()
    online_visitors = visitors_qs.filter(last_seen__gte=last_30_min).count()
    total_chats = chats_qs.count()
    active_chats = chats_qs.filter(status__in=['waiting', 'active']).count()
    today_visitors = visitors_qs.filter(first_visit__gte=today_start).count()
    today_chats = chats_qs.filter(created_at__gte=today_start).count()
    today_page_views = pageviews_qs.filter(timestamp__gte=today_start).count()
    unread_offline = OfflineMessage.objects.filter(organization=org, is_read=False).count()

    waiting_chats = chats_qs.filter(status='waiting').select_related('visitor')
    recent_visitors = visitors_qs.filter(last_seen__gte=last_30_min)[:10]

    browser_stats = visitors_qs.values('browser').annotate(count=Count('id')).order_by('-count')[:5]
    device_stats = visitors_qs.values('device_type').annotate(count=Count('id')).order_by('-count')
    referrer_stats = visitors_qs.values('referrer_source').annotate(count=Count('id')).order_by('-count')[:5]

    hourly_data = []
    for hour in range(24):
        hour_start = today_start.replace(hour=hour)
        hour_end = hour_start + timedelta(hours=1)
        count = pageviews_qs.filter(timestamp__gte=hour_start, timestamp__lt=hour_end).values('visitor').distinct().count()
        hourly_data.append({'hour': f'{hour:02d}:00', 'count': count})

    recent_chats = chats_qs.select_related('visitor', 'agent')[:5]
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
        'referrer_stats': list(referrer_stats),
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
        # Agent leaderboard
        'agent_leaderboard': User.objects.filter(
            agent_profile__organization=org
        ).annotate(
            chats_handled=Count('chat_rooms', filter=Q(chat_rooms__status='closed', chat_rooms__organization=org)),
        ).order_by('-chats_handled')[:5],
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

    from django.db.models import Exists, OuterRef, Subquery
    chats = ChatRoom.objects.filter(organization=org).select_related('visitor', 'agent').annotate(
        unread_count=Count('messages', filter=Q(messages__sender_type='visitor', messages__is_read=False)),
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

    return render(request, 'dashboard/chat_list.html', {
        'chats': chats,
        'current_filter': status_filter,
        'search_q': search_q,
        'tag_filter': tag_filter,
        'priority_filter': priority_filter,
        'date_from': date_from,
        'date_to': date_to,
        'sla_cutoff': sla_cutoff,
        'sla_minutes': sla_minutes,
    })


@login_required
def chat_room_view(request, room_id):
    org = get_user_org(request.user)
    room = get_object_or_404(ChatRoom, room_id=room_id, organization=org)
    visitor = room.visitor
    visitor_pages = visitor.page_views.all()[:20]
    visitor_notes = visitor.agent_notes.all()[:10]
    canned_responses = CannedResponse.objects.filter(Q(is_global=True) | Q(created_by=request.user))

    # Auto-join waiting chat (unless manual assignment rule)
    manual_only = org and org.chat_assign_rule == 'manual'
    join_requested = request.GET.get('join') == '1'
    if room.status == 'waiting' and request.user.is_authenticated and (not manual_only or join_requested):
        room.agent = request.user
        room.status = 'active'
        room.save()
        # Log activity + send "Agent joined" system message
        agent_name = request.user.get_full_name() or request.user.username
        _log_activity(org, request.user, 'agent.joined', f'{agent_name} joined chat #{room.room_id}', 'chat', room.room_id)
        Message.objects.create(
            room=room, sender_type='system', sender_name='System',
            content=f'{agent_name} joined the chat.',
        )
        # Notify chat room via WebSocket
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f'chat_{room.room_id}',
            {
                'type': 'chat_message',
                'message': f'{agent_name} joined the chat.',
                'sender_type': 'system',
                'sender_name': 'System',
                'msg_type': 'text',
                'file_url': '',
                'file_name': '',
                'timestamp': timezone.now().isoformat(),
            }
        )

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

    visitors = Visitor.objects.filter(organization=org).annotate(
        page_count=Count('page_views'),
        chat_count=Count('chat_rooms'),
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

    return render(request, 'dashboard/visitor_list.html', {
        'visitors': visitors[:100],
        'current_filter': filter_type,
        'last_30_min': last_30_min,
        'search_q': search_q,
        'date_from': date_from,
        'date_to': date_to,
    })


@login_required
def visitor_detail(request, visitor_id):
    org = get_user_org(request.user)
    visitor = get_object_or_404(Visitor, id=visitor_id, organization=org)
    page_views = visitor.page_views.all()[:50]
    chat_rooms = visitor.chat_rooms.all()
    notes = visitor.agent_notes.all()
    return render(request, 'dashboard/visitor_detail.html', {
        'visitor': visitor,
        'page_views': page_views,
        'chat_rooms': chat_rooms,
        'notes': notes,
    })


@login_required
def api_stats(request):
    org = get_user_org(request.user)
    close_stale_chats(inactive_minutes=30)
    now = timezone.now()
    last_30_min = now - timedelta(minutes=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    return JsonResponse({
        'online_visitors': Visitor.objects.filter(organization=org, last_seen__gte=last_30_min).count(),
        'active_chats': ChatRoom.objects.filter(organization=org, status__in=['waiting', 'active']).count(),
        'active_only_chats': ChatRoom.objects.filter(organization=org, status='active').count(),
        'waiting_chats': ChatRoom.objects.filter(organization=org, status='waiting').count(),
        'unread_messages': Message.objects.filter(
            room__organization=org,
            room__status__in=['waiting', 'active'],
            sender_type='visitor',
            is_read=False,
        ).count(),
        'today_visitors': Visitor.objects.filter(organization=org, first_visit__gte=today_start).count(),
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
    """Export all chats as CSV."""
    org = get_user_org(request.user)
    date_from = request.GET.get('from', '').strip()
    date_to = request.GET.get('to', '').strip()
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="chats_export.csv"'

    writer = csv.writer(response)
    writer.writerow(['Room ID', 'Visitor', 'Email', 'Agent', 'Status', 'Subject', 'Rating', 'Tags', 'Messages', 'Created', 'Closed'])

    chats_qs = ChatRoom.objects.filter(organization=org).select_related('agent')
    if date_from:
        chats_qs = chats_qs.filter(created_at__date__gte=date_from)
    if date_to:
        chats_qs = chats_qs.filter(created_at__date__lte=date_to)

    for c in chats_qs:
        writer.writerow([
            c.room_id, c.visitor_name, c.visitor_email,
            c.agent.get_full_name() if c.agent else '-',
            c.status, c.subject, c.rating or '-', c.tags,
            c.message_count, c.created_at.strftime('%Y-%m-%d %H:%M'),
            c.closed_at.strftime('%Y-%m-%d %H:%M') if c.closed_at else '-',
        ])

    return response


@login_required
def offline_messages_view(request):
    """View offline messages."""
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
    """Create or update website/widget settings from dashboard."""
    org = get_user_org(request.user)
    saved = False
    error = ''

    if request.method == 'POST':
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
            org.save()
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

        if not username or not password:
            error = 'Username and password are required.'
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
            AgentProfile.objects.create(
                user=user,
                max_chats=max(1, max_chats),
                is_available=is_available,
                organization=org,
                role='agent',
            )
            created = True

    agents = User.objects.filter(agent_profile__isnull=False, agent_profile__organization=org).select_related('agent_profile').order_by('username')
    return render(request, 'dashboard/add_agent.html', {
        'created': created,
        'error': error,
        'agents': agents,
        'org': org,
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
    html += '<div style="text-align:center;margin-top:30px;color:#9ca3af;font-size:11px;">Exported from LiveTrack</div></body></html>'

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

    return render(request, 'dashboard/analytics.html', {
        'daily_chats': daily_chats,
        'daily_csat': daily_csat,
        'status_counts': status_counts,
        'country_stats': country_stats,
        'hourly_chats': hourly_chats,
        'total_chats': total_chats,
        'avg_rating': avg_rating,
        'total_messages': total_messages,
    })


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

    _log_activity(org, request.user, f'bulk.{action}', f'Bulk {action} on {count} chats')
    return JsonResponse({'status': 'ok', 'affected': count})


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
