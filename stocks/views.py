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
    Return candle series for a symbol for Chart.js.

    We try a small list of popular tickers and return the first one Finnhub
    allows for the current API key/plan.
    """
    symbols_to_try = [
        "AAPL",
        "MSFT",
        "NVDA",
        "TSLA",
        "AMZN",
        "META",
        "AMD",
        "NFLX",
        "GOOGL",
        "SPY",
    ]

    last_exc = None
    for symbol in symbols_to_try:
        try:
            return JsonResponse(fetch_candles(symbol=symbol, resolution="D", days=30))
        except Exception as e:
            last_exc = e

    # If none of the tickers work, fall back (DEBUG) so the UI can be verified.
    if settings.DEBUG:
        fallback_symbol = "AAPL"
        series = mock_candles(symbol=fallback_symbol, days=30)
        series["note"] = f"Finnhub fallback (no symbol succeeded). Last error: {last_exc}"
        return JsonResponse(series)

    return JsonResponse({"error": str(last_exc) if last_exc else "Finnhub error"}, status=500)
