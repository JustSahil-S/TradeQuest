import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create an admin/superuser from env vars or command args."

    def add_arguments(self, parser):
        parser.add_argument("--username", default=os.getenv("DJANGO_SUPERUSER_USERNAME"))
        parser.add_argument("--email", default=os.getenv("DJANGO_SUPERUSER_EMAIL"))
        parser.add_argument("--password", default=os.getenv("DJANGO_SUPERUSER_PASSWORD"))

    def handle(self, *args, **options):
        username = options["username"]
        email = options["email"]
        password = options["password"]

        if not username or not email or not password:
            raise ValueError(
                "Missing admin credentials. Provide --username/--email/--password "
                "or set DJANGO_SUPERUSER_USERNAME/DJANGO_SUPERUSER_EMAIL/DJANGO_SUPERUSER_PASSWORD."
            )

        User = get_user_model()
        user, created = User.objects.get_or_create(username=username, defaults={"email": email})
        if not user.email:
            user.email = email

        user.is_staff = True
        user.is_superuser = True
        user.set_password(password)
        user.save()

        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} admin user: {username}"))

