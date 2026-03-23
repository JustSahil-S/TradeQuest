from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render
from django.urls import reverse

from .yahoo_data import fetch_weekly_candles, search_symbols

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
    Return weekly selected-symbol series for Chart.js using Yahoo Finance.
    """
    symbol = (request.GET.get("symbol") or "AAPL").upper().strip()
    try:
        return JsonResponse(fetch_weekly_candles(symbol=symbol, points=52))
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def symbol_search(request):
    query = request.GET.get("q", "")
    try:
        return JsonResponse(search_symbols(query=query, limit=8))
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
