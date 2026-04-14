from datetime import timedelta
from django.utils import timezone
from django.contrib.auth.models import User
from django.db.models import Count, Q
from tracker.chat.models import ChatRoom


def close_stale_chats(inactive_minutes=30):
    """Auto-close waiting/active chats that have been inactive for too long.
    Also sends WebSocket notification to visitors in those chats."""
    cutoff = timezone.now() - timedelta(minutes=inactive_minutes)
    stale_rooms = list(ChatRoom.objects.filter(status__in=['waiting', 'active'], updated_at__lt=cutoff).values_list('room_id', flat=True))
    count = ChatRoom.objects.filter(room_id__in=stale_rooms).update(status='closed', closed_at=timezone.now())

    # Notify visitors via WebSocket that chat was auto-closed
    if stale_rooms:
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            for rid in stale_rooms:
                async_to_sync(channel_layer.group_send)(
                    f'chat_{rid}',
                    {
                        'type': 'chat_closed',
                        'message': 'Chat closed due to inactivity.',
                    }
                )
        except Exception:
            pass
    return count


def auto_assign_agent(room):
    """
    Assign chat to available agent within the same org based on assignment rule.
    Rules: least_busy (default), round_robin, manual.
    Returns assigned user or None.
    """
    org = room.organization
    if not org:
        return None

    # Check assignment rule
    assign_rule = getattr(org, 'chat_assign_rule', 'least_busy')
    if assign_rule == 'manual':
        return None  # Don't auto-assign, agent must pick manually

    candidates = (
        User.objects.filter(
            is_active=True,
            agent_profile__isnull=False,
            agent_profile__is_available=True,
            agent_profile__organization=org,
        )
        .annotate(active_count=Count('chat_rooms', filter=Q(chat_rooms__status='active')))
    )

    if assign_rule == 'round_robin':
        # Round robin: pick agent who handled a chat LEAST recently
        from django.db.models import Max
        candidates = candidates.annotate(
            last_assigned=Max('chat_rooms__created_at')
        ).order_by('last_assigned', 'id')
    else:
        # Least busy: pick agent with fewest active chats
        candidates = candidates.order_by('active_count', 'id')

    for user in candidates:
        profile = getattr(user, 'agent_profile', None)
        if not profile:
            continue
        if user.active_count < profile.max_chats:
            room.agent = user
            room.status = 'active'
            room.save(update_fields=['agent', 'status', 'updated_at'])
            return user
    return None


def check_sla_breaches(sla_minutes=5, org_id=None):
    """Check for waiting chats that exceeded SLA response time and notify."""
    from tracker.chat.notifications import send_dashboard_notification
    from django.core.cache import cache

    cutoff = timezone.now() - timedelta(minutes=sla_minutes)
    breached = ChatRoom.objects.filter(
        status='waiting',
        created_at__lt=cutoff,
    )
    if org_id:
        breached = breached.filter(organization_id=org_id)
    breached = breached.select_related('organization')

    for room in breached:
        cache_key = f'sla_breach_notified:{room.room_id}'
        if not cache.get(cache_key):
            cache.set(cache_key, True, 1800)
            wait_mins = int((timezone.now() - room.created_at).total_seconds() / 60)
            send_dashboard_notification(
                org_id=room.organization_id,
                category='sla_breach',
                title='SLA Breach',
                body=f'{room.visitor_name} waiting {wait_mins}+ mins with no agent response',
                severity='error',
                url=f'/dashboard/chats/{room.room_id}/',
                sound=True,
            )
