from django.conf import settings
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render
from django.urls import reverse

from .finnhub import fetch_candles, mock_candles

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


@login_required
def aapl_chart(request):
    """
    Return candle series for AAPL for Chart.js.
    """
    symbol = "AAPL"
    try:
        series = fetch_candles(symbol=symbol, resolution="D", days=30)
    except Exception as e:
        if settings.DEBUG:
            # For local development: fall back to mock data so the UI wiring works.
            series = mock_candles(symbol=symbol, days=30)
            series["note"] = f"Finnhub fallback due to: {e}"
        else:
            # Keep the error payload JSON so the front-end can display/log it.
            return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse(series)
