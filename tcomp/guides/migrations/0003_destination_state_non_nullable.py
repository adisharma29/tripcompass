import django.db.models.deletion
from django.db import migrations, models


def backfill_destination_state(apps, schema_editor):
    """Ensure all destinations have a state before making the field non-nullable."""
    Destination = apps.get_model('guides', 'Destination')
    nulls = Destination.objects.filter(state__isnull=True)
    if nulls.exists():
        raise Exception(
            f"{nulls.count()} Destination(s) have NULL state. "
            "Assign a state to each destination before running this migration."
        )


class Migration(migrations.Migration):

    dependencies = [
        ('location', '0002_alter_city_unique_together'),
        ('guides', '0002_alter_destination_state_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill_destination_state, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='destination',
            name='state',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='destinations',
                to='location.state',
            ),
        ),
    ]
