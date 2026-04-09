from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_subscription_pending_plan'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='allowed_domains',
            field=models.TextField(blank=True, default='', help_text='Comma or newline separated domains (e.g. example.com)'),
        ),
        migrations.AddField(
            model_name='organization',
            name='allowed_domains_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='organization',
            name='blocked_countries',
            field=models.TextField(blank=True, default='', help_text='Comma or newline separated country names/codes (e.g. IN,US)'),
        ),
        migrations.AddField(
            model_name='organization',
            name='blocked_countries_enabled',
            field=models.BooleanField(default=False),
        ),
    ]
