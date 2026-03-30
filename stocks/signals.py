from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import PowerUp, Profile, UserPowerUp


@receiver(post_save, sender=get_user_model())
def ensure_profile_exists(sender, instance, created, **kwargs):
    """
    Ensure every Django User has a Profile (with default stardust=100).
    """
    profile, created_profile = Profile.objects.get_or_create(user=instance)

    # Grant starting power-ups to brand new users.
    if created_profile:
        shield_powerup, _ = PowerUp.objects.get_or_create(
            code="STARDUST_SHIELD",
            defaults={
                "name": "Stardust Shield",
                "description": "Auto-sells a protected stock when price falls below your trigger.",
            },
        )
        UserPowerUp.objects.get_or_create(user=instance, powerup=shield_powerup, defaults={"quantity": 1})

