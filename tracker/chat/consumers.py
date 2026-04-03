import json
import logging
from urllib.parse import parse_qs
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from tracker.chat.security import verify_ws_token

logger = logging.getLogger(__name__)

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.room_id = self.scope['url_route']['kwargs']['room_id']
        self.room_group_name = f'chat_{self.room_id}'
        self.is_agent = False
        self.sender_name = 'Visitor'

        query = parse_qs(self.scope.get('query_string', b'').decode())
        token = (query.get('token') or [''])[0]
        user = self.scope.get('user')
        session = self.scope.get('session')
        session_key = session.session_key if session else None

        auth_data = await self.authorize_connection(
            token=token,
            room_id=self.room_id,
            user_id=getattr(user, 'id', None),
            is_authenticated=bool(getattr(user, 'is_authenticated', False)),
            is_superuser=bool(getattr(user, 'is_superuser', False)),
            session_key=session_key,
        )
        if not auth_data:
            await self.close(code=4003)
            return

        self.is_agent = auth_data['is_agent']
        self.sender_name = auth_data['sender_name']
        self.org_id = auth_data.get('org_id')
        self.notify_group = f'agents_notify_{self.org_id}' if self.org_id else 'agents_notify'

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        if self.is_agent:
            await self.channel_layer.group_add(self.notify_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        if self.is_agent:
            await self.channel_layer.group_discard(self.notify_group, self.channel_name)

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return
        message_type = data.get('type', 'chat_message')

        if message_type == 'chat_message':
            content = data.get('message', '')
            sender_type = 'agent' if self.is_agent else 'visitor'
            sender_name = self.sender_name
            if data.get('sender_type') == 'system' and self.is_agent:
                sender_type = 'system'
                sender_name = 'System'
            msg_type = data.get('msg_type', 'text')
            file_url = data.get('file_url', '')
            file_name = data.get('file_name', '')

            # Only save text messages (file messages saved via API)
            if msg_type == 'text' and sender_type != 'system':
                await self.save_message(content, sender_type, sender_name)
            elif sender_type == 'system':
                await self.save_message(content, 'system', 'System')

            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_message',
                    'message': content,
                    'sender_type': sender_type,
                    'sender_name': sender_name,
                    'msg_type': msg_type,
                    'file_url': file_url,
                    'file_name': file_name,
                    'timestamp': timezone.now().isoformat(),
                }
            )

            # Notify agents dashboard
            if sender_type == 'visitor':
                await self.channel_layer.group_send(
                    self.notify_group,
                    {
                        'type': 'new_message_notify',
                        'room_id': self.room_id,
                        'message': content,
                        'sender_type': sender_type,
                        'sender_name': sender_name,
                    }
                )

        elif message_type == 'typing':
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'typing_indicator',
                    'sender_type': 'agent' if self.is_agent else 'visitor',
                    'is_typing': data.get('is_typing', False),
                    'preview_text': data.get('preview_text', ''),
                    'sender_name': self.sender_name,
                }
            )

        elif message_type == 'read_receipt':
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'read_receipt',
                    'sender_type': 'agent' if self.is_agent else 'visitor',
                    'sender_name': self.sender_name,
                }
            )

        # WebRTC signaling for screen share
        elif message_type in ('screen_share_request', 'screen_share_offer', 'screen_share_answer', 'ice_candidate', 'screen_share_stop', 'cobrowse_update'):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'webrtc_signal',
                    'message_type': message_type,
                    'sender_type': 'agent' if self.is_agent else 'visitor',
                    'data': data.get('data', {}),
                }
            )

        elif message_type == 'close_chat':
            await self.close_chat()
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'chat_closed',
                    'message': 'Chat has been closed.',
                }
            )

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': event['message'],
            'sender_type': event['sender_type'],
            'sender_name': event['sender_name'],
            'msg_type': event.get('msg_type', 'text'),
            'file_url': event.get('file_url', ''),
            'file_name': event.get('file_name', ''),
            'timestamp': event['timestamp'],
        }))

    async def typing_indicator(self, event):
        await self.send(text_data=json.dumps({
            'type': 'typing',
            'sender_type': event['sender_type'],
            'is_typing': event['is_typing'],
            'preview_text': event.get('preview_text', ''),
            'sender_name': event.get('sender_name', ''),
        }))

    async def read_receipt(self, event):
        await self.send(text_data=json.dumps({
            'type': 'read_receipt',
            'sender_type': event['sender_type'],
            'sender_name': event.get('sender_name', ''),
        }))

    async def webrtc_signal(self, event):
        await self.send(text_data=json.dumps({
            'type': event['message_type'],
            'sender_type': event['sender_type'],
            'data': event.get('data', {}),
        }))

    async def chat_closed(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_closed',
            'message': event['message'],
        }))

    async def chat_transferred(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_transferred',
            'message': event['message'],
            'from_agent': event['from_agent'],
            'to_agent': event['to_agent'],
            'to_agent_id': event['to_agent_id'],
        }))

    async def internal_note(self, event):
        # Only send internal notes to agents, never to visitors
        if self.is_agent:
            await self.send(text_data=json.dumps({
                'type': 'internal_note',
                'note_id': event['note_id'],
                'agent_name': event['agent_name'],
                'agent_id': event['agent_id'],
                'content': event['content'],
                'created_at': event['created_at'],
            }))

    async def new_message_notify(self, event):
        if self.is_agent:
            await self.send(text_data=json.dumps({
                'type': 'new_message_notify',
                'room_id': event['room_id'],
                'message': event['message'],
                'sender_type': event['sender_type'],
                'sender_name': event['sender_name'],
            }))

    @database_sync_to_async
    def save_message(self, content, sender_type, sender_name):
        from .models import ChatRoom, Message
        try:
            room = ChatRoom.objects.get(room_id=self.room_id)
            message = Message.objects.create(
                room=room,
                sender_type=sender_type,
                sender_name=sender_name,
                content=content,
            )
            room.save(update_fields=['updated_at'])
            return message
        except ChatRoom.DoesNotExist:
            return None

    @database_sync_to_async
    def close_chat(self):
        from .models import ChatRoom
        try:
            room = ChatRoom.objects.get(room_id=self.room_id)
            room.status = 'closed'
            room.closed_at = timezone.now()
            room.save()
        except ChatRoom.DoesNotExist:
            pass

    @database_sync_to_async
    def authorize_connection(self, token, room_id, user_id, is_authenticated, is_superuser, session_key):
        from .models import ChatRoom
        from django.contrib.auth.models import User

        payload = verify_ws_token(token)
        if not payload or payload.get('room_id') != str(room_id):
            return None

        role = payload.get('role')
        principal = str(payload.get('principal', ''))

        try:
            room = ChatRoom.objects.select_related('agent', 'visitor').get(room_id=room_id)
        except ChatRoom.DoesNotExist:
            return None

        if role == 'agent':
            if not is_authenticated or not user_id or principal != str(user_id):
                return None

            if room.agent_id and room.agent_id != user_id and not is_superuser:
                return None

            if not room.agent_id:
                room.agent_id = user_id
                room.status = 'active'
                room.save(update_fields=['agent', 'status', 'updated_at'])

            user = User.objects.filter(id=user_id).first()
            sender_name = (user.get_full_name() if user else '') or (user.username if user else 'Agent')
            return {'is_agent': True, 'sender_name': sender_name, 'org_id': room.organization_id}

        if role == 'visitor':
            if not session_key or principal != session_key:
                return None
            if room.visitor.session_key != session_key:
                return None
            return {'is_agent': False, 'sender_name': room.visitor_name or 'Visitor', 'org_id': room.organization_id}

        return None


class DashboardConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        user = self.scope.get('user')
        if not getattr(user, 'is_authenticated', False):
            await self.close(code=4003)
            return
        # Get org-scoped group names
        self.org_id = await self._get_org_id(user.id)
        self.dashboard_group = f'dashboard_updates_{self.org_id}' if self.org_id else 'dashboard_updates'
        self.notify_group = f'agents_notify_{self.org_id}' if self.org_id else 'agents_notify'
        await self.channel_layer.group_add(self.dashboard_group, self.channel_name)
        await self.channel_layer.group_add(self.notify_group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard(self.dashboard_group, self.channel_name)
        await self.channel_layer.group_discard(self.notify_group, self.channel_name)

    @database_sync_to_async
    def _get_org_id(self, user_id):
        from .models import AgentProfile
        profile = AgentProfile.objects.filter(user_id=user_id).first()
        return profile.organization_id if profile and profile.organization_id else None

    async def receive(self, text_data):
        data = json.loads(text_data)
        if data.get('type') == 'agent_join':
            room_id = data.get('room_id')
            await self.assign_agent(room_id, self.scope['user'].id)
            await self.channel_layer.group_send(
                self.dashboard_group,
                {
                    'type': 'dashboard_update',
                    'reason': 'agent_joined',
                    'room_id': room_id,
                }
            )

    async def new_message_notify(self, event):
        await self.send(text_data=json.dumps(event))

    async def dashboard_update(self, event):
        await self.send(text_data=json.dumps(event))

    @database_sync_to_async
    def assign_agent(self, room_id, user_id):
        from .models import ChatRoom
        try:
            room = ChatRoom.objects.get(room_id=room_id)
            if room.status == 'waiting' or not room.agent_id:
                room.agent_id = user_id
                room.status = 'active'
                room.save(update_fields=['agent', 'status', 'updated_at'])
        except ChatRoom.DoesNotExist:
            logger.warning("Failed to assign agent for room %s", room_id)
