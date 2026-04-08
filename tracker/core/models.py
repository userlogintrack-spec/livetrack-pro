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


class Subscription(models.Model):
    """Tracks organization's active plan and billing."""
    PLAN_CHOICES = [
        ('free', 'Free'),
        ('pro', 'Pro'),
        ('enterprise', 'Enterprise'),
    ]
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('past_due', 'Past Due'),
        ('cancelled', 'Cancelled'),
        ('trialing', 'Trialing'),
    ]
    INTERVAL_CHOICES = [
        ('month', 'Monthly'),
        ('year', 'Yearly'),
    ]

    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='subscription')
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free')
    billing_interval = models.CharField(max_length=10, choices=INTERVAL_CHOICES, default='month')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    # Stripe
    stripe_customer_id = models.CharField(max_length=100, blank=True, default='')
    stripe_subscription_id = models.CharField(max_length=100, blank=True, default='')
    # Billing
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancel_at_period_end = models.BooleanField(default=False)
    # Coupon
    coupon_applied = models.ForeignKey('Coupon', on_delete=models.SET_NULL, null=True, blank=True, related_name='subscriptions')
    discount_percent = models.PositiveIntegerField(default=0)
    # Pending purchase intent (set before redirecting to Stripe; consumed in billing_success)
    pending_plan = models.CharField(max_length=20, blank=True, default='')
    pending_interval = models.CharField(max_length=10, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.organization.name} — {self.plan}"

    @property
    def is_active(self):
        return self.status in ('active', 'trialing')

    @property
    def plan_limits(self):
        """Return limits for the current plan."""
        LIMITS = {
            'free': {
                'max_visitors_per_month': 100,
                'max_agents': 1,
                'chat_history_days': 30,
                'advanced_analytics': False,
                'ai_bot': False,
                'custom_branding': False,
                'api_access': False,
                'white_label': False,
                'priority_support': False,
                'sla_guarantee': False,
                'email_notifications': False,
                'unlimited_chat_history': False,
            },
            'pro': {
                'max_visitors_per_month': 999999,
                'max_agents': 5,
                'chat_history_days': 9999,
                'advanced_analytics': True,
                'ai_bot': False,
                'custom_branding': True,
                'api_access': False,
                'white_label': False,
                'priority_support': False,
                'sla_guarantee': False,
                'email_notifications': True,
                'unlimited_chat_history': True,
            },
            'enterprise': {
                'max_visitors_per_month': 999999,
                'max_agents': 999,
                'chat_history_days': 9999,
                'advanced_analytics': True,
                'ai_bot': True,
                'custom_branding': True,
                'api_access': True,
                'white_label': True,
                'priority_support': True,
                'sla_guarantee': True,
                'email_notifications': True,
                'unlimited_chat_history': True,
            },
        }
        return LIMITS.get(self.plan, LIMITS['free'])


class PaymentHistory(models.Model):
    """Records all payments."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='USD')
    plan = models.CharField(max_length=20)
    status = models.CharField(max_length=20, default='succeeded')
    stripe_payment_id = models.CharField(max_length=100, blank=True, default='')
    stripe_invoice_id = models.CharField(max_length=100, blank=True, default='')
    description = models.CharField(max_length=300, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.organization.name} — ${self.amount} ({self.plan})"


class Coupon(models.Model):
    """Discount coupons for subscriptions."""
    DISCOUNT_TYPES = [
        ('percent', 'Percentage Off'),
        ('fixed', 'Fixed Amount Off'),
    ]
    code = models.CharField(max_length=50, unique=True, db_index=True)
    name = models.CharField(max_length=200, blank=True, default='')
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPES, default='percent')
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, help_text='Percentage (e.g. 20) or fixed amount (e.g. 5.00)')
    # Restrictions
    applicable_plans = models.CharField(max_length=100, default='pro,enterprise', help_text='Comma-separated plan names')
    applicable_intervals = models.CharField(max_length=20, default='month,year', help_text='month,year or both')
    max_uses = models.PositiveIntegerField(default=0, help_text='0 = unlimited')
    times_used = models.PositiveIntegerField(default=0)
    min_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Validity
    is_active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(auto_now_add=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        if self.discount_type == 'percent':
            return f"{self.code} — {self.discount_value}% off"
        return f"{self.code} — ${self.discount_value} off"

    @property
    def is_valid(self):
        from django.utils import timezone
        if not self.is_active:
            return False
        if self.max_uses > 0 and self.times_used >= self.max_uses:
            return False
        if self.valid_until and timezone.now() > self.valid_until:
            return False
        return True

    def applies_to(self, plan, interval):
        plans = [p.strip() for p in self.applicable_plans.split(',')]
        intervals = [i.strip() for i in self.applicable_intervals.split(',')]
        return plan in plans and interval in intervals

    def calculate_discount(self, amount):
        if self.discount_type == 'percent':
            return round(float(amount) * float(self.discount_value) / 100, 2)
        return min(float(self.discount_value), float(amount))


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
