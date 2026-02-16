"""
Upload local media files to the configured storage backend (R2 in prod).

Creates ExperienceImage DB records using experience_images.json mapping,
uploads files from the local media directory, and uploads mood illustrations.
"""
import json
import os

from django.core.files import File
from django.core.management.base import BaseCommand

from guides.models import Experience, ExperienceImage, Mood


class Command(BaseCommand):
    help = 'Upload local media files to the configured storage backend'

    def add_arguments(self, parser):
        parser.add_argument(
            '--media-dir',
            default='/app/media',
            help='Path to local media directory',
        )
        parser.add_argument(
            '--data-dir',
            default='/data',
            help='Path to data directory (for experience_images.json)',
        )

    def handle(self, *args, **options):
        media_dir = options['media_dir']
        data_dir = options['data_dir']

        self._upload_experience_images(media_dir, data_dir)
        self._upload_mood_illustrations(media_dir)

        self.stdout.write(self.style.SUCCESS('\nDone!'))

    def _upload_experience_images(self, media_dir, data_dir):
        img_dir = os.path.join(media_dir, 'experience_images')
        images_json = os.path.join(data_dir, 'experience_images.json')

        if not os.path.isdir(img_dir):
            self.stdout.write(self.style.WARNING(
                f'No experience_images directory at {img_dir}'
            ))
            return

        if not os.path.exists(images_json):
            self.stdout.write(self.style.WARNING(
                f'No experience_images.json at {images_json}'
            ))
            return

        with open(images_json) as f:
            images_data = json.load(f)

        created_count = 0
        uploaded_count = 0

        for code, image_paths in images_data.items():
            experience = Experience.objects.filter(code=code).first()
            if not experience:
                self.stdout.write(self.style.WARNING(f'  No experience: {code}'))
                continue

            for i, img_rel_path in enumerate(image_paths):
                filename = os.path.basename(img_rel_path)
                local_path = os.path.join(img_dir, filename)

                if not os.path.exists(local_path):
                    self.stdout.write(self.style.WARNING(
                        f'  Missing file: {filename}'
                    ))
                    continue

                # Create DB record if needed
                img_obj, created = ExperienceImage.objects.get_or_create(
                    experience=experience,
                    alt_text=filename,
                    defaults={'sort_order': i},
                )
                if created:
                    created_count += 1

                # Upload to storage backend
                with open(local_path, 'rb') as f:
                    img_obj.image.save(filename, File(f), save=True)
                    uploaded_count += 1
                    self.stdout.write(f'  Uploaded: {code}/{filename}')

        self.stdout.write(self.style.SUCCESS(
            f'Experience images: {created_count} created, {uploaded_count} uploaded'
        ))

    def _upload_mood_illustrations(self, media_dir):
        illust_dir = os.path.join(media_dir, 'mood_illustrations')
        if not os.path.isdir(illust_dir):
            self.stdout.write(self.style.WARNING(
                f'No mood_illustrations directory at {illust_dir}'
            ))
            return

        uploaded_count = 0

        for filename in sorted(os.listdir(illust_dir)):
            filepath = os.path.join(illust_dir, filename)
            if not os.path.isfile(filepath):
                continue

            # Match mood by slug in filename
            matched_mood = None
            for mood in Mood.objects.all():
                if mood.slug.replace('-', '') in filename.lower().replace('-', ''):
                    matched_mood = mood
                    break

            if not matched_mood:
                self.stdout.write(self.style.WARNING(
                    f'  No matching mood for: {filename}'
                ))
                continue

            with open(filepath, 'rb') as f:
                matched_mood.illustration.save(filename, File(f), save=True)
                uploaded_count += 1
                self.stdout.write(f'  Uploaded: {filename} -> {matched_mood.name}')

        self.stdout.write(self.style.SUCCESS(
            f'Mood illustrations: {uploaded_count} uploaded'
        ))
