"""Magic-link passwordless login + 2FA TOTP challenge views.

Shared design notes:
  - Magic links are 32-byte URL-safe tokens, single-use, 30-min TTL.
  - Token comparison uses `hmac.compare_digest()` after a unique-index lookup —
    Postgres' index lookup is constant-ish on B-tree, and the explicit compare
    closes any residual gap in the application layer.
  - All write/verify endpoints go through `throttle.check()` so limits are
    consistent and centrally tunable.
  - TOTP secret is read via `profile.totp_secret_plain` — the raw column holds
    Fernet ciphertext.
  - Backup codes are stored as PBKDF2 hashes; verified with constant-time compare.
  - Session is rotated (`cycle_key`) after every successful auth_login to defeat
    session fixation: an attacker who pre-set a session cookie loses it on login.
"""
from __future__ import annotations

import hmac
import secrets as py_secrets
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from tracker.chat.models import MagicLinkToken
from tracker.core import throttle
from tracker.core.crypto import hash_code, verify_code


def _safe_login(request, user):
    """auth_login + cycle_key — rotates the session id so a pre-existing
    attacker-controlled session token can't ride to an authenticated state."""
    auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session.cycle_key()


# ────────────────────────────────────────────────
# MAGIC-LINK LOGIN
# ────────────────────────────────────────────────

@require_http_methods(['GET', 'POST'])
def magic_link_request(request):
    """Email a single-use login link. Always returns a generic success page so
    the form can't be used to enumerate which emails are registered."""
    if request.method == 'GET':
        return render(request, 'core/magic_link_request.html')

    email = (request.POST.get('email') or '').strip().lower()
    # 5/15min per IP — covers a real user retrying a typo'd email but stops
    # email-bomb abuse cold.
    state = throttle.check(request, action='magic_link_request', limit=5, window=900)
    if state.blocked:
        messages.error(request, 'Too many requests. Try again in 15 minutes.')
        return render(request, 'core/magic_link_request.html')

    user = User.objects.filter(email__iexact=email, is_active=True).first()
    if user:
        token_str = py_secrets.token_urlsafe(32)
        MagicLinkToken.objects.create(
            user=user,
            token=token_str,
            expires_at=timezone.now() + timedelta(minutes=30),
            requested_ip=throttle.client_ip(request),
        )
        link = request.build_absolute_uri(reverse('core:magic_link_consume', args=[token_str]))
        try:
            send_mail(
                subject='Your LiveTrack Pro sign-in link',
                message=f'Click to sign in: {link}\n\nThis link expires in 30 minutes and works once.',
                from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
                recipient_list=[user.email],
                fail_silently=True,
            )
        except Exception:
            pass
    return render(request, 'core/magic_link_sent.html', {'email': email})


def magic_link_consume(request, token):
    """Consume the token and log the user in. One-shot — token is marked used.

    Despite the unique-index lookup we still do an explicit `compare_digest`:
    cheap defense in depth against any future change that loosens the lookup
    (e.g., switching to ORM filtering by a non-indexed alt key)."""
    # Cap token length up front so an attacker can't waste DB cycles with megabyte
    # URLs. URL-safe base64 of 32 bytes = 43 chars; allow a little slack.
    if not token or len(token) > 64:
        return render(request, 'core/magic_link_invalid.html', status=400)
    tok = MagicLinkToken.objects.filter(token=token).select_related('user').first()
    if not tok or not hmac.compare_digest(tok.token, token) or not tok.is_valid:
        return render(request, 'core/magic_link_invalid.html', status=400)
    tok.consumed_at = timezone.now()
    tok.save(update_fields=['consumed_at'])
    user = tok.user
    profile = getattr(user, 'agent_profile', None)
    if profile and profile.totp_enabled:
        # Rotate session before stashing pending_2fa so a pre-set cookie can't
        # be replayed at totp_verify by an attacker who tricked the user into
        # consuming a magic link.
        request.session.cycle_key()
        request.session['pending_2fa_user_id'] = user.id
        return redirect('core:totp_verify')
    _safe_login(request, user)
    return redirect('dashboard:home')


# ────────────────────────────────────────────────
# TOTP 2FA
# ────────────────────────────────────────────────

def _totp_module():
    """Import pyotp lazily so the app boots even when the dep is missing."""
    try:
        import pyotp  # type: ignore
        return pyotp
    except ImportError:
        return None


def _render_qr_svg(text):
    """Render a QR code as inline SVG so the TOTP secret never leaves our server.

    Falls back to None if `qrcode[svg]` isn't installed; the template then shows
    only the plain-text secret for manual entry into the authenticator app.
    """
    try:
        import qrcode  # type: ignore
        from qrcode.image.svg import SvgPathImage  # type: ignore
        import io
        img = qrcode.make(text, image_factory=SvgPathImage, box_size=10, border=1)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode('utf-8')
    except Exception:
        return None


@login_required
def totp_setup(request):
    """Show QR + accept verification code to flip totp_enabled."""
    pyotp = _totp_module()
    profile = getattr(request.user, 'agent_profile', None)
    if pyotp is None:
        return render(request, 'core/totp_setup.html', {'error': 'pyotp is not installed on the server. Run: pip install pyotp'})
    if not profile:
        return redirect('dashboard:home')

    secret_plain = profile.totp_secret_plain
    if not secret_plain:
        secret_plain = pyotp.random_base32()
        profile.totp_secret_plain = secret_plain
        profile.save(update_fields=['totp_secret'])

    totp = pyotp.TOTP(secret_plain)
    issuer = 'LiveTrack Pro'
    label = request.user.email or request.user.username
    provisioning_uri = totp.provisioning_uri(name=label, issuer_name=issuer)
    # Render the QR locally so the secret never leaves our server.
    qr_svg = _render_qr_svg(provisioning_uri)

    error = None
    if request.method == 'POST':
        action = request.POST.get('action', 'verify')
        if action == 'disable' and profile.totp_enabled:
            profile.totp_enabled = False
            profile.backup_codes = []
            profile.totp_secret_plain = ''
            profile.save(update_fields=['totp_enabled', 'backup_codes', 'totp_secret'])
            messages.success(request, '2FA disabled.')
            return redirect('core:totp_setup')

        # 5 attempts/min per user — TOTP space is 10^6, so unbounded brute force
        # finishes in ~3 seconds without this guard.
        state = throttle.check(
            request, action='totp_setup_verify', limit=5, window=60,
            key=str(request.user.id),
        )
        if state.blocked:
            error = 'Too many attempts. Wait a minute before trying again.'
        else:
            code = (request.POST.get('code') or '').strip().replace(' ', '')
            # valid_window=1 (±30s) is RFC 6238 standard tolerance for clock skew.
            if totp.verify(code, valid_window=1):
                # Generate 8 single-use backup codes; store hashes only. We
                # show the plaintext to the user once, here, then it's gone.
                plain_backup = [py_secrets.token_hex(4) for _ in range(8)]
                profile.backup_codes = [hash_code(c) for c in plain_backup]
                profile.totp_enabled = True
                profile.save(update_fields=['totp_enabled', 'backup_codes'])
                return render(request, 'core/totp_setup.html', {
                    'profile': profile,
                    'enabled': True,
                    'backup_codes': plain_backup,
                    'provisioning_uri': provisioning_uri,
                })
            error = 'That code did not match. Try again with the latest 6-digit code from your authenticator.'

    return render(request, 'core/totp_setup.html', {
        'profile': profile,
        'enabled': profile.totp_enabled,
        'provisioning_uri': provisioning_uri,
        'qr_svg': qr_svg,
        'totp_secret': secret_plain,
        'error': error,
    })


def totp_verify(request):
    """Login challenge after username/password — required when totp_enabled."""
    pyotp = _totp_module()
    user_id = request.session.get('pending_2fa_user_id')
    if not user_id:
        return redirect('core:login')
    user = User.objects.filter(id=user_id, is_active=True).first()
    if not user:
        request.session.pop('pending_2fa_user_id', None)
        return redirect('core:login')
    profile = getattr(user, 'agent_profile', None)
    if not profile or not profile.totp_enabled:
        request.session.pop('pending_2fa_user_id', None)
        _safe_login(request, user)
        return redirect('dashboard:home')

    error = None
    if request.method == 'POST':
        # 10 attempts/5min per pending-user — bounds backup-code brute force
        # (8 codes × 8-hex chars) and TOTP brute force (10^6) without nuking
        # legitimate users with shaky thumbs.
        state = throttle.check(
            request, action='totp_verify', limit=10, window=300,
            key=f'user:{user.id}',
        )
        if state.blocked:
            error = 'Too many attempts. Wait a few minutes before trying again.'
        else:
            code = (request.POST.get('code') or '').strip().replace(' ', '')
            ok = False
            if pyotp and profile.totp_secret_plain:
                ok = pyotp.TOTP(profile.totp_secret_plain).verify(code, valid_window=1)
            if not ok and code:
                # Check against hashed backup codes; consume on match.
                for stored in (profile.backup_codes or []):
                    if verify_code(code, stored):
                        profile.backup_codes = [c for c in profile.backup_codes if c != stored]
                        profile.save(update_fields=['backup_codes'])
                        ok = True
                        break
            if ok:
                request.session.pop('pending_2fa_user_id', None)
                _safe_login(request, user)
                return redirect('dashboard:home')
            error = 'Invalid code. Use a 6-digit code from your authenticator or one of your backup codes.'

    return render(request, 'core/totp_verify.html', {'error': error})
