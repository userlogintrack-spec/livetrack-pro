from django.db import models
from django.utils import timezone


class Visitor(models.Model):
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='visitors', null=True, blank=True)
    session_key = models.CharField(max_length=100, db_index=True)
    visitor_fingerprint = models.CharField(max_length=100, blank=True, default='', db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True, default='')
    browser = models.CharField(max_length=100, blank=True, default='')
    os = models.CharField(max_length=100, blank=True, default='')
    device_type = models.CharField(max_length=20, choices=[
        ('desktop', 'Desktop'),
        ('mobile', 'Mobile'),
        ('tablet', 'Tablet'),
        ('unknown', 'Unknown'),
    ], default='unknown')
    country = models.CharField(max_length=100, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    referrer = models.URLField(max_length=500, blank=True, default='')
    referrer_source = models.CharField(max_length=100, blank=True, default='Direct')
    first_visit = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(auto_now=True)
    total_visits = models.PositiveIntegerField(default=1)
    is_online = models.BooleanField(default=True)
    is_banned = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default='')
    score = models.PositiveIntegerField(default=0, help_text='Visitor engagement score')
    # UTM Campaign Tracking
    utm_source = models.CharField(max_length=200, blank=True, default='')
    utm_medium = models.CharField(max_length=200, blank=True, default='')
    utm_campaign = models.CharField(max_length=200, blank=True, default='')
    utm_term = models.CharField(max_length=200, blank=True, default='')
    utm_content = models.CharField(max_length=200, blank=True, default='')
    # Language
    language = models.CharField(max_length=20, blank=True, default='')
    # Landing / Exit page
    landing_page = models.URLField(max_length=500, blank=True, default='')
    exit_page = models.URLField(max_length=500, blank=True, default='')
    # Session metrics
    session_duration = models.PositiveIntegerField(default=0, help_text='Total session duration in seconds')
    pages_per_session = models.PositiveIntegerField(default=0)
    is_bounced = models.BooleanField(default=False, help_text='Visited only 1 page')

    @property
    def score_label(self):
        if self.score >= 70:
            return 'hot'
        elif self.score >= 30:
            return 'warm'
        return 'cold'

    class Meta:
        ordering = ['-last_seen']
        unique_together = [('session_key', 'organization')]
        indexes = [
            models.Index(fields=['-last_seen']),
            models.Index(fields=['organization', '-last_seen']),
            models.Index(fields=['organization', 'is_banned']),
        ]

    def __str__(self):
        return f"Visitor {self.ip_address} ({self.browser}/{self.os})"


class PageView(models.Model):
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='page_views')
    url = models.URLField(max_length=500)
    page_title = models.CharField(max_length=300, blank=True, default='')
    timestamp = models.DateTimeField(default=timezone.now)
    time_spent = models.PositiveIntegerField(default=0, help_text="Time spent in seconds")
    # Page performance
    load_time_ms = models.PositiveIntegerField(default=0, help_text="Page load time in milliseconds")
    # UTM params on this pageview
    utm_source = models.CharField(max_length=200, blank=True, default='')
    utm_medium = models.CharField(max_length=200, blank=True, default='')
    utm_campaign = models.CharField(max_length=200, blank=True, default='')
    is_entry = models.BooleanField(default=False, help_text="Is this the landing/entry page")
    is_exit = models.BooleanField(default=False, help_text="Is this the exit page")

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['visitor', '-timestamp']),
            models.Index(fields=['-timestamp']),
        ]

    def __str__(self):
        return f"{self.visitor.ip_address} - {self.url}"

    @property
    def path(self):
        from urllib.parse import urlparse
        return urlparse(self.url).path or '/'


# ──────────────────────────────────────────────────────────
# Custom Event Tracking
# ──────────────────────────────────────────────────────────
class CustomEvent(models.Model):
    """Track custom events like button clicks, form submits, etc."""
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='custom_events', null=True, blank=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='events')
    event_name = models.CharField(max_length=200, db_index=True)
    event_category = models.CharField(max_length=200, blank=True, default='')
    event_label = models.CharField(max_length=500, blank=True, default='')
    event_value = models.FloatField(default=0)
    page_url = models.URLField(max_length=500, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', 'event_name', '-timestamp']),
            models.Index(fields=['visitor', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.event_name} - {self.visitor.ip_address}"


# ──────────────────────────────────────────────────────────
# Goals & Conversion Tracking
# ──────────────────────────────────────────────────────────
class Goal(models.Model):
    """Trackable goals/conversions."""
    GOAL_TYPES = [
        ('pageview', 'Page View (URL match)'),
        ('event', 'Custom Event'),
        ('duration', 'Session Duration'),
        ('pages', 'Pages Per Session'),
    ]
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='goals')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    goal_type = models.CharField(max_length=20, choices=GOAL_TYPES, default='pageview')
    target_url = models.CharField(max_length=500, blank=True, default='', help_text='URL pattern to match for pageview goals')
    target_event = models.CharField(max_length=200, blank=True, default='', help_text='Event name for event goals')
    target_value = models.FloatField(default=0, help_text='Duration (seconds) or pages count threshold')
    monetary_value = models.FloatField(default=0, help_text='Value of each conversion in currency')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def completion_count(self):
        return self.completions.count()

    @property
    def total_value(self):
        return self.completion_count * self.monetary_value


class GoalCompletion(models.Model):
    """Records when a goal is completed."""
    goal = models.ForeignKey(Goal, on_delete=models.CASCADE, related_name='completions')
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='goal_completions')
    page_url = models.URLField(max_length=500, blank=True, default='')
    completed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-completed_at']
        indexes = [
            models.Index(fields=['goal', '-completed_at']),
        ]

    def __str__(self):
        return f"{self.goal.name} by {self.visitor.ip_address}"


# ──────────────────────────────────────────────────────────
# Scheduled Email Reports
# ──────────────────────────────────────────────────────────
class ScheduledReport(models.Model):
    """Automated email reports."""
    FREQUENCY_CHOICES = [
        ('daily', 'Daily'),
        ('weekly', 'Weekly (Monday)'),
        ('monthly', 'Monthly (1st)'),
    ]
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='scheduled_reports')
    name = models.CharField(max_length=200)
    email = models.EmailField()
    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default='weekly')
    include_visitors = models.BooleanField(default=True)
    include_chats = models.BooleanField(default=True)
    include_goals = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)
    last_sent = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.frequency})"


# ══════════════════════════════════════════════════════════
# MICROSOFT CLARITY FEATURES
# ══════════════════════════════════════════════════════════

class SessionRecording(models.Model):
    """Records visitor session for replay."""
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='session_recordings', null=True, blank=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='recordings')
    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    events_data = models.JSONField(default=list, help_text='Array of recorded DOM events')
    duration = models.PositiveIntegerField(default=0, help_text='Recording duration in seconds')
    pages_visited = models.PositiveIntegerField(default=0)
    start_url = models.URLField(max_length=500, blank=True, default='')
    device_type = models.CharField(max_length=20, blank=True, default='')
    screen_width = models.PositiveIntegerField(default=0)
    screen_height = models.PositiveIntegerField(default=0)
    has_rage_clicks = models.BooleanField(default=False)
    has_dead_clicks = models.BooleanField(default=False)
    has_quick_back = models.BooleanField(default=False)
    has_errors = models.BooleanField(default=False)
    frustration_score = models.PositiveIntegerField(default=0, help_text='0-100 frustration level')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['frustration_score']),
        ]

    def __str__(self):
        return f"Recording {self.session_id[:8]} - {self.visitor.ip_address}"


class ClickData(models.Model):
    """Individual click events for heatmap generation."""
    CLICK_TYPES = [
        ('click', 'Normal Click'),
        ('rage', 'Rage Click'),
        ('dead', 'Dead Click'),
    ]
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='click_data', null=True, blank=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='clicks')
    recording = models.ForeignKey(SessionRecording, on_delete=models.SET_NULL, null=True, blank=True, related_name='clicks')
    page_url = models.URLField(max_length=500)
    page_path = models.CharField(max_length=500, blank=True, default='')
    x_percent = models.FloatField(help_text='Click X position as % of viewport width')
    y_percent = models.FloatField(help_text='Click Y position as % of page height')
    x_px = models.PositiveIntegerField(default=0)
    y_px = models.PositiveIntegerField(default=0)
    element_tag = models.CharField(max_length=50, blank=True, default='')
    element_text = models.CharField(max_length=200, blank=True, default='')
    element_selector = models.CharField(max_length=500, blank=True, default='')
    click_type = models.CharField(max_length=10, choices=CLICK_TYPES, default='click')
    device_type = models.CharField(max_length=20, blank=True, default='desktop')
    viewport_width = models.PositiveIntegerField(default=0)
    viewport_height = models.PositiveIntegerField(default=0)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', 'page_path', '-timestamp']),
            models.Index(fields=['click_type']),
        ]

    def __str__(self):
        return f"{self.click_type} on {self.page_path} ({self.x_percent:.0f}%, {self.y_percent:.0f}%)"


class ScrollData(models.Model):
    """Scroll depth tracking per page per visitor."""
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='scroll_data', null=True, blank=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='scrolls')
    page_url = models.URLField(max_length=500)
    page_path = models.CharField(max_length=500, blank=True, default='')
    max_scroll_percent = models.PositiveIntegerField(default=0, help_text='Max scroll depth 0-100%')
    page_height = models.PositiveIntegerField(default=0)
    viewport_height = models.PositiveIntegerField(default=0)
    device_type = models.CharField(max_length=20, blank=True, default='desktop')
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', 'page_path', '-timestamp']),
        ]

    def __str__(self):
        return f"Scroll {self.max_scroll_percent}% on {self.page_path}"


class JSError(models.Model):
    """JavaScript errors caught from visitor browsers."""
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='js_errors', null=True, blank=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='js_errors')
    recording = models.ForeignKey(SessionRecording, on_delete=models.SET_NULL, null=True, blank=True, related_name='errors')
    error_message = models.TextField()
    error_source = models.CharField(max_length=500, blank=True, default='')
    error_line = models.PositiveIntegerField(default=0)
    error_col = models.PositiveIntegerField(default=0)
    stack_trace = models.TextField(blank=True, default='')
    page_url = models.URLField(max_length=500, blank=True, default='')
    browser = models.CharField(max_length=100, blank=True, default='')
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', '-timestamp']),
        ]

    def __str__(self):
        return f"JS Error: {self.error_message[:60]}"


class FrustrationSignal(models.Model):
    """Detected frustration signals — rage clicks, dead clicks, quick-backs."""
    SIGNAL_TYPES = [
        ('rage_click', 'Rage Click'),
        ('dead_click', 'Dead Click'),
        ('quick_back', 'Quick Back'),
        ('excessive_scroll', 'Excessive Scrolling'),
        ('error_click', 'Click After Error'),
    ]
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='frustration_signals', null=True, blank=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='frustration_signals')
    recording = models.ForeignKey(SessionRecording, on_delete=models.SET_NULL, null=True, blank=True, related_name='frustration_signals')
    signal_type = models.CharField(max_length=20, choices=SIGNAL_TYPES)
    page_url = models.URLField(max_length=500, blank=True, default='')
    page_path = models.CharField(max_length=500, blank=True, default='')
    element_selector = models.CharField(max_length=500, blank=True, default='')
    element_text = models.CharField(max_length=200, blank=True, default='')
    details = models.JSONField(default=dict, blank=True)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['organization', 'signal_type', '-timestamp']),
        ]

    def __str__(self):
        return f"{self.signal_type} on {self.page_path}"


class PageInsight(models.Model):
    """Per-page aggregated insights — engagement, frustration, scroll depth."""
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='page_insights')
    page_path = models.CharField(max_length=500, db_index=True)
    total_views = models.PositiveIntegerField(default=0)
    unique_visitors = models.PositiveIntegerField(default=0)
    avg_time_spent = models.FloatField(default=0)
    avg_scroll_depth = models.FloatField(default=0)
    bounce_rate = models.FloatField(default=0)
    total_clicks = models.PositiveIntegerField(default=0)
    rage_clicks = models.PositiveIntegerField(default=0)
    dead_clicks = models.PositiveIntegerField(default=0)
    frustration_score = models.FloatField(default=0)
    engagement_score = models.FloatField(default=0)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('organization', 'page_path')]
        ordering = ['-total_views']

    def __str__(self):
        return f"Insights: {self.page_path}"
