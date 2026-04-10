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
    website = models.ForeignKey('core.Website', on_delete=models.SET_NULL, null=True, blank=True, related_name='chat_rooms')
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
        indexes = [
            models.Index(fields=['organization', 'status']),
            models.Index(fields=['organization', '-updated_at']),
            models.Index(fields=['organization', '-created_at']),
            models.Index(fields=['visitor', '-created_at']),
        ]

    def __str__(self):
        return f"Chat #{self.room_id} - {self.visitor_name}"

    @property
    def duration(self):
        end = self.closed_at or timezone.now()
        return end - self.created_at

    @property
    def duration_display(self):
        """Human-readable duration like '2h 15m' or '5m 30s'."""
        total = int(self.duration.total_seconds())
        if total < 60:
            return f'{total}s'
        minutes = total // 60
        hours = minutes // 60
        if hours > 0:
            return f'{hours}h {minutes % 60}m'
        return f'{minutes}m'

    @property
    def message_count(self):
        return self.messages.count()


class ChatParticipant(models.Model):
    """Tracks every agent/owner who joined a chat — supports multi-agent collaboration.
    The first joiner is `is_primary=True` and is also written to ChatRoom.agent for
    backwards compatibility. Additional joiners are collaborators."""
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='participants')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_participations')
    is_primary = models.BooleanField(default=False)
    joined_at = models.DateTimeField(default=timezone.now)
    left_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = [('room', 'user')]
        ordering = ['joined_at']
        indexes = [
            models.Index(fields=['room', 'user']),
        ]

    def __str__(self):
        return f"{self.user.username} in {self.room.room_id}{' (primary)' if self.is_primary else ''}"


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
        indexes = [
            models.Index(fields=['room', 'timestamp']),
            models.Index(fields=['room', 'sender_type', 'is_read']),
        ]

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


class AgentWebsiteAccess(models.Model):
    """Controls which websites an agent can access."""
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='website_access')
    website = models.ForeignKey('core.Website', on_delete=models.CASCADE, related_name='agent_access')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('agent', 'website')]

    def __str__(self):
        return f"{self.agent.user.username} → {self.website.name}"


class OfflineMessage(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='offline_messages', null=True, blank=True)
    website = models.ForeignKey('core.Website', on_delete=models.SET_NULL, null=True, blank=True, related_name='offline_messages')
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


# ──────────────────────────────────────────────────────────
# Feature 7: Agent Departments
# ──────────────────────────────────────────────────────────
class Department(models.Model):
    """Agent departments for organized routing."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='departments')
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default='')
    color = models.CharField(max_length=7, default='#6366f1')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('organization', 'name')]
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def agent_count(self):
        return self.members.count()

    @property
    def online_agent_count(self):
        return self.members.filter(agent__is_available=True).count()


class DepartmentMember(models.Model):
    """Maps agents to departments."""
    department = models.ForeignKey(Department, on_delete=models.CASCADE, related_name='members')
    agent = models.ForeignKey(AgentProfile, on_delete=models.CASCADE, related_name='departments')
    is_lead = models.BooleanField(default=False)
    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = [('department', 'agent')]

    def __str__(self):
        return f"{self.agent.user.username} in {self.department.name}"


# ──────────────────────────────────────────────────────────
# Feature 8: SLA Management
# ──────────────────────────────────────────────────────────
class SLAPolicy(models.Model):
    """SLA policies with response/resolution time targets."""
    PRIORITY_CHOICES = [
        ('low', 'Low'), ('medium', 'Medium'), ('high', 'High'), ('urgent', 'Urgent'),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sla_policies')
    name = models.CharField(max_length=100)
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default='medium')
    first_response_minutes = models.PositiveIntegerField(default=5, help_text='Target first response time in minutes')
    resolution_minutes = models.PositiveIntegerField(default=60, help_text='Target resolution time in minutes')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['priority']
        verbose_name = 'SLA Policy'
        verbose_name_plural = 'SLA Policies'

    def __str__(self):
        return f"{self.name} ({self.priority})"


class SLABreach(models.Model):
    """Records when an SLA target is breached."""
    BREACH_TYPES = [
        ('first_response', 'First Response'),
        ('resolution', 'Resolution'),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='sla_breaches')
    chat = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='sla_breaches')
    policy = models.ForeignKey(SLAPolicy, on_delete=models.CASCADE, related_name='breaches')
    breach_type = models.CharField(max_length=20, choices=BREACH_TYPES)
    target_minutes = models.PositiveIntegerField()
    actual_minutes = models.PositiveIntegerField()
    breached_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-breached_at']

    def __str__(self):
        return f"SLA breach: {self.breach_type} on chat #{self.chat.room_id}"


# ──────────────────────────────────────────────────────────
# Feature 9: In-App Surveys / NPS
# ──────────────────────────────────────────────────────────
class Survey(models.Model):
    """Custom surveys and NPS forms."""
    SURVEY_TYPES = [
        ('nps', 'Net Promoter Score'),
        ('csat', 'Customer Satisfaction'),
        ('custom', 'Custom Survey'),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='surveys')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    survey_type = models.CharField(max_length=10, choices=SURVEY_TYPES, default='nps')
    is_active = models.BooleanField(default=True)
    show_after_chat = models.BooleanField(default=True, help_text='Show survey after chat closes')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.title

    @property
    def response_count(self):
        return self.responses.count()

    @property
    def avg_score(self):
        from django.db.models import Avg
        return self.responses.aggregate(avg=Avg('score'))['avg']


class SurveyQuestion(models.Model):
    """Individual questions in a survey."""
    QUESTION_TYPES = [
        ('rating', 'Rating (1-10)'),
        ('text', 'Text Answer'),
        ('choice', 'Multiple Choice'),
        ('yesno', 'Yes/No'),
    ]
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='questions')
    question_text = models.CharField(max_length=500)
    question_type = models.CharField(max_length=10, choices=QUESTION_TYPES, default='rating')
    choices = models.TextField(blank=True, default='', help_text='Comma-separated choices for multiple choice questions')
    order = models.PositiveIntegerField(default=0)
    is_required = models.BooleanField(default=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.question_text[:50]

    @property
    def choices_list(self):
        return [c.strip() for c in self.choices.split(',') if c.strip()] if self.choices else []


class SurveyResponse(models.Model):
    """A visitor's response to a survey."""
    survey = models.ForeignKey(Survey, on_delete=models.CASCADE, related_name='responses')
    visitor = models.ForeignKey(Visitor, on_delete=models.CASCADE, related_name='survey_responses')
    chat = models.ForeignKey(ChatRoom, on_delete=models.SET_NULL, null=True, blank=True, related_name='survey_responses')
    score = models.PositiveIntegerField(null=True, blank=True, help_text='NPS/CSAT score')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Response to {self.survey.title} by visitor {self.visitor.id}"


class SurveyAnswer(models.Model):
    """Individual answer to a survey question."""
    response = models.ForeignKey(SurveyResponse, on_delete=models.CASCADE, related_name='answers')
    question = models.ForeignKey(SurveyQuestion, on_delete=models.CASCADE, related_name='answers')
    answer_text = models.TextField(blank=True, default='')
    answer_rating = models.PositiveIntegerField(null=True, blank=True)

    def __str__(self):
        return f"Answer to {self.question.question_text[:30]}"


# ──────────────────────────────────────────────────────────
# Feature 1: AI Auto-Reply Bot
# ──────────────────────────────────────────────────────────
class AIBotConfig(models.Model):
    """AI auto-reply bot configuration per organization."""
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='ai_bot_config')
    is_enabled = models.BooleanField(default=False)
    bot_name = models.CharField(max_length=100, default='AI Assistant')
    greeting_message = models.TextField(default='Hi! I\'m an AI assistant. I can help answer your questions. How can I assist you today?')
    fallback_message = models.TextField(default='I\'m not sure about that. Let me connect you with a human agent.')
    handoff_keywords = models.TextField(default='agent,human,person,help,speak,talk', help_text='Comma-separated keywords that trigger agent handoff')
    max_auto_replies = models.PositiveIntegerField(default=5, help_text='Max AI replies before auto-handoff to agent')
    response_delay_seconds = models.PositiveIntegerField(default=2, help_text='Delay before AI responds (natural feel)')
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"AI Bot for {self.organization.name}"

    @property
    def handoff_keywords_list(self):
        return [k.strip().lower() for k in self.handoff_keywords.split(',') if k.strip()]


class AIBotKnowledge(models.Model):
    """Knowledge entries for the AI bot to reference."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='ai_knowledge')
    question = models.TextField(help_text='Common question or keywords')
    answer = models.TextField(help_text='Bot response for this question')
    keywords = models.TextField(blank=True, default='', help_text='Comma-separated matching keywords')
    priority = models.PositiveIntegerField(default=0, help_text='Higher priority matches first')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-priority', '-created_at']
        verbose_name_plural = 'AI Bot Knowledge'

    def __str__(self):
        return self.question[:50]

    @property
    def keywords_list(self):
        return [k.strip().lower() for k in self.keywords.split(',') if k.strip()] if self.keywords else []


# ──────────────────────────────────────────────────────────
# Feature 2: Chatbot Flow Builder
# ──────────────────────────────────────────────────────────
class ChatbotFlow(models.Model):
    """A chatbot conversation flow."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='chatbot_flows')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    is_active = models.BooleanField(default=False)
    trigger_type = models.CharField(max_length=20, choices=[
        ('greeting', 'On Chat Start'),
        ('keyword', 'On Keyword'),
        ('page', 'On Page Visit'),
        ('idle', 'On Idle Timeout'),
    ], default='greeting')
    trigger_value = models.CharField(max_length=500, blank=True, default='', help_text='Keyword or page URL for trigger')
    flow_data = models.JSONField(default=dict, help_text='JSON structure of the flow nodes')
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name


# ──────────────────────────────────────────────────────────
# Feature 3: Knowledge Base
# ──────────────────────────────────────────────────────────
class KBCategory(models.Model):
    """Knowledge base category."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='kb_categories')
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200)
    description = models.TextField(blank=True, default='')
    icon = models.CharField(max_length=50, default='fas fa-folder')
    order = models.PositiveIntegerField(default=0)
    is_published = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['order', 'name']
        unique_together = [('organization', 'slug')]
        verbose_name_plural = 'KB Categories'

    def __str__(self):
        return self.name

    @property
    def article_count(self):
        return self.articles.filter(is_published=True).count()


class KBArticle(models.Model):
    """Knowledge base article."""
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='kb_articles')
    category = models.ForeignKey(KBCategory, on_delete=models.CASCADE, related_name='articles')
    title = models.CharField(max_length=300)
    slug = models.SlugField(max_length=300)
    content = models.TextField()
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    is_published = models.BooleanField(default=True)
    views_count = models.PositiveIntegerField(default=0)
    helpful_yes = models.PositiveIntegerField(default=0)
    helpful_no = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = [('organization', 'slug')]

    def __str__(self):
        return self.title


# ──────────────────────────────────────────────────────────
# Feature 4: WhatsApp Integration
# ──────────────────────────────────────────────────────────
class WhatsAppConfig(models.Model):
    """WhatsApp Business API configuration."""
    organization = models.OneToOneField(Organization, on_delete=models.CASCADE, related_name='whatsapp_config')
    is_enabled = models.BooleanField(default=False)
    phone_number_id = models.CharField(max_length=100, blank=True, default='')
    access_token = models.TextField(blank=True, default='')
    verify_token = models.CharField(max_length=100, blank=True, default='')
    webhook_secret = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"WhatsApp config for {self.organization.name}"


class WhatsAppMessage(models.Model):
    """WhatsApp messages synced to the dashboard."""
    DIRECTION_CHOICES = [('inbound', 'Inbound'), ('outbound', 'Outbound')]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='whatsapp_messages')
    wa_message_id = models.CharField(max_length=200, unique=True)
    phone_number = models.CharField(max_length=20)
    contact_name = models.CharField(max_length=200, blank=True, default='')
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    content = models.TextField()
    msg_type = models.CharField(max_length=20, default='text')
    media_url = models.URLField(max_length=500, blank=True, default='')
    chat_room = models.ForeignKey(ChatRoom, on_delete=models.SET_NULL, null=True, blank=True, related_name='whatsapp_messages')
    status = models.CharField(max_length=20, default='received')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"WA {self.direction}: {self.phone_number}"


# ──────────────────────────────────────────────────────────
# Feature 5: Visitor Segmentation
# ──────────────────────────────────────────────────────────
class VisitorSegment(models.Model):
    """Custom visitor segments for targeted engagement."""
    CONDITION_TYPES = [
        ('visits_gte', 'Total visits >='),
        ('visits_lte', 'Total visits <='),
        ('country', 'Country is'),
        ('device', 'Device type is'),
        ('referrer', 'Referrer source is'),
        ('score_gte', 'Engagement score >='),
        ('score_lte', 'Engagement score <='),
        ('browser', 'Browser is'),
        ('returning', 'Is returning visitor'),
        ('has_chatted', 'Has started a chat'),
    ]
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name='visitor_segments')
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default='')
    color = models.CharField(max_length=7, default='#6366f1')
    conditions = models.JSONField(default=list, help_text='List of filter conditions')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_visitors(self):
        """Apply segment conditions and return matching visitors."""
        from django.db.models import Count
        qs = Visitor.objects.filter(organization=self.organization)
        for cond in (self.conditions or []):
            ctype = cond.get('type', '')
            cval = cond.get('value', '')
            if ctype == 'visits_gte':
                qs = qs.filter(total_visits__gte=int(cval))
            elif ctype == 'visits_lte':
                qs = qs.filter(total_visits__lte=int(cval))
            elif ctype == 'country':
                qs = qs.filter(country__iexact=cval)
            elif ctype == 'device':
                qs = qs.filter(device_type=cval)
            elif ctype == 'referrer':
                qs = qs.filter(referrer_source__iexact=cval)
            elif ctype == 'score_gte':
                qs = qs.filter(score__gte=int(cval))
            elif ctype == 'score_lte':
                qs = qs.filter(score__lte=int(cval))
            elif ctype == 'browser':
                qs = qs.filter(browser__icontains=cval)
            elif ctype == 'returning':
                qs = qs.filter(total_visits__gte=2)
            elif ctype == 'has_chatted':
                qs = qs.annotate(chat_count=Count('chat_rooms')).filter(chat_count__gte=1)
        return qs

    @property
    def visitor_count(self):
        return self.get_visitors().count()
