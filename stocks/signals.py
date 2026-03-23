from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=get_user_model())
def ensure_profile_exists(sender, instance, created, **kwargs):
    """
    Ensure every Django User has a Profile (with default stardust=100).
    """
    Profile.objects.get_or_create(user=instance)

