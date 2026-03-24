import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

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

# Smallest stardust unit (8 decimal places) for exact arithmetic.
DUST_Q = Decimal("0.00000001")
DUST_DISPLAY_Q = Decimal("0.01")


def _dust_from_float(price: float) -> Decimal:
    """Convert a market USD float to stardust per share (exact Decimal, 8 dp)."""
    d = Decimal(str(float(price)))
    if d < DUST_Q:
        d = DUST_Q
    return d.quantize(DUST_Q, rounding=ROUND_HALF_UP)


def _dust_str(d: Decimal) -> str:
    """Serialize stardust for JSON (fixed 8 dp, no scientific notation)."""
    return format(d.quantize(DUST_Q, rounding=ROUND_HALF_UP), "f")


def _dust_display_str(d: Decimal) -> str:
    """Human-readable stardust for error messages (hundredths)."""
    q = d.quantize(DUST_DISPLAY_Q, rounding=ROUND_HALF_UP)
    return f"{q:.2f}"


def _ollama_chat(ollama_host: str, model: str, messages: list[dict]) -> str:
    """
    Call Ollama's non-streaming chat endpoint and return assistant text.
    Expected Ollama endpoint: POST {host}/api/chat
    """
    host = (ollama_host or "").rstrip("/")
    if not host:
        raise RuntimeError("OLLAMA_HOST is not configured")

    url = host + "/api/chat"
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "stream": False,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        raise RuntimeError(f"Ollama connection error: {e}") from e

    try:
        payload = json.loads(raw)
    except Exception as e:
        raise RuntimeError("Ollama returned invalid JSON") from e

    # Ollama responds with: {"message": {"role":"assistant","content":"..."}, ...}
    msg = payload.get("message") or {}
    content = msg.get("content")
    if not content:
        raise RuntimeError("Ollama returned an empty response")
    return str(content)


def _parse_as_of_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _effective_as_of(request) -> date | None:
    """
    Admin-only timeline support. Non-admin users always use live prices.
    """
    if not request.user.is_superuser:
        return None
    return _parse_as_of_date(request.GET.get("as_of"))


def _pct_growth(current: Decimal, baseline: Decimal) -> Decimal:
    if baseline <= 0:
        return Decimal("0")
    return ((current - baseline) / baseline * Decimal("100")).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


@login_required
def home(request):
    return render(request, "stocks/index.html")


@login_required
def leaderboard(request):
    sort_key = (request.GET.get("sort") or "networth").strip().lower()
    allowed_sorts = {"stardust", "networth", "assets", "growth_day", "growth_week"}
    if sort_key not in allowed_sorts:
        sort_key = "networth"

    today = date.today()
    day_ago = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    rows = []
    profiles = Profile.objects.select_related("user").all()

    for profile in profiles:
        positions = Position.objects.filter(user=profile.user)
        cash = profile.stardust_balance
        assets_now = Decimal("0")
        assets_day = Decimal("0")
        assets_week = Decimal("0")

        for p in positions:
            qty = Decimal(p.quantity)
            if qty <= 0:
                continue
            try:
                px_now = _dust_from_float(fetch_latest_price(p.symbol))
            except Exception:
                px_now = Decimal("0")
            try:
                px_day = _dust_from_float(fetch_latest_price(p.symbol, as_of=day_ago))
            except Exception:
                px_day = px_now
            try:
                px_week = _dust_from_float(fetch_latest_price(p.symbol, as_of=week_ago))
            except Exception:
                px_week = px_day

            assets_now += (qty * px_now).quantize(DUST_Q, rounding=ROUND_HALF_UP)
            assets_day += (qty * px_day).quantize(DUST_Q, rounding=ROUND_HALF_UP)
            assets_week += (qty * px_week).quantize(DUST_Q, rounding=ROUND_HALF_UP)

        networth_now = (cash + assets_now).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        networth_day = (cash + assets_day).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        networth_week = (cash + assets_week).quantize(DUST_Q, rounding=ROUND_HALF_UP)

        rows.append(
            {
                "username": profile.user.username,
                "stardust": cash,
                "assets": assets_now,
                "networth": networth_now,
                "growth_day": _pct_growth(networth_now, networth_day),
                "growth_week": _pct_growth(networth_now, networth_week),
            }
        )

    rows.sort(key=lambda r: r[sort_key], reverse=True)
    for idx, row in enumerate(rows, start=1):
        row["rank"] = idx

    return render(
        request,
        "stocks/leaderboard.html",
        {
            "rows": rows,
            "sort_key": sort_key,
        },
    )


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
def stock_chart(request):
    """
    Return price series for Chart.js (Yahoo Finance). Default symbol: SPY (S&P 500 ETF).
    """
    symbol = (request.GET.get("symbol") or "SPY").upper().strip()
    timeframe = (request.GET.get("timeframe") or "w").lower().strip()
    as_of = _effective_as_of(request)
    points_raw = request.GET.get("points") or "52"
    try:
        points = int(points_raw)
    except ValueError:
        return JsonResponse({"error": "Invalid points value"}, status=400)

    try:
        return JsonResponse(
            fetch_candles(symbol=symbol, timeframe=timeframe, points=points, as_of=as_of)
        )
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
    as_of = _effective_as_of(request)
    positions = Position.objects.filter(user=request.user).order_by("symbol")
    items = []
    total_value = Decimal("0")

    for p in positions:
        try:
            latest = fetch_latest_price(p.symbol, as_of=as_of)
            current_price = _dust_from_float(latest)
        except Exception:
            current_price = Decimal("0")

        current_value = (current_price * Decimal(p.quantity)).quantize(
            DUST_Q, rounding=ROUND_HALF_UP
        )
        total_value += current_value
        reset_val = p.last_reset_value_stardust
        if reset_val == 0 and p.quantity > 0:
            reset_val = (Decimal(p.quantity) * p.average_cost_stardust).quantize(
                DUST_Q, rounding=ROUND_HALF_UP
            )
        pnl = (current_value - reset_val).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        items.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "average_cost_stardust": _dust_str(p.average_cost_stardust),
                "current_price_stardust": _dust_str(current_price),
                "current_value_stardust": _dust_str(current_value),
                "pnl_stardust": _dust_str(pnl),
            }
        )

    return JsonResponse(
        {"positions": items, "portfolio_value_stardust": _dust_str(total_value)}
    )


@login_required
@require_POST
@transaction.atomic
def buy_stock(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    as_of = _parse_as_of_date(payload.get("as_of")) if request.user.is_superuser else None

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
        market_price = fetch_latest_price(symbol, as_of=as_of)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    price_per_share = _dust_from_float(market_price)
    qty_dec = Decimal(quantity)
    total_cost = (price_per_share * qty_dec).quantize(DUST_Q, rounding=ROUND_HALF_UP)

    profile = Profile.objects.select_for_update().get(user=request.user)
    if profile.stardust_balance < total_cost:
        return JsonResponse(
            {
                "error": (
                    f"Not enough stardust. Need {_dust_display_str(total_cost)} stardust, "
                    f"but you only have {_dust_display_str(profile.stardust_balance)}."
                )
            },
            status=400,
        )

    position, created = Position.objects.select_for_update().get_or_create(
        user=request.user,
        symbol=symbol,
        defaults={
            "quantity": 0,
            "average_cost_stardust": price_per_share,
            "last_reset_value_stardust": Decimal("0"),
        },
    )

    existing_qty = position.quantity
    new_qty = existing_qty + quantity
    if existing_qty == 0:
        new_avg = price_per_share
    else:
        prev_total = position.average_cost_stardust * Decimal(existing_qty)
        new_total = prev_total + (price_per_share * qty_dec)
        new_avg = (new_total / Decimal(new_qty)).quantize(DUST_Q, rounding=ROUND_HALF_UP)

    reset_snapshot = (Decimal(new_qty) * price_per_share).quantize(
        DUST_Q, rounding=ROUND_HALF_UP
    )
    position.quantity = new_qty
    position.average_cost_stardust = new_avg
    position.last_reset_value_stardust = reset_snapshot
    position.save(
        update_fields=[
            "quantity",
            "average_cost_stardust",
            "last_reset_value_stardust",
        ]
    )

    profile.stardust_balance = (profile.stardust_balance - total_cost).quantize(
        DUST_Q, rounding=ROUND_HALF_UP
    )
    profile.save(update_fields=["stardust_balance"])

    return JsonResponse(
        {
            "ok": True,
            "symbol": symbol,
            "quantity_bought": quantity,
            "price_per_share": _dust_str(price_per_share),
            "total_cost": _dust_str(total_cost),
            "owned_quantity": new_qty,
            "average_cost_stardust": _dust_str(new_avg),
            "remaining_stardust": _dust_str(profile.stardust_balance),
        }
    )


@login_required
@require_POST
@transaction.atomic
def sell_stock(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    as_of = _parse_as_of_date(payload.get("as_of")) if request.user.is_superuser else None

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
        market_price = fetch_latest_price(symbol, as_of=as_of)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=400)

    price_per_share = _dust_from_float(market_price)
    qty_dec = Decimal(quantity)

    try:
        position = Position.objects.select_for_update().get(user=request.user, symbol=symbol)
    except Position.DoesNotExist:
        return JsonResponse({"error": f"You do not own {symbol}."}, status=400)

    if quantity > position.quantity:
        return JsonResponse(
            {
                "error": (
                    f"You only own {position.quantity} shares of {symbol}. "
                    f"Cannot sell {quantity}."
                )
            },
            status=400,
        )

    proceeds = (price_per_share * qty_dec).quantize(DUST_Q, rounding=ROUND_HALF_UP)
    remaining_qty = position.quantity - quantity
    if remaining_qty == 0:
        position.delete()
    else:
        position.quantity = remaining_qty
        position.last_reset_value_stardust = (
            Decimal(remaining_qty) * price_per_share
        ).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        position.save(update_fields=["quantity", "last_reset_value_stardust"])

    profile = Profile.objects.select_for_update().get(user=request.user)
    profile.stardust_balance = (profile.stardust_balance + proceeds).quantize(
        DUST_Q, rounding=ROUND_HALF_UP
    )
    profile.save(update_fields=["stardust_balance"])

    return JsonResponse(
        {
            "ok": True,
            "symbol": symbol,
            "quantity_sold": quantity,
            "price_per_share": _dust_str(price_per_share),
            "total_proceeds": _dust_str(proceeds),
            "owned_quantity": remaining_qty,
            "remaining_stardust": _dust_str(profile.stardust_balance),
        }
    )


@login_required
@require_POST
def advisor_chat(request):
    """
    Chat endpoint for TradeQuest advisor panel.
    Uses local Ollama to generate buy/sell suggestions based on the user's portfolio.
    """
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    user_message = (payload.get("message") or "").strip()
    history = payload.get("history") or []
    if not user_message:
        return JsonResponse({"error": "Message is required"}, status=400)

    # Keep prompt smaller; this is not meant for long conversations.
    cleaned_history: list[dict] = []
    for h in history[-8:]:
        role = h.get("role")
        content = str(h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned_history.append({"role": role, "content": content[:2000]})

    profile = Profile.objects.get(user=request.user)
    positions = Position.objects.filter(user=request.user).order_by("symbol")[:20]

    holdings = []
    for p in positions:
        try:
            latest = fetch_latest_price(p.symbol)
            current_price = _dust_from_float(latest)
            current_value = (current_price * Decimal(p.quantity)).quantize(
                DUST_Q, rounding=ROUND_HALF_UP
            )
            reset_val = p.last_reset_value_stardust
            if reset_val == 0 and p.quantity > 0:
                reset_val = (Decimal(p.quantity) * p.average_cost_stardust).quantize(
                    DUST_Q, rounding=ROUND_HALF_UP
                )
            pnl = (current_value - reset_val).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        except Exception:
            current_price = Decimal("0")
            current_value = Decimal("0")
            pnl = Decimal("0")

        holdings.append(
            {
                "symbol": p.symbol,
                "quantity": int(p.quantity),
                "avg_cost": _dust_display_str(p.average_cost_stardust),
                "current_price": _dust_display_str(current_price),
                "current_value": _dust_display_str(current_value),
                "net_pnl": _dust_display_str(pnl),
            }
        )

    stardust_balance = _dust_display_str(profile.stardust_balance)

    # The model should produce concrete, market-realistic suggestions for this simulator.
    system_prompt = (
        "You are the TradeQuest trading advisor. "
        "You help a user decide what to buy or sell in a paper trading app called TradeQuest. "
        "The main currency is stardust. Use the provided stardust balance and current holdings "
        "to suggest actions that the user can actually do.\n\n"
        "Rules:\n"
        "- Always respect constraints: buy total cost must be <= stardust balance; "
        "sell quantity must be <= the user's current quantity for that symbol.\n"
        "- Use latest prices from the prompt to estimate cost/proceeds.\n"
        "- If exact calculations are required, tell the user the numbers are estimates based on latest price.\n"
        "- Suggest only real, widely traded US stocks/ETFs with valid tickers (examples: SPY, QQQ, AAPL, MSFT, NVDA, AMZN, TSLA, META).\n"
        "- Never invent fake companies, fake tickers, or themed roleplay assets.\n"
        "- Keep suggestions practical and concrete: ticker, buy/sell action, share count, estimated stardust impact, and 1 short reason.\n"
        "- When giving trades, suggest at most 3 candidates.\n"
        "- Do NOT execute trades; only recommend.\n"
        "- Do not include legal disclaimers or 'not financial advice' warnings.\n"
    )

    context_text = (
        f"User stardust balance: {stardust_balance}\n"
        f"Current holdings (latest price): {json.dumps(holdings)}\n"
        "If the user asks a direct question (e.g., 'Should I buy SPY?'), answer directly and propose "
        "specific share quantities and estimated stardust cost/proceeds."
    )

    ollama_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    # Include conversation history.
    ollama_messages.extend(cleaned_history)
    # Add the portfolio context just before the actual user message.
    ollama_messages.append(
        {
            "role": "user",
            "content": f"{context_text}\n\nUser message: {user_message}",
        }
    )

    model = os.environ.get("OLLAMA_MODEL", "llama3")
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

    try:
        reply = _ollama_chat(ollama_host=ollama_host, model=model, messages=ollama_messages)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"reply": reply})
