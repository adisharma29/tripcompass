import base64

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.core.management.base import BaseCommand
from py_vapid import Vapid


class Command(BaseCommand):
    help = 'Generate VAPID key pair for Web Push notifications'

    def handle(self, *args, **options):
        vapid = Vapid()
        vapid.generate_keys()

        # Raw 65-byte uncompressed EC point → base64url (for browser pushManager.subscribe)
        raw_public = vapid.public_key.public_bytes(
            encoding=Encoding.X962,
            format=PublicFormat.UncompressedPoint,
        )
        app_server_key = base64.urlsafe_b64encode(raw_public).rstrip(b'=').decode()

        self.stdout.write('\nAdd these to your backend .env file:\n')
        self.stdout.write(f'VAPID_PRIVATE_KEY={vapid.private_pem().decode().strip()}')
        self.stdout.write(f'VAPID_PUBLIC_KEY={vapid.public_pem().decode().strip()}')
        self.stdout.write('\nAdd this to your frontend .env.local / .dev.vars:\n')
        self.stdout.write(f'NEXT_PUBLIC_VAPID_PUBLIC_KEY={app_server_key}')
        self.stdout.write('\n')
