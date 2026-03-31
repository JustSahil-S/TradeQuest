from django.db import migrations


def grant_initial_recon_powerup(apps, schema_editor):
    Profile = apps.get_model("stocks", "Profile")
    PowerUp = apps.get_model("stocks", "PowerUp")
    UserPowerUp = apps.get_model("stocks", "UserPowerUp")

    recon_powerup, _ = PowerUp.objects.get_or_create(
        code="RECON",
        defaults={
            "name": "Recon",
            "description": "Reveal the top 3 players' current holdings; snapshot refreshes when you deploy again.",
        },
    )

    for profile in Profile.objects.all().select_related("user"):
        item, created = UserPowerUp.objects.get_or_create(
            user=profile.user,
            powerup=recon_powerup,
            defaults={"quantity": 1},
        )
        if not created and int(item.quantity or 0) < 1:
            item.quantity = 1
            item.save(update_fields=["quantity"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("stocks", "0014_seed_recon_powerup"),
    ]

    operations = [
        migrations.RunPython(grant_initial_recon_powerup, noop_reverse),
    ]
