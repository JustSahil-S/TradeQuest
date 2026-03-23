from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render
from django.urls import reverse

@login_required
def home(request):
    return render(request, "stocks/index.html")


def signup(request):
    # Basic registration using Django's built-in password validation.
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, "Account created successfully.")
            return redirect(reverse("home"))
    else:
        form = UserCreationForm()

    return render(request, "registration/signup.html", {"form": form})
