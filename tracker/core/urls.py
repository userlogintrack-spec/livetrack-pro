from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.shortcuts import render
from django.urls import path

from tracker.core import throttle

from . import views
from . import auth_extras

app_name = 'core'


class _ThrottledPasswordResetView(auth_views.PasswordResetView):
    """Wraps the default password-reset endpoint with a per-IP rate limit so
    nobody can email-bomb a victim by repeatedly triggering reset emails."""

    def post(self, request, *args, **kwargs):
        state = throttle.check(request, action='password_reset', limit=5, window=900)
        if state.blocked:
            messages.error(request, 'Too many reset requests. Try again in 15 minutes.')
            return render(request, self.template_name, {'form': self.form_class()})
        return super().post(request, *args, **kwargs)


urlpatterns = [
    path('login/', views.login_view, name='login'),
    path('register/', views.register_view, name='register'),
    path('logout/', views.logout_view, name='logout'),
    path('magic-link/', auth_extras.magic_link_request, name='magic_link_request'),
    path('magic-link/<str:token>/', auth_extras.magic_link_consume, name='magic_link_consume'),
    path('2fa/setup/', auth_extras.totp_setup, name='totp_setup'),
    path('2fa/verify/', auth_extras.totp_verify, name='totp_verify'),
    path('password-reset/', _ThrottledPasswordResetView.as_view(
        template_name='core/password_reset.html',
        email_template_name='core/password_reset_email.html',
        html_email_template_name='core/password_reset_email_html.html',
        success_url='/accounts/password-reset/done/',
    ), name='password_reset'),
    path('password-reset/done/', auth_views.PasswordResetDoneView.as_view(
        template_name='core/password_reset_done.html',
    ), name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='core/password_reset_confirm.html',
        success_url='/accounts/login/',
    ), name='password_reset_confirm'),
]
