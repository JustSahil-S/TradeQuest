import json

from django.db import transaction
from django.http import JsonResponse
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import Position, Profile
from .yahoo_data import fetch_candles, fetch_latest_price, search_symbols

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
    Return selected-symbol series for Chart.js using Yahoo Finance.
    """
    symbol = (request.GET.get("symbol") or "AAPL").upper().strip()
    timeframe = (request.GET.get("timeframe") or "w").lower().strip()
    points_raw = request.GET.get("points") or "52"
    try:
        points = int(points_raw)
    except ValueError:
        return JsonResponse({"error": "Invalid points value"}, status=400)

    try:
        return JsonResponse(fetch_candles(symbol=symbol, timeframe=timeframe, points=points))
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def symbol_search(request):
    query = request.GET.get("q", "")
    try:
        return JsonResponse(search_symbols(query=query, limit=8))
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@login_required
def portfolio(request):
    positions = Position.objects.filter(user=request.user).order_by("symbol")
    items = []
    total_value = 0

    for p in positions:
        try:
            latest = fetch_latest_price(p.symbol)
            current_price = max(1, int(round(latest)))
        except Exception:
            current_price = 0

        current_value = current_price * p.quantity
        total_value += current_value
        items.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "average_cost_stardust": p.average_cost_stardust,
                "current_price_stardust": current_price,
                "current_value_stardust": current_value,
            }
        )

    return JsonResponse({"positions": items, "portfolio_value_stardust": total_value})


@login_required
@require_POST
@transaction.atomic
def buy_stock(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    symbol = (payload.get("symbol") or "").upper().strip()
    quantity_raw = payload.get("quantity")
    if not symbol:
        return JsonResponse({"error": "Symbol is required"}, status=400)

    try:
        quantity = int(quantity_raw)
    except (TypeError, ValueError):
        return JsonResponse({"error": "Quantity must be an integer"}, status=400)
    if quantity <= 0:
        return JsonResponse({"error": "Quantity must be greater than 0"}, status=400)

    try:
        market_price = fetch_latest_price(symbol)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    price_per_share = max(1, int(round(market_price)))
    total_cost = price_per_share * quantity

    profile = Profile.objects.select_for_update().get(user=request.user)
    if profile.stardust_balance < total_cost:
        return JsonResponse(
            {
                "error": (
                    f"Not enough stardust. Need {total_cost}, "
                    f"but you only have {profile.stardust_balance}."
                )
            },
            status=400,
        )

    position, created = Position.objects.select_for_update().get_or_create(
        user=request.user,
        symbol=symbol,
        defaults={"quantity": 0, "average_cost_stardust": price_per_share},
    )

    existing_qty = position.quantity
    new_qty = existing_qty + quantity
    if existing_qty == 0:
        new_avg = price_per_share
    else:
        prev_total = position.average_cost_stardust * existing_qty
        new_total = prev_total + (price_per_share * quantity)
        new_avg = int(round(new_total / new_qty))

    position.quantity = new_qty
    position.average_cost_stardust = new_avg
    position.save(update_fields=["quantity", "average_cost_stardust"])

    profile.stardust_balance -= total_cost
    profile.save(update_fields=["stardust_balance"])

    return JsonResponse(
        {
            "ok": True,
            "symbol": symbol,
            "quantity_bought": quantity,
            "price_per_share": price_per_share,
            "total_cost": total_cost,
            "owned_quantity": new_qty,
            "average_cost_stardust": new_avg,
            "remaining_stardust": profile.stardust_balance,
        }
    )
