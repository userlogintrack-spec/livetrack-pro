"""Outbound SMTP for email-channel chats.

When an agent replies to a chat where `room.channel == 'email'`, we ship
that reply via the mailbox's SMTP credentials instead of broadcasting
over the chat WebSocket. Threading headers are set so the visitor's mail
client groups replies under the original conversation.
"""
from __future__ import annotations

import email.utils
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger(__name__)


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

    subject = room.subject or 'Re: your message'
    if not subject.lower().startswith('re:'):
        subject = f'Re: {subject}'

    from_display = f'{(mb.from_name or agent_name or mb.from_email)} <{mb.from_email}>'
    msg = MIMEMultipart('alternative')
    msg['From'] = from_display
    msg['To'] = room.visitor_email
    msg['Subject'] = subject
    msg['Message-ID'] = email.utils.make_msgid(domain=mb.from_email.split('@', 1)[-1])
    # Reference the thread so the visitor's client groups replies.
    if room.external_thread_id:
        msg['References'] = f'<{room.external_thread_id}@livetrack.local>'
        msg['In-Reply-To'] = f'<{room.external_thread_id}@livetrack.local>'

    body_text = body_text or ''
    # Plain text first (most compatible), then a soft HTML version.
    msg.attach(MIMEText(body_text, 'plain', 'utf-8'))
    html_body = '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.6;color:#1f2937;">' \
                + body_text.replace('\n', '<br>') + '</div>'
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
