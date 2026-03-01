from django.db import migrations


def seed_template(apps, schema_editor):
    WhatsAppTemplate = apps.get_model('concierge', 'WhatsAppTemplate')
    WhatsAppTemplate.objects.get_or_create(
        hotel=None,
        template_type='REQUEST_STATUS_UPDATE',
        is_active=True,
        defaults={
            'gupshup_template_id': 'fd1f77b3-8e6c-45e9-8bc1-b0765bb6d901',
            'name': 'Request Status Update - Default',
            'body_text': (
                "Hi {{1}}, here's an update on your request.\n\n"
                "*{{2}}*\n"
                "At {{3}} \u2014 your request has been {{4}}."
            ),
            'footer_text': 'Powered by Refuje',
            'buttons': [{'type': 'quick_reply', 'label': 'View details'}],
            'variables': [
                {'index': 1, 'key': 'guest_name', 'label': 'Guest name'},
                {'index': 2, 'key': 'item_name', 'label': 'Experience/event/offering name'},
                {'index': 3, 'key': 'hotel_name', 'label': 'Hotel name'},
                {'index': 4, 'key': 'status_label', 'label': 'Status text'},
            ],
        },
    )


def reverse_seed(apps, schema_editor):
    WhatsAppTemplate = apps.get_model('concierge', 'WhatsAppTemplate')
    WhatsAppTemplate.objects.filter(
        hotel=None, template_type='REQUEST_STATUS_UPDATE',
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('concierge', '0040_add_requires_room_number_to_experience_event'),
    ]

    operations = [
        migrations.RunPython(seed_template, reverse_seed),
    ]
