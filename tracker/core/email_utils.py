"""
Beautiful HTML email templates for LiveVisitorHub.
All emails share a consistent, modern design with the brand gradient.
"""
from django.core.mail import EmailMultiAlternatives
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


def _base_html(body_content, preview_text=''):
    """Wrap body_content in a beautiful, responsive HTML email shell."""
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>LiveVisitorHub</title>
<!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml></noscript><![endif]-->
<style>
  body, table, td, a {{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
  table, td {{ mso-table-lspace:0pt; mso-table-rspace:0pt; }}
  img {{ -ms-interpolation-mode:bicubic; border:0; outline:none; text-decoration:none; }}
  body {{ margin:0; padding:0; width:100%!important; -webkit-font-smoothing:antialiased; }}
  .wrapper {{ width:100%; table-layout:fixed; background-color:#f0f2f5; padding:40px 0; }}
  .main {{ max-width:600px; margin:0 auto; background:#ffffff; border-radius:16px; overflow:hidden; box-shadow:0 4px 24px rgba(0,0,0,0.06); }}
  @media only screen and (max-width:620px) {{
    .main {{ margin:0 12px!important; border-radius:12px!important; }}
    .content {{ padding:28px 20px!important; }}
    .header {{ padding:32px 20px 24px!important; }}
  }}
</style>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;">
<div style="display:none;max-height:0;overflow:hidden;">{preview_text}</div>
<div class="wrapper" style="width:100%;table-layout:fixed;background-color:#f0f2f5;padding:40px 0;">
<table class="main" width="600" cellpadding="0" cellspacing="0" role="presentation" style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.06);">
  <!-- Header with gradient -->
  <tr>
    <td class="header" style="background:linear-gradient(135deg,#7c3aed 0%,#a78bfa 50%,#6d28d9 100%);padding:40px 40px 32px;text-align:center;">
      <div style="display:inline-block;width:48px;height:48px;background:rgba(255,255,255,0.2);border-radius:14px;line-height:48px;margin-bottom:12px;">
        <span style="font-size:22px;color:#ffffff;">&#9672;</span>
      </div>
      <div style="font-size:22px;font-weight:800;color:#ffffff;letter-spacing:-0.3px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">LiveVisitorHub</div>
    </td>
  </tr>
  <!-- Body content -->
  <tr>
    <td class="content" style="padding:36px 40px 40px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#1e293b;font-size:15px;line-height:1.7;">
      {body_content}
    </td>
  </tr>
  <!-- Footer -->
  <tr>
    <td style="padding:0 40px 32px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
      <div style="border-top:1px solid #e2e8f0;padding-top:24px;text-align:center;">
        <div style="font-size:12px;color:#94a3b8;line-height:1.6;">
          &copy; LiveVisitorHub &middot; Real-time visitor tracking & live chat<br>
          <span style="color:#cbd5e1;">You received this email because of your LiveVisitorHub account.</span>
        </div>
      </div>
    </td>
  </tr>
</table>
</div>
</body>
</html>'''


def _button_html(url, label, color='#7c3aed'):
    """Generate a bulletproof CTA button that works in all email clients."""
    return f'''<table cellpadding="0" cellspacing="0" role="presentation" style="margin:28px auto;">
  <tr>
    <td style="background:{color};border-radius:10px;text-align:center;">
      <!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{url}" style="height:48px;v-text-anchor:middle;width:220px;" arcsize="21%" fillcolor="{color}"><w:anchorlock/><center style="font-size:15px;font-weight:700;color:#ffffff;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{label}</center></v:roundrect><![endif]-->
      <!--[if !mso]><!-->
      <a href="{url}" target="_blank" style="display:inline-block;background:{color};color:#ffffff;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;font-size:15px;font-weight:700;padding:14px 36px;border-radius:10px;text-decoration:none;letter-spacing:0.2px;">
        {label}
      </a>
      <!--<![endif]-->
    </td>
  </tr>
</table>'''


def _info_row(label, value):
    """A label: value row for data display."""
    return f'''<tr>
  <td style="padding:10px 16px;font-size:13px;color:#64748b;font-weight:600;border-bottom:1px solid #f1f5f9;width:140px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{label}</td>
  <td style="padding:10px 16px;font-size:14px;color:#1e293b;border-bottom:1px solid #f1f5f9;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{value}</td>
</tr>'''


def _card_html(content, bg='#f8fafc', border='#e2e8f0'):
    """A rounded card container for grouping content."""
    return f'<div style="background:{bg};border:1px solid {border};border-radius:12px;padding:20px;margin:20px 0;">{content}</div>'


def _stat_box(value, label, color='#7c3aed'):
    """A stat display box (e.g., "142 Visitors")."""
    return f'''<td style="text-align:center;padding:16px;">
  <div style="font-size:28px;font-weight:800;color:{color};font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;line-height:1;">{value}</div>
  <div style="font-size:11px;font-weight:600;color:#94a3b8;text-transform:uppercase;letter-spacing:0.5px;margin-top:6px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{label}</div>
</td>'''


def send_html_email(subject, plain_text, html_content, from_email, to_list, fail_silently=False):
    """Send an email with both HTML and plain-text versions."""
    msg = EmailMultiAlternatives(subject, plain_text, from_email, to_list)
    msg.attach_alternative(html_content, 'text/html')
    msg.send(fail_silently=fail_silently)


# ═══════════════════════════════════════════════════════════
# EMAIL BUILDERS
# ═══════════════════════════════════════════════════════════

def send_welcome_email(user, login_url, dashboard_url):
    """Beautiful welcome email for new users."""
    name = user.first_name or user.username
    plain = (
        f'Hi {name},\n\n'
        'Welcome to LiveVisitorHub! Your account is ready.\n'
        f'Login: {login_url}\n'
        f'Dashboard: {dashboard_url}\n\n'
        'Thanks,\nLiveVisitorHub Team'
    )
    body = f'''
<div style="text-align:center;margin-bottom:8px;">
  <div style="display:inline-block;width:64px;height:64px;background:linear-gradient(135deg,#10b981,#34d399);border-radius:50%;line-height:64px;margin-bottom:16px;">
    <span style="font-size:28px;color:#fff;">&#10003;</span>
  </div>
</div>
<h1 style="font-size:24px;font-weight:800;color:#1e293b;margin:0 0 8px;text-align:center;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Welcome, {name}!</h1>
<p style="text-align:center;color:#64748b;font-size:15px;margin:0 0 28px;">Your LiveVisitorHub account is ready to go.</p>

<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:24px;margin-bottom:24px;">
  <div style="font-size:13px;font-weight:700;color:#7c3aed;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:16px;">Get started in 3 steps</div>
  <table cellpadding="0" cellspacing="0" role="presentation" style="width:100%;">
    <tr>
      <td style="padding:8px 0;vertical-align:top;width:32px;">
        <div style="width:26px;height:26px;background:linear-gradient(135deg,#7c3aed,#a78bfa);border-radius:50%;text-align:center;line-height:26px;font-size:12px;font-weight:800;color:#fff;">1</div>
      </td>
      <td style="padding:8px 0 8px 12px;font-size:14px;color:#334155;">
        <strong>Add your website</strong> &mdash; Enter your domain in the dashboard
      </td>
    </tr>
    <tr>
      <td style="padding:8px 0;vertical-align:top;width:32px;">
        <div style="width:26px;height:26px;background:linear-gradient(135deg,#7c3aed,#a78bfa);border-radius:50%;text-align:center;line-height:26px;font-size:12px;font-weight:800;color:#fff;">2</div>
      </td>
      <td style="padding:8px 0 8px 12px;font-size:14px;color:#334155;">
        <strong>Embed tracking code</strong> &mdash; Paste the script tag on your site
      </td>
    </tr>
    <tr>
      <td style="padding:8px 0;vertical-align:top;width:32px;">
        <div style="width:26px;height:26px;background:linear-gradient(135deg,#7c3aed,#a78bfa);border-radius:50%;text-align:center;line-height:26px;font-size:12px;font-weight:800;color:#fff;">3</div>
      </td>
      <td style="padding:8px 0 8px 12px;font-size:14px;color:#334155;">
        <strong>Watch visitors live</strong> &mdash; See real-time data in your dashboard
      </td>
    </tr>
  </table>
</div>

{_button_html(dashboard_url, 'Open Dashboard &rarr;')}

<p style="text-align:center;font-size:13px;color:#94a3b8;margin:0;">Need help? Just reply to this email.</p>
'''
    html = _base_html(body, preview_text=f'Welcome to LiveVisitorHub, {name}! Your account is ready.')
    try:
        send_html_email(
            'Welcome to LiveVisitorHub',
            plain, html,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
    except Exception:
        logger.exception('Failed to send welcome email to %s', user.email)


def send_new_chat_notification(org, visitor_name, subject, room_id, dashboard_url):
    """Beautiful notification email when a new chat is initiated."""
    from django.utils.html import escape
    v_name = escape(visitor_name)
    subj_text = escape(subject or '-')

    plain = (
        f'New chat from {visitor_name}\n'
        f'Subject: {subject or "-"}\n'
        f'Room: {room_id}\n\n'
        f'Login to respond: {dashboard_url}'
    )
    body = f'''
<div style="text-align:center;margin-bottom:8px;">
  <div style="display:inline-block;width:56px;height:56px;background:linear-gradient(135deg,#f59e0b,#fbbf24);border-radius:50%;line-height:56px;margin-bottom:12px;">
    <span style="font-size:24px;color:#fff;">&#128172;</span>
  </div>
</div>
<h1 style="font-size:22px;font-weight:800;color:#1e293b;margin:0 0 6px;text-align:center;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">New Chat Request</h1>
<p style="text-align:center;color:#64748b;font-size:14px;margin:0 0 24px;">A visitor is waiting for your response</p>

<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;padding:20px;margin-bottom:24px;">
  <table cellpadding="0" cellspacing="0" role="presentation" style="width:100%;">
    {_info_row('Visitor', f'<strong>{v_name}</strong>')}
    {_info_row('Subject', subj_text)}
    {_info_row('Room ID', f'<code style="background:#f1f5f9;padding:2px 8px;border-radius:4px;font-size:12px;">{escape(str(room_id))}</code>')}
  </table>
</div>

{_button_html(dashboard_url, 'Reply to Chat &rarr;', '#f59e0b')}

<p style="text-align:center;font-size:12px;color:#94a3b8;margin:0;">Respond quickly for the best visitor experience.</p>
'''
    html = _base_html(body, preview_text=f'New chat from {visitor_name} on {org.name}')
    try:
        send_html_email(
            f'New chat from {visitor_name} - {org.name}',
            plain, html,
            settings.DEFAULT_FROM_EMAIL,
            [org.notify_email],
        )
    except Exception:
        logger.exception('Failed to send new-chat notification for room=%s', room_id)


def send_chat_transcript(org, room, messages_qs, to_email):
    """Beautiful chat transcript email."""
    from django.utils.html import escape

    # Build message bubbles
    bubbles = ''
    for msg in messages_qs:
        is_agent = msg.sender_type == 'agent'
        is_system = msg.sender_type == 'system'
        if is_system:
            bubbles += f'''
<div style="text-align:center;margin:8px 0;">
  <span style="display:inline-block;background:#f1f5f9;color:#94a3b8;font-size:11px;padding:4px 12px;border-radius:10px;">{escape(msg.content)}</span>
</div>'''
        else:
            align = 'right' if is_agent else 'left'
            bg = '#7c3aed' if is_agent else '#f1f5f9'
            color = '#ffffff' if is_agent else '#1e293b'
            name_color = '#7c3aed' if is_agent else '#64748b'
            bubbles += f'''
<div style="margin:6px 0;text-align:{align};">
  <div style="display:inline-block;max-width:80%;text-align:left;">
    <div style="font-size:10px;color:{name_color};font-weight:600;margin-bottom:2px;padding:0 4px;">{escape(msg.sender_name)} &middot; {msg.timestamp.strftime("%H:%M")}</div>
    <div style="display:inline-block;background:{bg};color:{color};padding:10px 14px;border-radius:{'12px 12px 4px 12px' if is_agent else '12px 12px 12px 4px'};font-size:13px;line-height:1.5;">{escape(msg.content)}</div>
  </div>
</div>'''

    # Plain text fallback
    lines = [f'Chat Transcript - {room.visitor_name}', f'Room: {room.room_id}', f'Date: {room.created_at.strftime("%Y-%m-%d %H:%M")}', '']
    for msg in messages_qs:
        lines.append(f'[{msg.timestamp.strftime("%H:%M")}] {msg.sender_name}: {msg.content}')
    plain = '\n'.join(lines)

    body = f'''
<h1 style="font-size:22px;font-weight:800;color:#1e293b;margin:0 0 6px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Chat Transcript</h1>
<p style="color:#64748b;font-size:14px;margin:0 0 24px;">Your conversation with {escape(org.name)}</p>

<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:16px 20px;margin-bottom:8px;">
  <table cellpadding="0" cellspacing="0" role="presentation" style="width:100%;">
    {_info_row('Visitor', f'<strong>{escape(room.visitor_name)}</strong>')}
    {_info_row('Room', f'<code style="background:#e2e8f0;padding:2px 6px;border-radius:4px;font-size:11px;">{escape(str(room.room_id)[:8])}</code>')}
    {_info_row('Date', room.created_at.strftime("%B %d, %Y at %H:%M"))}
  </table>
</div>

<div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin:20px 0;max-height:none;">
  {bubbles or '<p style="text-align:center;color:#94a3b8;font-size:13px;">No messages in this chat.</p>'}
</div>

<p style="text-align:center;font-size:12px;color:#94a3b8;margin:20px 0 0;">This transcript was sent from {escape(org.name)} via LiveVisitorHub.</p>
'''
    html = _base_html(body, preview_text=f'Chat transcript with {room.visitor_name}')
    send_html_email(
        f'Chat Transcript - {room.visitor_name} - {org.name}',
        plain, html,
        settings.DEFAULT_FROM_EMAIL,
        [to_email],
    )


def send_scheduled_report(report, org, stats):
    """Beautiful scheduled analytics report email.
    stats dict: {visitors, online, chats_total, chats_closed, avg_rating, goal_completions, period_label, dashboard_url}
    """
    from django.utils.html import escape

    # Build stat boxes
    stat_cells = ''
    stat_count = 0
    if report.include_visitors:
        stat_cells += _stat_box(stats.get('visitors', 0), 'New Visitors', '#7c3aed')
        stat_cells += _stat_box(stats.get('online', 0), 'Online Now', '#10b981')
        stat_count += 2
    if report.include_chats:
        stat_cells += _stat_box(stats.get('chats_total', 0), 'Total Chats', '#f59e0b')
        stat_cells += _stat_box(stats.get('chats_closed', 0), 'Closed', '#3b82f6')
        stat_count += 2
    if report.include_goals:
        stat_cells += _stat_box(stats.get('goal_completions', 0), 'Goals', '#10b981')
        stat_count += 1

    # Build ratings section
    rating_html = ''
    if report.include_chats and stats.get('avg_rating'):
        stars = ''
        full = int(stats['avg_rating'])
        for i in range(5):
            star_color = '#f59e0b' if i < full else '#e2e8f0'
            stars += f'<span style="color:{star_color};font-size:18px;">&#9733;</span>'
        rating_html = f'''
<div style="text-align:center;margin:20px 0;padding:16px;background:#fffbeb;border:1px solid #fde68a;border-radius:10px;">
  <div style="font-size:12px;font-weight:600;color:#92400e;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">Average Rating</div>
  <div>{stars}</div>
  <div style="font-size:20px;font-weight:800;color:#92400e;margin-top:4px;">{stats["avg_rating"]:.1f}<span style="font-size:14px;color:#b45309;">/5</span></div>
</div>'''

    period = stats.get('period_label', 'Last 7 days')
    dashboard_url = stats.get('dashboard_url', '/dashboard/advanced-analytics/')

    # Plain text
    plain_lines = [f'LiveVisitorHub - {report.name}', f'Period: {period}', '']
    if report.include_visitors:
        plain_lines.append(f'VISITORS: {stats.get("visitors", 0)} new, {stats.get("online", 0)} online')
    if report.include_chats:
        plain_lines.append(f'CHATS: {stats.get("chats_total", 0)} total, {stats.get("chats_closed", 0)} closed')
        if stats.get('avg_rating'):
            plain_lines.append(f'AVG RATING: {stats["avg_rating"]:.1f}/5')
    if report.include_goals:
        plain_lines.append(f'GOAL COMPLETIONS: {stats.get("goal_completions", 0)}')
    plain_lines += ['', f'View full analytics: {dashboard_url}', '', '- LiveVisitorHub']
    plain = '\n'.join(plain_lines)

    body = f'''
<h1 style="font-size:22px;font-weight:800;color:#1e293b;margin:0 0 4px;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">{escape(report.name)}</h1>
<p style="color:#64748b;font-size:14px;margin:0 0 24px;">
  <span style="display:inline-block;background:#f1f5f9;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;color:#475569;">{escape(period)}</span>
</p>

<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;overflow:hidden;margin-bottom:8px;">
  <table cellpadding="0" cellspacing="0" role="presentation" style="width:100%;">
    <tr>
      {stat_cells}
    </tr>
  </table>
</div>

{rating_html}

{_button_html(dashboard_url, 'View Full Analytics &rarr;')}

<p style="text-align:center;font-size:12px;color:#94a3b8;margin:0;">This report is sent {escape(report.frequency)} to {escape(report.email)}.</p>
'''
    html = _base_html(body, preview_text=f'{report.name} - {period}')
    send_html_email(
        f'[LiveVisitorHub] {report.name} - {period}',
        plain, html,
        settings.DEFAULT_FROM_EMAIL,
        [report.email],
    )


def send_password_reset_email(user, reset_url):
    """Beautiful password reset email."""
    from django.utils.html import escape
    name = escape(user.first_name or user.username)
    plain = (
        f'Hi {user.first_name or user.username},\n\n'
        'We received a request to reset your password for your LiveVisitorHub account.\n\n'
        f'Reset your password: {reset_url}\n\n'
        'If you didn\'t request this, you can safely ignore this email.\n\n'
        '- LiveVisitorHub Team'
    )
    body = f'''
<div style="text-align:center;margin-bottom:8px;">
  <div style="display:inline-block;width:56px;height:56px;background:linear-gradient(135deg,#3b82f6,#60a5fa);border-radius:50%;line-height:56px;margin-bottom:12px;">
    <span style="font-size:24px;color:#fff;">&#128274;</span>
  </div>
</div>
<h1 style="font-size:22px;font-weight:800;color:#1e293b;margin:0 0 6px;text-align:center;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Reset Your Password</h1>
<p style="text-align:center;color:#64748b;font-size:14px;margin:0 0 28px;">We received a request to reset your password.</p>

<p style="font-size:14px;color:#334155;">Hi <strong>{name}</strong>,</p>
<p style="font-size:14px;color:#334155;">Click the button below to set a new password for your LiveVisitorHub account. This link will expire in 24 hours.</p>

{_button_html(reset_url, 'Reset Password &rarr;', '#3b82f6')}

<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:10px;padding:14px 18px;margin:24px 0;">
  <p style="margin:0;font-size:13px;color:#991b1b;line-height:1.5;">
    <strong>Didn't request this?</strong> If you didn't ask for a password reset, you can safely ignore this email. Your password will remain unchanged.
  </p>
</div>

<p style="text-align:center;font-size:12px;color:#94a3b8;margin:20px 0 0;">If the button doesn't work, copy this link:<br><a href="{reset_url}" style="color:#7c3aed;word-break:break-all;font-size:11px;">{reset_url}</a></p>
'''
    html = _base_html(body, preview_text='Reset your LiveVisitorHub password')
    try:
        send_html_email(
            'Reset Your Password - LiveVisitorHub',
            plain, html,
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
    except Exception:
        logger.exception('Failed to send password reset email to %s', user.email)
