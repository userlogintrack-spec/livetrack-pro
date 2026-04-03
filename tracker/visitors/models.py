from django.db import models
from django.utils import timezone


class Visitor(models.Model):
    organization = models.ForeignKey('core.Organization', on_delete=models.CASCADE, related_name='visitors', null=True, blank=True)
    session_key = models.CharField(max_length=100, db_index=True)
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

    def __str__(self):
        return f"Visitor {self.ip_address} ({self.browser}/{self.os})"


class PageView(models.Model):
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='page_views')
    url = models.URLField(max_length=500)
    page_title = models.CharField(max_length=300, blank=True, default='')
    timestamp = models.DateTimeField(default=timezone.now)
    time_spent = models.PositiveIntegerField(default=0, help_text="Time spent in seconds")

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.visitor.ip_address} - {self.url}"
