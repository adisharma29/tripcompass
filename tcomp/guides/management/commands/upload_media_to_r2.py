"""
Upload local media files to the configured storage backend (R2 in prod).

Reads images from the local /app/media/ directory and re-saves them
through Django's storage backend so they land in R2.
"""
import os

from django.core.files import File
from django.core.management.base import BaseCommand

from guides.models import ExperienceImage, Mood


class Command(BaseCommand):
    help = 'Upload local media files to the configured storage backend'

    def add_arguments(self, parser):
        parser.add_argument(
            '--media-dir',
            default='/app/media',
            help='Path to local media directory',
        )

    def handle(self, *args, **options):
        media_dir = options['media_dir']

        # Upload experience images
        images = ExperienceImage.objects.all()
        img_count = 0
        for img in images:
            # Current DB value is like "experience_images/filename.jpg"
            local_path = os.path.join(media_dir, str(img.image))
            if not os.path.exists(local_path):
                self.stdout.write(self.style.WARNING(f'  Missing: {local_path}'))
                continue

            with open(local_path, 'rb') as f:
                filename = os.path.basename(str(img.image))
                img.image.save(filename, File(f), save=True)
                img_count += 1
                self.stdout.write(f'  Uploaded: {filename}')

        self.stdout.write(self.style.SUCCESS(f'Experience images: {img_count} uploaded'))

        # Upload mood illustrations
        moods = Mood.objects.exclude(illustration='')
        mood_count = 0
        for mood in moods:
            local_path = os.path.join(media_dir, str(mood.illustration))
            if not os.path.exists(local_path):
                self.stdout.write(self.style.WARNING(f'  Missing: {local_path}'))
                continue

            with open(local_path, 'rb') as f:
                filename = os.path.basename(str(mood.illustration))
                mood.illustration.save(filename, File(f), save=True)
                mood_count += 1
                self.stdout.write(f'  Uploaded: {filename}')

        self.stdout.write(self.style.SUCCESS(f'Mood illustrations: {mood_count} uploaded'))
        self.stdout.write(self.style.SUCCESS('\nDone!'))
