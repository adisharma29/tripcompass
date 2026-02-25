"""Add event-scoped notification routing.

- Make NotificationRoute.department nullable (was required)
- Add NotificationRoute.event FK (nullable)
- Add Event.notify_department boolean (default True)
- Add scope constraints: dept XOR event, no experience on event routes
- Add uniqueness constraint for event routes
- Update existing dept-wide uniqueness condition to exclude event routes
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('concierge', '0026_migrate_sms_to_whatsapp_values'),
    ]

    operations = [
        # 1. Make department nullable
        migrations.AlterField(
            model_name='notificationroute',
            name='department',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='notification_routes',
                to='concierge.department',
            ),
        ),

        # 2. Add event FK
        migrations.AddField(
            model_name='notificationroute',
            name='event',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='notification_routes',
                to='concierge.event',
            ),
        ),

        # 3. Add notify_department to Event
        migrations.AddField(
            model_name='event',
            name='notify_department',
            field=models.BooleanField(
                default=True,
                help_text='When True, also notify the resolved department contacts for requests on this event.',
            ),
        ),

        # 4. Remove old dept-wide uniqueness (condition changed to also exclude event routes)
        migrations.RemoveConstraint(
            model_name='notificationroute',
            name='unique_route_dept_wide',
        ),

        # 5. Re-add dept-wide uniqueness with updated condition
        migrations.AddConstraint(
            model_name='notificationroute',
            constraint=models.UniqueConstraint(
                condition=models.Q(experience__isnull=True, event__isnull=True),
                fields=['department', 'channel', 'target'],
                name='unique_route_dept_wide',
            ),
        ),

        # 6. Event route uniqueness: one per (event, channel, target)
        migrations.AddConstraint(
            model_name='notificationroute',
            constraint=models.UniqueConstraint(
                condition=models.Q(event__isnull=False),
                fields=['event', 'channel', 'target'],
                name='unique_event_channel_target',
            ),
        ),

        # 7. Scope exclusivity: exactly one of department or event
        migrations.AddConstraint(
            model_name='notificationroute',
            constraint=models.CheckConstraint(
                check=(
                    models.Q(department__isnull=False, event__isnull=True)
                    | models.Q(department__isnull=True, event__isnull=False)
                ),
                name='route_scope_dept_xor_event',
            ),
        ),

        # 8. Experience only valid on department-scoped routes
        migrations.AddConstraint(
            model_name='notificationroute',
            constraint=models.CheckConstraint(
                check=models.Q(event__isnull=True) | models.Q(experience__isnull=True),
                name='route_no_experience_on_event_scope',
            ),
        ),
    ]
