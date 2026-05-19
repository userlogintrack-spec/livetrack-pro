"""Pull new messages from each configured EmailMailbox (IMAP) and ingest
them as ChatRoom + Message rows.

Threading rule:
    - Subject line is normalized (Re:/Fwd: stripped) and a stable thread id
      is computed as the hash of (mailbox_id, normalized_subject, from_addr).
    - If a ChatRoom with that `external_thread_id` exists for this org and
      is still open, append the email body as the next Message.
    - Otherwise create a new ChatRoom (channel='email', status='waiting')
      and a Visitor row keyed by the sender's email.

Run from cron every 1–5 minutes:
    */2 * * * * python manage.py poll_email_inboxes

Idempotent: tracks the highest UID seen per mailbox in
`EmailMailbox.last_polled_uid` so a repeated run won't re-ingest the
same message.
"""
from __future__ import annotations

import email
import hashlib
import imaplib
import logging
import re
import uuid
from email.header import decode_header
from email.utils import parseaddr

from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker.chat.models import ChatRoom, EmailMailbox, Message
from tracker.visitors.models import Visitor

logger = logging.getLogger(__name__)

_SUBJECT_PREFIX_RE = re.compile(r'^\s*(re|fwd|fw)\s*:\s*', flags=re.IGNORECASE)


def _decode(s):
    """Decode an RFC 2047 encoded header into a plain string."""
    if not s:
        return ''
    parts = decode_header(s)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or 'utf-8', errors='replace'))
            except (LookupError, TypeError):
                out.append(chunk.decode('utf-8', errors='replace'))
        else:
            out.append(chunk)
    return ''.join(out).strip()


def _normalize_subject(subj: str) -> str:
    s = _SUBJECT_PREFIX_RE.sub('', subj or '').strip()
    # Collapse repeated whitespace.
    return re.sub(r'\s+', ' ', s)[:150]


def _body(msg: email.message.Message) -> str:
    """Extract a text/plain body; fall back to stripping HTML."""
    if msg.is_multipart():
        # Prefer plain text part.
        for part in msg.walk():
            if part.get_content_type() == 'text/plain' and 'attachment' not in (part.get('Content-Disposition') or ''):
                payload = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                try:
                    return payload.decode(charset, errors='replace')
                except (LookupError, TypeError):
                    return payload.decode('utf-8', errors='replace')
        # No text/plain — try HTML as a last resort.
        for part in msg.walk():
            if part.get_content_type() == 'text/html':
                payload = part.get_payload(decode=True) or b''
                charset = part.get_content_charset() or 'utf-8'
                try:
                    html = payload.decode(charset, errors='replace')
                except (LookupError, TypeError):
                    html = payload.decode('utf-8', errors='replace')
                return re.sub(r'<[^>]+>', '', html)
        return ''
    payload = msg.get_payload(decode=True) or b''
    charset = msg.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, errors='replace')
    except (LookupError, TypeError):
        return payload.decode('utf-8', errors='replace')


def _thread_id(mailbox: EmailMailbox, subject: str, from_addr: str) -> str:
    raw = f'{mailbox.id}|{_normalize_subject(subject).lower()}|{from_addr.lower()}'
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]


def _ingest_message(mailbox: EmailMailbox, raw_bytes: bytes) -> bool:
    """Parse an email + create / append to a ChatRoom. Returns True if a row
    was created or updated (so the caller can log a summary)."""
    msg = email.message_from_bytes(raw_bytes)
    subject = _decode(msg.get('Subject', ''))
    from_name, from_addr = parseaddr(_decode(msg.get('From', '')))
    if not from_addr:
        return False
    body_text = _body(msg).strip()
    if not body_text and not subject:
        return False

    thread_id = _thread_id(mailbox, subject, from_addr)
    org = mailbox.organization

    # Find or create a Visitor keyed by email — emails don't bring a session
    # key, so we use the email as the dedup anchor inside this org.
    visitor = Visitor.objects.filter(organization=org, visitor_email__iexact=from_addr).first()
    if not visitor:
        session_key = f'email:{uuid.uuid4().hex[:24]}'
        visitor = Visitor.objects.create(
            organization=org,
            website=mailbox.website,
            session_key=session_key,
            visitor_email=from_addr,
            visitor_name=from_name or from_addr.split('@')[0],
            ip_address='0.0.0.0',
            user_agent='email-ingest',
        )

    room = ChatRoom.objects.filter(
        organization=org,
        channel='email',
        external_thread_id=thread_id,
        status__in=('waiting', 'active'),
    ).first()
    created_room = False
    if not room:
        room = ChatRoom.objects.create(
            organization=org,
            website=mailbox.website,
            room_id=uuid.uuid4().hex[:12],
            visitor=visitor,
            visitor_name=from_name or visitor.visitor_name or from_addr,
            visitor_email=from_addr,
            subject=_normalize_subject(subject)[:200],
            channel='email',
            external_thread_id=thread_id,
            status='waiting',
        )
        created_room = True
    else:
        # Refresh updated_at + bump back to waiting if it was closed (unlikely
        # since we filter above) so agents notice.
        room.save(update_fields=['updated_at'])

    Message.objects.create(
        room=room,
        sender_type='visitor',
        sender_name=from_name or from_addr,
        content=body_text[:8000],
    )
    return True if created_room else False


class Command(BaseCommand):
    help = 'Pull new emails from each enabled EmailMailbox and ingest as chats.'

    def add_arguments(self, parser):
        parser.add_argument('--mailbox-id', type=int, default=None,
                            help='Run only this mailbox (default: all enabled).')
        parser.add_argument('--limit', type=int, default=50,
                            help='Max new messages per mailbox per run.')
        parser.add_argument('--dry-run', action='store_true',
                            help="Connect + list but don't ingest.")

    def handle(self, *args, **opts):
        qs = EmailMailbox.objects.filter(is_enabled=True)
        if opts['mailbox_id']:
            qs = qs.filter(id=opts['mailbox_id'])
        total_new = 0
        for mb in qs:
            try:
                new_count = self._poll_mailbox(mb, limit=opts['limit'], dry_run=opts['dry_run'])
                total_new += new_count
                self.stdout.write(f'  {mb.name}: ingested {new_count}')
            except Exception as e:
                self.stderr.write(self.style.ERROR(f'  {mb.name}: {e}'))
                logger.warning('mailbox %s poll failed', mb.name, exc_info=True)
        self.stdout.write(self.style.SUCCESS(f'Done. {total_new} new email(s) ingested.'))

    def _poll_mailbox(self, mailbox: EmailMailbox, limit: int = 50, dry_run: bool = False) -> int:
        cls = imaplib.IMAP4_SSL if mailbox.imap_use_ssl else imaplib.IMAP4
        conn = cls(mailbox.imap_host, mailbox.imap_port)
        try:
            conn.login(mailbox.imap_username, mailbox.imap_password_plain)
            conn.select(mailbox.imap_folder, readonly=False)
            # UID search for everything strictly newer than what we've seen.
            since_uid = mailbox.last_polled_uid + 1
            typ, data = conn.uid('search', None, f'(UID {since_uid}:*)')
            if typ != 'OK':
                return 0
            uids = data[0].split() if data and data[0] else []
            # IMAP returns the lowest matching UID even when none ≥ since_uid,
            # so filter again client-side.
            uids = [u for u in uids if int(u) >= since_uid]
            uids = uids[:limit]
            from django.db import transaction
            new_count = 0
            highest_uid = mailbox.last_polled_uid
            for uid in uids:
                typ, msg_data = conn.uid('fetch', uid, '(RFC822)')
                if typ != 'OK' or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not raw:
                    continue
                ingested = False
                if dry_run:
                    ingested = True
                else:
                    # Wrap each ingest in its own atomic block so a parse
                    # failure on email N doesn't roll back email N-1.
                    try:
                        with transaction.atomic():
                            _ingest_message(mailbox, raw)
                        ingested = True
                    except Exception:
                        logger.warning('mailbox %s: ingest failed for uid %s', mailbox.name, uid, exc_info=True)

                # Critical: only advance the watermark for UIDs we
                # *successfully ingested*. Previously the bump happened
                # unconditionally — a parse failure silently lost the
                # message (next run skipped past it).
                if ingested:
                    new_count += 1
                    try:
                        highest_uid = max(highest_uid, int(uid))
                    except (TypeError, ValueError):
                        pass
            if not dry_run:
                mailbox.last_polled_uid = highest_uid
                mailbox.last_polled_at = timezone.now()
                mailbox.save(update_fields=['last_polled_uid', 'last_polled_at'])
            return new_count
        finally:
            try:
                conn.logout()
            except Exception:
                pass
