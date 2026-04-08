from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_coupon_subscription_billing_interval_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='subscription',
            name='pending_plan',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AddField(
            model_name='subscription',
            name='pending_interval',
            field=models.CharField(blank=True, default='', max_length=10),
        ),
    ]
