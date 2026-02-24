"""Data migration: map legacy SMS/EMAIL_SMS escalation channel values to WHATSAPP/EMAIL_WHATSAPP."""

from django.db import migrations


def migrate_sms_to_whatsapp(apps, schema_editor):
    Hotel = apps.get_model('concierge', 'Hotel')
    mapping = {
        'SMS': 'WHATSAPP',
        'EMAIL_SMS': 'EMAIL_WHATSAPP',
    }
    for old_val, new_val in mapping.items():
        updated = Hotel.objects.filter(escalation_fallback_channel=old_val).update(
            escalation_fallback_channel=new_val,
        )
        if updated:
            print(f'  Migrated {updated} hotel(s) from {old_val} to {new_val}')


class Migration(migrations.Migration):

    dependencies = [
        ('concierge', '0025_department_event_gallery_images'),
    ]

    operations = [
        migrations.RunPython(migrate_sms_to_whatsapp, migrations.RunPython.noop),
    ]
