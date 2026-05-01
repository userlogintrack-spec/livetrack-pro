from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tracker.visitors.models import (
    PageView, CustomEvent, ClickData, ScrollData, JSError,
    FrustrationSignal, SessionRecording,
)


class Command(BaseCommand):
    help = (
        'Delete high-volume tracking rows (pageviews, events, recordings) older '
        'than --days. Run nightly to keep the DB bounded. Use --dry-run first.'
    )

    BATCH_SIZE = 5000

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=90,
                            help='Delete rows older than this many days (default: 90)')
        parser.add_argument('--dry-run', action='store_true',
                            help='Count rows that would be deleted without deleting')

    def handle(self, *args, **opts):
        days = opts['days']
        cutoff = timezone.now() - timedelta(days=days)
        dry = opts['dry_run']

        targets = [
            ('PageView', PageView, 'timestamp'),
            ('CustomEvent', CustomEvent, 'timestamp'),
            ('ClickData', ClickData, 'timestamp'),
            ('ScrollData', ScrollData, 'timestamp'),
            ('JSError', JSError, 'timestamp'),
            ('FrustrationSignal', FrustrationSignal, 'timestamp'),
            ('SessionRecording', SessionRecording, 'created_at'),
        ]

        self.stdout.write(f'cutoff: {cutoff.isoformat()} ({days}d ago)')
        if dry:
            self.stdout.write(self.style.WARNING('DRY RUN — no rows will be deleted'))

        total = 0
        for label, model, field in targets:
            qs = model.objects.filter(**{f'{field}__lt': cutoff})
            count = qs.count()
            total += count
            if not count:
                self.stdout.write(f'  {label}: 0')
                continue
            if dry:
                self.stdout.write(f'  {label}: {count} would be deleted')
                continue
            deleted = self._batch_delete(qs)
            self.stdout.write(self.style.SUCCESS(f'  {label}: {deleted} deleted'))

        self.stdout.write(self.style.SUCCESS(f'done — {total} total'))

    def _batch_delete(self, qs):
        """Delete in chunks so a single huge purge doesn't hold a long transaction
        and lock production tables."""
        deleted = 0
        while True:
            ids = list(qs.values_list('pk', flat=True)[:self.BATCH_SIZE])
            if not ids:
                break
            with transaction.atomic():
                n, _ = qs.model.objects.filter(pk__in=ids).delete()
            deleted += n
            if n < self.BATCH_SIZE:
                break
        return deleted
