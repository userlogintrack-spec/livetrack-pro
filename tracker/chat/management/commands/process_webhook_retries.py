"""Re-fire webhooks whose previous attempts failed.

Schedule on a 1-minute cron so failed deliveries with `next_retry_at <= now`
get picked up and retried. Idempotent — `_attempt_webhook_delivery` updates
the delivery row in place.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker.chat.models import WebhookDelivery


class Command(BaseCommand):
    help = 'Retry webhook deliveries whose next_retry_at has elapsed.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=200,
                            help='Max deliveries to attempt this run.')

    def handle(self, *args, **options):
        from tracker.dashboard.views import _attempt_webhook_delivery
        now = timezone.now()
        qs = (
            WebhookDelivery.objects
            .filter(status='pending', next_retry_at__lte=now)
            .select_related('webhook', 'webhook__organization')
            .order_by('next_retry_at')[:options['limit']]
        )
        ok = bad = 0
        for d in qs:
            if _attempt_webhook_delivery(d):
                ok += 1
            else:
                bad += 1
        self.stdout.write(self.style.SUCCESS(f'Retried {ok + bad} deliveries: {ok} success, {bad} still failing.'))
