"""Send auto-responder messages to chats waiting longer than the org's threshold.

Schedule this on a 1-minute cron (Render Cron Job, system crontab, or Heroku
scheduler):

    * * * * *  /path/to/python /path/to/manage.py send_auto_replies

The command is idempotent — once a chat receives the auto-response it's marked
in cache for the rest of the day so it doesn't get spammed every minute.
"""
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.db.models import Max, Q
from django.utils import timezone

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from tracker.chat.models import ChatRoom, Message
from tracker.core.models import Organization


class Command(BaseCommand):
    help = "Send auto-responder messages to waiting chats older than the org threshold."

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be sent without actually creating messages.',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        now = timezone.now()
        sent = 0

        orgs = Organization.objects.filter(auto_responder_enabled=True)
        for org in orgs:
            delay = max(int(org.auto_responder_delay or 0), 1)
            threshold = now - timedelta(minutes=delay)
            # Candidate chats: waiting/active, no agent reply yet, last visitor
            # message older than the threshold. We compare against the most
            # recent message timestamp so reopened chats don't immediately fire.
            chats = (
                ChatRoom.objects
                .filter(organization=org, status__in=('waiting', 'active'))
                .annotate(last_msg_at=Max('messages__timestamp'))
                .filter(Q(last_msg_at__lte=threshold) | Q(last_msg_at__isnull=True, created_at__lte=threshold))
            )
            for chat in chats:
                cache_key = f'auto_reply_sent:{chat.id}:{now.date().isoformat()}'
                if cache.get(cache_key):
                    continue
                # Skip if the most recent message is already from an agent (they replied)
                last_msg = chat.messages.order_by('-timestamp').first()
                if last_msg and last_msg.sender_type == 'agent':
                    continue
                if dry:
                    self.stdout.write(f'[dry-run] would send to chat #{chat.room_id} (org={org.slug})')
                    sent += 1
                    continue
                msg = Message.objects.create(
                    room=chat,
                    sender_type='system',
                    sender_name=org.name,
                    content=org.auto_responder_message,
                    msg_type='text',
                )
                cache.set(cache_key, True, timeout=24 * 3600)
                self._broadcast(chat.room_id, msg)
                sent += 1

        verb = 'would send' if dry else 'sent'
        self.stdout.write(self.style.SUCCESS(f'{verb} {sent} auto-responder message(s).'))

    def _broadcast(self, room_id, msg):
        # Shape must match chat.consumers.ChatConsumer.chat_message — it reads
        # `message` as the text payload and the rest as flat fields.
        try:
            async_to_sync(get_channel_layer().group_send)(
                f'chat_{room_id}',
                {
                    'type': 'chat_message',
                    'message': msg.content,
                    'sender_type': msg.sender_type,
                    'sender_name': msg.sender_name,
                    'msg_type': msg.msg_type,
                    'file_url': '',
                    'file_name': '',
                    'timestamp': msg.timestamp.isoformat(),
                },
            )
        except Exception:
            # WebSocket layer may be unavailable in some environments — message
            # is still saved and visible on next refresh, so don't fail the run.
            pass
