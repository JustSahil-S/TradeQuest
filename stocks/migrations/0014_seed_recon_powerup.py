from django.db import migrations


def seed_recon_powerup(apps, schema_editor):
    PowerUp = apps.get_model("stocks", "PowerUp")
    PowerUp.objects.get_or_create(
        code="RECON",
        defaults={
            "name": "Recon",
            "description": "Reveal the top 3 players' current holdings; snapshot refreshes when you deploy again.",
        },
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("stocks", "0013_recon_snapshot"),
    ]

    operations = [
        migrations.RunPython(seed_recon_powerup, noop_reverse),
    ]
