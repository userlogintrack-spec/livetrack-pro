from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from tracker.visitors.models import Visitor
from tracker.core.models import Organization


class ChatRoom(models.Model):
    STATUS_CHOICES = [
        ('waiting', 'Waiting'),
        ('active', 'Active'),
        ('closed', 'Closed'),
    ]

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='chat_rooms', null=True, blank=True)
    room_id = models.CharField(max_length=100, unique=True, db_index=True)
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='chat_rooms')
    agent = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='chat_rooms')
    visitor_name = models.CharField(max_length=100, default='Visitor')
    visitor_email = models.EmailField(blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    subject = models.CharField(max_length=200, blank=True, default='')
    rating = models.PositiveSmallIntegerField(null=True, blank=True)
    rating_feedback = models.TextField(blank=True, default='')
    tags = models.CharField(max_length=500, blank=True, default='')
    priority = models.CharField(max_length=10, choices=[
        ('low', 'Low'), ('medium', 'Medium'), ('high', 'High'),
    ], default='medium')
    is_pinned = models.BooleanField(default=False)
    is_bookmarked = models.BooleanField(default=False)
    is_snoozed = models.BooleanField(default=False)
    snooze_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return f"Chat #{self.room_id} - {self.visitor_name}"

    @property
    def duration(self):
        end = self.closed_at or timezone.now()
        return end - self.created_at

    @property
    def message_count(self):
        return self.messages.count()


class Message(models.Model):
    SENDER_CHOICES = [
        ('visitor', 'Visitor'),
        ('agent', 'Agent'),
        ('system', 'System'),
    ]

    MSG_TYPE_CHOICES = [
        ('text', 'Text'),
        ('file', 'File'),
        ('image', 'Image'),
        ('system', 'System'),
    ]

    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages')
    sender_type = models.CharField(max_length=10, choices=SENDER_CHOICES)
    sender_name = models.CharField(max_length=100, default='')
    content = models.TextField()
    msg_type = models.CharField(max_length=10, choices=MSG_TYPE_CHOICES, default='text')
    file = models.FileField(upload_to='chat_files/%Y/%m/', null=True, blank=True)
    file_name = models.CharField(max_length=255, blank=True, default='')
    is_read = models.BooleanField(default=False)
    timestamp = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['timestamp']

    def __str__(self):
        return f"[{self.sender_type}] {self.content[:50]}"


class AgentProfile(models.Model):
    ROLE_CHOICES = [('owner', 'Owner'), ('agent', 'Agent')]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='agent_profile')
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='agents', null=True, blank=True)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='agent')
    avatar_color = models.CharField(max_length=7, default='#6366f1')
    is_available = models.BooleanField(default=True)
    max_chats = models.PositiveIntegerField(default=5)
    total_chats_handled = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Agent: {self.user.get_full_name() or self.user.username}"

    @property
    def active_chats_count(self):
        return self.user.chat_rooms.filter(status='active').count()


class OfflineMessage(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='offline_messages', null=True, blank=True)
    name = models.CharField(max_length=100)
    email = models.EmailField()
    message = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Offline: {self.name} - {self.email}"


class CannedResponse(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='canned_responses', null=True, blank=True)
    title = models.CharField(max_length=100)
    message = models.TextField()
    shortcut = models.CharField(max_length=20, blank=True, default='', help_text="Type /shortcut to use")
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='canned_responses')
    is_global = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return self.title


class VisitorNote(models.Model):
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='agent_notes')
    agent = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Note on {self.visitor.ip_address} by {self.agent.username}"


class InternalNote(models.Model):
    """Agent-to-agent internal notes within a chat room (not visible to visitors)."""
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='internal_notes')
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='internal_notes')
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"Note by {self.agent.username} in {self.room.room_id}"


class Webhook(models.Model):
    """Webhook endpoints for chat events."""
    EVENT_CHOICES = [
        ('chat.created', 'Chat Created'),
        ('chat.closed', 'Chat Closed'),
        ('message.new', 'New Message'),
        ('visitor.new', 'New Visitor'),
        ('agent.joined', 'Agent Joined'),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='webhooks')
    url = models.URLField(max_length=500)
    events = models.CharField(max_length=500, default='chat.created,chat.closed')
    is_active = models.BooleanField(default=True)
    secret = models.CharField(max_length=64, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Webhook {self.url[:50]} for {self.organization.name}"


class ActivityLog(models.Model):
    """Audit trail for important actions."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='activity_logs')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=50)
    description = models.TextField()
    target_type = models.CharField(max_length=50, blank=True, default='')
    target_id = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.user} - {self.action} - {self.created_at}"


class ChatLabel(models.Model):
    """Custom labels/categories for chats."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='chat_labels')
    name = models.CharField(max_length=50)
    color = models.CharField(max_length=7, default='#6366f1')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('organization', 'name')]
        ordering = ['name']

    def __str__(self):
        return self.name


class SavedReply(models.Model):
    """Personal saved replies per agent."""
    agent = models.ForeignKey(User, on_delete=models.CASCADE, related_name='saved_replies')
    title = models.CharField(max_length=100)
    message = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['title']

    def __str__(self):
        return f"{self.title} - {self.agent.username}"
