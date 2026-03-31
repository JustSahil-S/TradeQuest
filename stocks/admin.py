from django.contrib import admin

from .models import (
    MultiplyProfitBoost,
    Position,
    PowerUp,
    Profile,
    ReconSnapshot,
    StardustShield,
    Trade,
    UserPowerUp,
)


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "stardust_balance")
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)
    ordering = ("user__username",)


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("user", "symbol", "quantity", "average_cost_stardust", "last_reset_value_stardust")
    list_filter = ("symbol",)
    search_fields = ("user__username", "symbol")
    autocomplete_fields = ("user",)
    ordering = ("user__username", "symbol")


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "side", "symbol", "quantity", "total_stardust", "executed_as_of")
    list_filter = ("side", "symbol")
    search_fields = ("user__username", "symbol")
    autocomplete_fields = ("user",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(PowerUp)
class PowerUpAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "created_at")
    search_fields = ("code", "name", "description")
    ordering = ("code",)


@admin.register(UserPowerUp)
class UserPowerUpAdmin(admin.ModelAdmin):
    list_display = ("user", "powerup", "quantity", "acquired_at")
    list_filter = ("powerup",)
    search_fields = ("user__username", "powerup__code", "powerup__name")
    autocomplete_fields = ("user", "powerup")
    ordering = ("user__username", "powerup__code")


@admin.register(StardustShield)
class StardustShieldAdmin(admin.ModelAdmin):
    list_display = ("user", "symbol", "trigger_price_stardust", "is_active", "created_at", "triggered_at")
    list_filter = ("is_active", "symbol")
    search_fields = ("user__username", "symbol")
    autocomplete_fields = ("user",)
    ordering = ("-created_at",)


@admin.register(MultiplyProfitBoost)
class MultiplyProfitBoostAdmin(admin.ModelAdmin):
    list_display = ("user", "symbol", "multiplier", "is_active", "created_at", "consumed_at")
    list_filter = ("is_active", "symbol")
    search_fields = ("user__username", "symbol")
    autocomplete_fields = ("user",)
    ordering = ("-created_at",)


@admin.register(ReconSnapshot)
class ReconSnapshotAdmin(admin.ModelAdmin):
    list_display = ("user", "captured_at")
    search_fields = ("user__username",)
    autocomplete_fields = ("user",)
    ordering = ("-captured_at",)
