from django.conf import settings
from django.db import models


class Profile(models.Model):
    """
    Extra per-user data for TradeQuest.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    stardust_balance = models.PositiveIntegerField(default=100)

    def __str__(self):
        return f"Profile(user={self.user.username})"
