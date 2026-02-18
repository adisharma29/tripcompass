from django.contrib.auth.models import AbstractUser
from django.db import models

from .managers import UserManager


class User(AbstractUser):
    class UserType(models.TextChoices):
        STAFF = 'STAFF', 'Staff'
        GUEST = 'GUEST', 'Guest'

    username = None
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=20, blank=True, default='')
    avatar = models.ImageField(upload_to='avatars/', blank=True)
    bio = models.TextField(blank=True)
    user_type = models.CharField(
        max_length=10,
        choices=UserType.choices,
        default=UserType.STAFF,
    )
    preferred_destinations = models.ManyToManyField(
        'guides.Destination', blank=True, related_name='preferred_by'
    )

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    objects = UserManager()

    class Meta:
        constraints = [
            # Unique email for non-blank values (allows multiple guest rows with email='')
            models.UniqueConstraint(
                fields=['email'],
                condition=models.Q(email__gt=''),
                name='users_user_email_unique',
            ),
            # Unique phone for non-blank values (allows multiple rows with phone='')
            models.UniqueConstraint(
                fields=['phone'],
                condition=models.Q(phone__gt=''),
                name='users_user_phone_unique',
            ),
        ]

    def __str__(self):
        if self.email:
            return self.email
        return f'{self.phone} ({self.user_type})'
