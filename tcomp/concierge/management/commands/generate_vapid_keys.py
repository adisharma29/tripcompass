from django.core.management.base import BaseCommand
from py_vapid import Vapid


class Command(BaseCommand):
    help = 'Generate VAPID key pair for Web Push notifications'

    def handle(self, *args, **options):
        vapid = Vapid()
        vapid.generate_keys()

        self.stdout.write('\nAdd these to your .env file:\n')
        self.stdout.write(f'VAPID_PRIVATE_KEY={vapid.private_pem().decode().strip()}')
        self.stdout.write(f'VAPID_PUBLIC_KEY={vapid.public_pem().decode().strip()}')
        self.stdout.write('\n')
