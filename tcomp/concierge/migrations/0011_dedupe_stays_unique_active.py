"""Deduplicate active GuestStay records and add a partial unique constraint
ensuring at most one active stay per (guest, hotel)."""

from django.db import migrations, models


def dedupe_active_stays(apps, schema_editor):
    """For each (guest, hotel) pair with multiple active stays, keep the most
    recent one and deactivate the rest."""
    GuestStay = apps.get_model('concierge', 'GuestStay')
    from django.db.models import Count, Max

    dupes = (
        GuestStay.objects
        .filter(is_active=True)
        .values('guest', 'hotel')
        .annotate(cnt=Count('id'), latest=Max('id'))
        .filter(cnt__gt=1)
    )
    for row in dupes:
        GuestStay.objects.filter(
            guest_id=row['guest'],
            hotel_id=row['hotel'],
            is_active=True,
        ).exclude(
            id=row['latest'],
        ).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('concierge', '0010_experience_unique_dept_slug'),
    ]

    operations = [
        migrations.RunPython(dedupe_active_stays, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='gueststay',
            constraint=models.UniqueConstraint(
                fields=['guest', 'hotel'],
                condition=models.Q(is_active=True),
                name='unique_active_stay_per_guest_hotel',
            ),
        ),
    ]
