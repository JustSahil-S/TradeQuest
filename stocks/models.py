from decimal import Decimal

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
    stardust_balance = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("100.00000000"),
    )

    def __str__(self):
        return f"Profile(user={self.user.username})"


class Position(models.Model):
    """
    Tracks how many shares of a symbol a user owns.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="positions",
    )
    symbol = models.CharField(max_length=12)
    quantity = models.PositiveIntegerField(default=0)
    average_cost_stardust = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )
    # Market value (qty × price) at last buy/sell; P/L = current value − this (resets each trade).
    last_reset_value_stardust = models.DecimalField(
        max_digits=24,
        decimal_places=8,
        default=Decimal("0"),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "symbol"], name="unique_user_symbol_position")
        ]

    def __str__(self):
        return f"Position(user={self.user.username}, symbol={self.symbol}, qty={self.quantity})"


class Trade(models.Model):
    class Side(models.TextChoices):
        BUY = "BUY", "Buy"
        SELL = "SELL", "Sell"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trades",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # When time travel is used by a superuser, this records the simulated execution date.
    executed_as_of = models.DateField(null=True, blank=True)

    symbol = models.CharField(max_length=12)
    side = models.CharField(max_length=4, choices=Side.choices)
    quantity = models.PositiveIntegerField()

    price_per_share_stardust = models.DecimalField(max_digits=24, decimal_places=8)
    total_stardust = models.DecimalField(max_digits=24, decimal_places=8)

    def __str__(self):
        return f"Trade(user={self.user.username}, {self.side} {self.quantity} {self.symbol})"
