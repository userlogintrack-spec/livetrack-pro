"""Centralized real-time notification helper.

Usage:
    from tracker.chat.notifications import send_dashboard_notification
    send_dashboard_notification(
        org_id=org.id,
        category='new_chat',
        title='New Chat',
        body='Visitor started a chat',
        severity='warning',
        url='/dashboard/chats/abc123/',
    )
"""
import logging
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

logger = logging.getLogger('tracker')

# Map notification categories to Website model boolean fields.
# If the field is False for a website, that notification is suppressed.
CATEGORY_TO_WEBSITE_FLAG = {
    'new_visitor': 'notify_new_visitor',
    'new_chat': 'notify_new_chat',
    'offline_message': 'notify_offline_msg',
    'js_error': 'notify_error',
}


def send_dashboard_notification(org_id, category, title, body,
                                severity='info', url='', sound=True,
                                website=None):
    """Send a real-time notification to all dashboard agents in the org.

    Args:
        org_id: Organization ID (int)
        category: One of new_visitor, new_chat, offline_message, hot_lead,
                  no_agents, sla_breach, js_error
        title: Short notification title
        body: Description text
        severity: info | warning | error
        url: Optional click target URL
        sound: Whether to play sound on client (default True)
        website: Optional Website instance - used to check per-site prefs
    """
    # Respect per-website notification preferences
    if website:
        flag = CATEGORY_TO_WEBSITE_FLAG.get(category)
        if flag and not getattr(website, flag, True):
            return

    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        group = f'dashboard_updates_{org_id}'
        async_to_sync(channel_layer.group_send)(group, {
            'type': 'notification',
            'category': category,
            'title': title,
            'body': body,
            'severity': severity,
            'url': url,
            'sound': sound,
        })
    except Exception:
        logger.exception('Failed to send dashboard notification: %s', category)
