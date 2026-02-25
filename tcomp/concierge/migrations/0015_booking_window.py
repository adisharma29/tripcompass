from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('concierge', '0014_add_event_model'),
    ]

    operations = [
        # Hotel defaults
        migrations.AddField(
            model_name='hotel',
            name='default_booking_opens_hours',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Default hours before event start when bookings open. 0 = always open.',
            ),
        ),
        migrations.AddField(
            model_name='hotel',
            name='default_booking_closes_hours',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Default hours before event start when bookings close. 0 = no cutoff.',
            ),
        ),
        # Event overrides
        migrations.AddField(
            model_name='event',
            name='booking_opens_hours',
            field=models.PositiveIntegerField(
                null=True, blank=True,
                help_text='Hours before event start when bookings open. Null = use hotel default.',
            ),
        ),
        migrations.AddField(
            model_name='event',
            name='booking_closes_hours',
            field=models.PositiveIntegerField(
                null=True, blank=True,
                help_text='Hours before event start when bookings close. Null = use hotel default.',
            ),
        ),
    ]
