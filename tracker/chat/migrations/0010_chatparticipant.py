from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('chat', '0009_aibotconfig_aibotknowledge_chatbotflow_department_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChatParticipant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('is_primary', models.BooleanField(default=False)),
                ('joined_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('left_at', models.DateTimeField(blank=True, null=True)),
                ('room', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='participants', to='chat.chatroom')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='chat_participations', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['joined_at'],
                'indexes': [models.Index(fields=['room', 'user'], name='chat_chatpa_room_id_b3a6cd_idx')],
                'unique_together': {('room', 'user')},
            },
        ),
    ]
