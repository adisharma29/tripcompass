"""Normalize all User.phone values to digits-only (no +, spaces, hyphens).

Canonical format: "919876543210" (digits-only with country code, no +).
Skips rows that would collide after normalization (requires manual resolution).
"""
import re

from django.db import migrations


def normalize_phones(apps, schema_editor):
    User = apps.get_model('users', 'User')
    for user in User.objects.exclude(phone='').exclude(phone__isnull=True):
        normalized = re.sub(r'\D', '', user.phone)
        if normalized == user.phone:
            continue  # Already clean
        # Check for collision before saving
        if User.objects.filter(phone=normalized).exclude(id=user.id).exists():
            # Skip — requires manual resolution
            continue
        user.phone = normalized
        user.save(update_fields=['phone'])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0002_alter_user_options_user_user_type_alter_user_email_and_more'),
    ]

    operations = [
        migrations.RunPython(normalize_phones, migrations.RunPython.noop),
    ]
