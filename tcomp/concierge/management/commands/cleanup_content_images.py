from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Delete content images not referenced by any model HTML field'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='List orphaned files without deleting them',
        )
        parser.add_argument(
            '--min-age',
            type=int,
            default=24,
            help='Minimum file age in hours before eligible for deletion (default: 24)',
        )

    def handle(self, *args, **options):
        from concierge.tasks import cleanup_orphaned_content_images

        dry_run = options['dry_run']
        min_age = options['min_age']

        if dry_run:
            self.stdout.write('Running in dry-run mode...\n')

        deleted, total = cleanup_orphaned_content_images(
            min_age_hours=min_age,
            dry_run=dry_run,
        )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f'{deleted} orphaned file(s) found ({total} total scanned)'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'{deleted} orphaned file(s) deleted ({total} total scanned)'
                )
            )
