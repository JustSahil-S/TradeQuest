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
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    Position,
    Profile,
    Trade,
    PowerUp,
    UserPowerUp,
    StardustShield,
    MultiplyProfitBoost,
    ReconSnapshot,
)
from .yahoo_data import fetch_candles, fetch_latest_price, fetch_news, search_symbols, fetch_sector

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


def _leaderboard_networth_rows(as_of: date | None = None) -> list[dict]:
    """
    All users ranked by net worth (cash + position value at latest or simulated close).
    """
    rows: list[dict] = []
    profiles = Profile.objects.select_related("user").all()
    for profile in profiles:
        user = profile.user
        cash = profile.stardust_balance
        positions = Position.objects.filter(user=user)
        assets_now = Decimal("0")
        for p in positions:
            qty = Decimal(p.quantity)
            if qty <= 0:
                continue
            try:
                px_now = _dust_from_float(fetch_latest_price(p.symbol, as_of=as_of))
            except Exception:
                px_now = Decimal("0")
            assets_now += (qty * px_now).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        networth_now = (cash + assets_now).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        rows.append(
            {
                "user_id": user.id,
                "username": user.username,
                "networth": networth_now,
            }
        )
    rows.sort(key=lambda r: r["networth"], reverse=True)
    return rows


def _holdings_for_user_id(user_id: int, as_of: date | None = None) -> list[dict]:
    out: list[dict] = []
    for p in Position.objects.filter(user_id=user_id, quantity__gt=0).order_by("symbol"):
        try:
            px = _dust_from_float(fetch_latest_price(p.symbol, as_of=as_of))
        except Exception:
            px = Decimal("0")
        val = (Decimal(p.quantity) * px).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        out.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "current_price_stardust": _dust_str(px),
                "value_stardust": _dust_str(val),
            }
        )
    return out


def _build_recon_intel_payload(as_of: date | None = None) -> dict:
    ranked = _leaderboard_networth_rows(as_of=as_of)
    top = ranked[:3]
    players: list[dict] = []
    for i, row in enumerate(top, start=1):
        players.append(
            {
                "rank": i,
                "username": row["username"],
                "networth_stardust": _dust_str(row["networth"]),
                "holdings": _holdings_for_user_id(row["user_id"], as_of=as_of),
            }
        )
    return {"players": players}


def _recon_qty_for_user(user) -> int:
    try:
        recon_pu = PowerUp.objects.get(code="RECON")
        item = UserPowerUp.objects.get(user=user, powerup=recon_pu)
        return int(item.quantity or 0)
    except (PowerUp.DoesNotExist, UserPowerUp.DoesNotExist):
        return 0


def _run_shields_for_user(user, as_of: date | None = None) -> None:
    """
    Trigger and execute stardust shields when current price <= trigger threshold.
    Disabled while using admin time travel (as_of) to avoid mutating portfolio in simulations.
    """
    if as_of is not None:
        # Mutation-based triggers are disabled during admin time-travel simulation.
        # Portfolio endpoints can simulate shield effects without writing to DB.
        return

    active_shields = StardustShield.objects.filter(user=user, is_active=True).order_by("id")
    for shield in active_shields:
        symbol = shield.symbol
        try:
            current_price = _dust_from_float(fetch_latest_price(symbol))
        except Exception:
            continue

        if current_price > shield.trigger_price_stardust:
            continue

        with transaction.atomic():
            shield_locked = StardustShield.objects.select_for_update().get(pk=shield.pk)
            if not shield_locked.is_active:
                continue
            try:
                position = Position.objects.select_for_update().get(user=user, symbol=symbol)
            except Position.DoesNotExist:
                shield_locked.is_active = False
                shield_locked.triggered_at = timezone.now()
                shield_locked.save(update_fields=["is_active", "triggered_at"])
                continue

            qty = position.quantity
            if qty <= 0:
                shield_locked.is_active = False
                shield_locked.triggered_at = timezone.now()
                shield_locked.save(update_fields=["is_active", "triggered_at"])
                continue

            proceeds = (current_price * Decimal(qty)).quantize(DUST_Q, rounding=ROUND_HALF_UP)
            position.delete()

            profile = Profile.objects.select_for_update().get(user=user)
            profile.stardust_balance = (profile.stardust_balance + proceeds).quantize(
                DUST_Q, rounding=ROUND_HALF_UP
            )
            profile.save(update_fields=["stardust_balance"])

            Trade.objects.create(
                user=user,
                executed_as_of=None,
                symbol=symbol,
                side=Trade.Side.SELL,
                quantity=qty,
                price_per_share_stardust=current_price,
                total_stardust=proceeds,
            )

            shield_locked.is_active = False
            shield_locked.triggered_at = timezone.now()
            shield_locked.save(update_fields=["is_active", "triggered_at"])


def _get_simulated_shield_triggered_symbols(user, as_of: date) -> set[str]:
    """
    Compute which shield-protected symbols would be auto-sold at this simulated date.
    This is read-only (no DB mutations).
    """
    triggered: set[str] = set()
    active_shields = StardustShield.objects.filter(user=user, is_active=True)
    for shield in active_shields:
        try:
            current_price = _dust_from_float(fetch_latest_price(shield.symbol, as_of=as_of))
        except Exception:
            continue
        if current_price <= shield.trigger_price_stardust:
            triggered.add(shield.symbol)
    return triggered


@login_required
def home(request):
    return render(request, "stocks/index.html")


@login_required
def inventory(request):
    positions_qs = Position.objects.filter(user=request.user, quantity__gt=0).order_by("symbol")

    # Stardust Shield quantity in inventory.
    try:
        shield_powerup = PowerUp.objects.get(code="STARDUST_SHIELD")
        shield_item = UserPowerUp.objects.get(user=request.user, powerup=shield_powerup)
        shield_qty = int(shield_item.quantity or 0)
    except (PowerUp.DoesNotExist, UserPowerUp.DoesNotExist):
        shield_qty = 0

    # Multiply profit boost quantity in inventory.
    try:
        multiply_powerup = PowerUp.objects.get(code="MULTIPLY_PROFIT_2X")
        multiply_item = UserPowerUp.objects.get(user=request.user, powerup=multiply_powerup)
        multiply_qty = int(multiply_item.quantity or 0)
    except (PowerUp.DoesNotExist, UserPowerUp.DoesNotExist):
        multiply_qty = 0

    recon_qty = _recon_qty_for_user(request.user)
    recon_snapshot = None
    try:
        recon_snapshot = ReconSnapshot.objects.get(user=request.user)
    except ReconSnapshot.DoesNotExist:
        pass

    positions = []
    for p in positions_qs:
        try:
            px = fetch_latest_price(p.symbol)
            current_price = _dust_from_float(px)
        except Exception:
            current_price = Decimal("0")
        positions.append(
            {
                "symbol": p.symbol,
                "quantity": p.quantity,
                "current_price_stardust": current_price,
            }
        )

    active_shields = StardustShield.objects.filter(user=request.user, is_active=True).order_by("symbol")
    active_multipliers = MultiplyProfitBoost.objects.filter(
        user=request.user, is_active=True
    ).order_by("symbol")
    return render(
        request,
        "stocks/inventory.html",
        {
            "shield_qty": shield_qty,
            "multiply_qty": multiply_qty,
            "recon_qty": recon_qty,
            "recon_snapshot": recon_snapshot,
            "positions": positions,
            "active_shields": active_shields,
            "active_multipliers": active_multipliers,
        },
    )


@login_required
@require_POST
@transaction.atomic
def apply_multiply_profit_boost(request):
    """
    Apply a 2x profit multiplier power-up to a held symbol.
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    symbol = (payload.get("symbol") or "").upper().strip()
    if not symbol:
        return JsonResponse({"error": "Symbol is required"}, status=400)

    try:
        Position.objects.get(user=request.user, symbol=symbol, quantity__gt=0)
    except Position.DoesNotExist:
        return JsonResponse({"error": f"You do not currently own {symbol}."}, status=400)

    if MultiplyProfitBoost.objects.filter(user=request.user, symbol=symbol, is_active=True).exists():
        return JsonResponse({"error": f"A multiplier is already active for {symbol}."}, status=400)

    try:
        multiply_powerup = PowerUp.objects.get(code="MULTIPLY_PROFIT_2X")
    except PowerUp.DoesNotExist:
        return JsonResponse({"error": "Multiply power-up is not configured yet."}, status=500)

    try:
        inventory_item = UserPowerUp.objects.select_for_update().get(
            user=request.user, powerup=multiply_powerup
        )
    except UserPowerUp.DoesNotExist:
        return JsonResponse({"error": "You do not have any Multiply power-ups in inventory."}, status=400)

    if inventory_item.quantity <= 0:
        return JsonResponse({"error": "You do not have any Multiply power-ups left."}, status=400)

    inventory_item.quantity -= 1
    inventory_item.save(update_fields=["quantity"])

    MultiplyProfitBoost.objects.create(
        user=request.user,
        symbol=symbol,
        multiplier=Decimal("2.00"),
        is_active=True,
    )

    return JsonResponse({"ok": True, "symbol": symbol, "remaining_boosts": inventory_item.quantity})


@login_required
@require_POST
@transaction.atomic
def apply_stardust_shield(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    symbol = (payload.get("symbol") or "").upper().strip()
    trigger_raw = payload.get("trigger_price")
    if not symbol:
        return JsonResponse({"error": "Symbol is required"}, status=400)

    try:
        trigger_price = _dust_from_float(float(trigger_raw))
    except Exception:
        return JsonResponse({"error": "Trigger price must be a valid number"}, status=400)

    if trigger_price <= 0:
        return JsonResponse({"error": "Trigger price must be greater than 0"}, status=400)

    try:
        Position.objects.get(user=request.user, symbol=symbol, quantity__gt=0)
    except Position.DoesNotExist:
        return JsonResponse({"error": f"You do not currently own {symbol}."}, status=400)

    if StardustShield.objects.filter(user=request.user, symbol=symbol, is_active=True).exists():
        return JsonResponse({"error": f"A shield is already active for {symbol}."}, status=400)

    try:
        shield_powerup = PowerUp.objects.get(code="STARDUST_SHIELD")
    except PowerUp.DoesNotExist:
        return JsonResponse({"error": "Stardust Shield power-up is not configured yet."}, status=500)

    try:
        inventory_item = UserPowerUp.objects.select_for_update().get(
            user=request.user, powerup=shield_powerup
        )
    except UserPowerUp.DoesNotExist:
        return JsonResponse({"error": "You do not have any Stardust Shields in inventory."}, status=400)

    if inventory_item.quantity <= 0:
        return JsonResponse({"error": "You do not have any Stardust Shields left."}, status=400)

    inventory_item.quantity -= 1
    inventory_item.save(update_fields=["quantity"])

    shield = StardustShield.objects.create(
        user=request.user,
        symbol=symbol,
        trigger_price_stardust=trigger_price,
        is_active=True,
    )

    return JsonResponse(
        {
            "ok": True,
            "symbol": symbol,
            "trigger_price_stardust": _dust_str(shield.trigger_price_stardust),
            "remaining_shields": inventory_item.quantity,
        }
    )


@login_required
@require_POST
@transaction.atomic
def use_recon(request):
    """
    Consume one Recon power-up and refresh the stored snapshot of top-3 players' holdings.
    """
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = {}

    as_of = None
    if request.user.is_superuser:
        as_of = _parse_as_of_date(payload.get("as_of"))

    try:
        recon_pu = PowerUp.objects.get(code="RECON")
    except PowerUp.DoesNotExist:
        return JsonResponse({"error": "Recon power-up is not configured yet."}, status=500)

    try:
        inventory_item = UserPowerUp.objects.select_for_update().get(
            user=request.user, powerup=recon_pu
        )
    except UserPowerUp.DoesNotExist:
        return JsonResponse({"error": "You do not have any Recon power-ups in inventory."}, status=400)

    if inventory_item.quantity <= 0:
        return JsonResponse({"error": "You do not have any Recon power-ups left."}, status=400)

    inventory_item.quantity -= 1
    inventory_item.save(update_fields=["quantity"])

    intel = _build_recon_intel_payload(as_of=as_of)
    now = timezone.now()
    ReconSnapshot.objects.update_or_create(
        user=request.user,
        defaults={
            "players": intel["players"],
            "captured_at": now,
        },
    )

    return JsonResponse(
        {
            "ok": True,
            "remaining_recon": inventory_item.quantity,
            "recon_intel": {
                "captured_at": now.isoformat(),
                "players": intel["players"],
            },
        }
    )


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


@login_required
def trade_history(request):
    trades = (
        Trade.objects.filter(user=request.user)
        .order_by("-created_at")
        .only(
            "created_at",
            "executed_as_of",
            "symbol",
            "side",
            "quantity",
            "price_per_share_stardust",
            "total_stardust",
        )
    )
    return render(request, "stocks/trade_history.html", {"trades": trades})


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
def stock_news(request):
    """
    Yahoo Finance headlines for the selected symbol (yfinance Ticker.news).
    """
    symbol = (request.GET.get("symbol") or "SPY").upper().strip()
    limit_raw = request.GET.get("limit") or "8"
    try:
        limit = int(limit_raw)
    except ValueError:
        return JsonResponse({"error": "Invalid limit value"}, status=400)

    try:
        articles = fetch_news(symbol=symbol, limit=limit)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)

    return JsonResponse({"symbol": symbol, "articles": articles})


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
    triggered_symbols: set[str] = set()
    if as_of is None:
        _run_shields_for_user(request.user, as_of=None)
    else:
        triggered_symbols = _get_simulated_shield_triggered_symbols(request.user, as_of=as_of)
    positions = Position.objects.filter(user=request.user).order_by("symbol")
    items = []
    total_value = Decimal("0")

    shield_symbols = set(
        StardustShield.objects.filter(user=request.user, is_active=True).values_list(
            "symbol", flat=True
        )
    )
    multiply_symbols = set(
        MultiplyProfitBoost.objects.filter(user=request.user, is_active=True).values_list(
            "symbol", flat=True
        )
    )

    for p in positions:
        if p.symbol in triggered_symbols:
            continue
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
                "shield_active": p.symbol in shield_symbols,
                "multiply_active": p.symbol in multiply_symbols,
            }
        )

    recon_intel = None
    try:
        snap = ReconSnapshot.objects.get(user=request.user)
        recon_intel = {
            "captured_at": snap.captured_at.isoformat(),
            "players": snap.players,
        }
    except ReconSnapshot.DoesNotExist:
        pass

    return JsonResponse(
        {
            "positions": items,
            "portfolio_value_stardust": _dust_str(total_value),
            "recon_qty": _recon_qty_for_user(request.user),
            "recon_intel": recon_intel,
        }
    )


@login_required
def sector_breakdown(request):
    """
    Return portfolio sector allocation (by current position value).
    """
    as_of = _effective_as_of(request)
    triggered_symbols: set[str] = set()
    if as_of is None:
        _run_shields_for_user(request.user, as_of=None)
    else:
        triggered_symbols = _get_simulated_shield_triggered_symbols(request.user, as_of=as_of)
    positions = Position.objects.filter(user=request.user).order_by("symbol")

    totals: dict[str, Decimal] = {}
    grand_total = Decimal("0")

    for p in positions:
        if p.symbol in triggered_symbols:
            continue
        if p.quantity <= 0:
            continue
        try:
            px = _dust_from_float(fetch_latest_price(p.symbol, as_of=as_of))
        except Exception:
            px = Decimal("0")
        value = (Decimal(p.quantity) * px).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        if value <= 0:
            continue

        sector = fetch_sector(p.symbol) or "Unknown"
        totals[sector] = (totals.get(sector, Decimal("0")) + value).quantize(
            DUST_Q, rounding=ROUND_HALF_UP
        )
        grand_total += value

    items = []
    for sector, value in sorted(totals.items(), key=lambda kv: kv[1], reverse=True):
        pct = Decimal("0")
        if grand_total > 0:
            pct = (value / grand_total * Decimal("100")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        items.append({"sector": sector, "value_stardust": _dust_str(value), "pct": str(pct)})

    return JsonResponse({"total_stardust": _dust_str(grand_total), "sectors": items})


@login_required
@require_POST
@transaction.atomic
def buy_stock(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)
    as_of = _parse_as_of_date(payload.get("as_of")) if request.user.is_superuser else None
    _run_shields_for_user(request.user, as_of=as_of)

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

    Trade.objects.create(
        user=request.user,
        executed_as_of=as_of,
        symbol=symbol,
        side=Trade.Side.BUY,
        quantity=quantity,
        price_per_share_stardust=price_per_share,
        total_stardust=total_cost,
    )

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
    _run_shields_for_user(request.user, as_of=as_of)

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
        StardustShield.objects.filter(user=request.user, symbol=symbol, is_active=True).update(
            is_active=False,
            triggered_at=timezone.now(),
        )
    else:
        position.quantity = remaining_qty
        position.last_reset_value_stardust = (
            Decimal(remaining_qty) * price_per_share
        ).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        position.save(update_fields=["quantity", "last_reset_value_stardust"])

    profile = Profile.objects.select_for_update().get(user=request.user)
    credited = proceeds

    # If a multiplier is active, boost only positive profit.
    boost = MultiplyProfitBoost.objects.filter(
        user=request.user, symbol=symbol, is_active=True
    ).first()
    bonus = Decimal("0")
    multiplier = None
    if boost:
        cost_basis = (position.average_cost_stardust * qty_dec).quantize(
            DUST_Q, rounding=ROUND_HALF_UP
        )
        profit = (proceeds - cost_basis).quantize(DUST_Q, rounding=ROUND_HALF_UP)
        multiplier = boost.multiplier
        if profit > 0 and multiplier is not None:
            bonus = (profit * (multiplier - Decimal("1"))).quantize(
                DUST_Q, rounding=ROUND_HALF_UP
            )
            credited = (credited + bonus).quantize(DUST_Q, rounding=ROUND_HALF_UP)

        boost.is_active = False
        boost.consumed_at = timezone.now()
        boost.save(update_fields=["is_active", "consumed_at"])

    profile.stardust_balance = (profile.stardust_balance + credited).quantize(
        DUST_Q, rounding=ROUND_HALF_UP
    )
    profile.save(update_fields=["stardust_balance"])

    Trade.objects.create(
        user=request.user,
        executed_as_of=as_of,
        symbol=symbol,
        side=Trade.Side.SELL,
        quantity=quantity,
        price_per_share_stardust=price_per_share,
        total_stardust=credited,
    )

    return JsonResponse(
        {
            "ok": True,
            "symbol": symbol,
            "quantity_sold": quantity,
            "price_per_share": _dust_str(price_per_share),
            "total_proceeds": _dust_str(credited),
            "profit_multiplier_applied": bool(boost),
            "profit_bonus_stardust": _dust_str(bonus),
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
