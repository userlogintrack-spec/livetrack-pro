import uuid
from django.db import models
from django.contrib.auth.models import User


class Organization(models.Model):
    """Multi-tenant organization - each signup creates one."""
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    widget_key = models.CharField(max_length=32, unique=True, db_index=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='owned_organizations')
    created_at = models.DateTimeField(auto_now_add=True)

    # Widget customization
    widget_title = models.CharField(max_length=100, default='LiveTrack Support')
    widget_color = models.CharField(max_length=7, default='#7c3aed')
    widget_position = models.CharField(max_length=20, default='bottom-right',
        choices=[('bottom-right', 'Bottom Right'), ('bottom-left', 'Bottom Left')])
    welcome_message = models.TextField(default='Hi! How can we help you today?')
    offline_message = models.TextField(default='We are currently offline. Please leave a message.')
    auto_reply_enabled = models.BooleanField(default=True)
    auto_reply_message = models.TextField(default='An agent will be with you shortly.')
    require_email = models.BooleanField(default=False)

    # Business hours
    business_hours_enabled = models.BooleanField(default=False)
    business_hours_start = models.TimeField(default='09:00')
    business_hours_end = models.TimeField(default='18:00')
    business_hours_timezone = models.CharField(max_length=50, default='Asia/Kolkata')

    # Email notifications
    notify_email = models.EmailField(blank=True, default='', help_text='Email for new chat notifications')
    notify_on_new_chat = models.BooleanField(default=True)

    # Auto-responder (if no agent replies in X minutes)
    auto_responder_enabled = models.BooleanField(default=False)
    auto_responder_delay = models.PositiveIntegerField(default=2, help_text='Minutes before auto-response')
    auto_responder_message = models.TextField(default='Thanks for waiting! An agent will be with you very soon.')

    # Chat assignment rule
    ASSIGN_CHOICES = [('least_busy', 'Least Busy'), ('round_robin', 'Round Robin'), ('manual', 'Manual Only')]
    chat_assign_rule = models.CharField(max_length=20, choices=ASSIGN_CHOICES, default='least_busy')

    # Proactive chat
    proactive_enabled = models.BooleanField(default=False)
    proactive_delay = models.PositiveIntegerField(default=30, help_text='Seconds before showing proactive message')
    proactive_message = models.CharField(max_length=200, default='Need help? Chat with us!')

    def save(self, *args, **kwargs):
        if not self.widget_key:
            self.widget_key = uuid.uuid4().hex
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class WebsiteSettings(models.Model):
    """Legacy - kept for backward compatibility."""
    site_name = models.CharField(max_length=200, default='My Website')
    welcome_message = models.TextField(default='Hi! How can we help you today?')
    offline_message = models.TextField(default='We are currently offline. Please leave a message.')
    chat_widget_color = models.CharField(max_length=7, default='#6366f1')
    auto_reply_enabled = models.BooleanField(default=True)
    auto_reply_message = models.TextField(
        default='Thanks for reaching out! An agent will be with you shortly.'
    )
    require_email = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = 'Website Settings'

    def __str__(self):
        return self.site_name
