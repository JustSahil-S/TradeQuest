import datetime
import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

from django.conf import settings


def fetch_candles(symbol: str, resolution: str = "D", days: int = 30) -> dict:
    """
    Fetch candle data from Finnhub.

    Returns a dict:
      { "labels": [...], "closes": [...] }
    """
    api_key = getattr(settings, "FINNHUB_API_KEY", "") or ""
    if not api_key:
        raise RuntimeError("FINNHUB_API_KEY is not set")

    now = datetime.datetime.utcnow()
    start = now - datetime.timedelta(days=days)

    params = {
        "symbol": symbol,
        "resolution": resolution,
        "from": int(start.timestamp()),
        "to": int(now.timestamp()),
        "token": api_key,
    }

    url = f"https://finnhub.io/api/v1/stock/candle?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        # Read the body so the caller can see Finnhub's actual error message.
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Finnhub HTTP {e.code}: {body or e.reason}")
    except URLError as e:
        raise RuntimeError(f"Finnhub network error: {e.reason}")

    payload = json.loads(raw)
    if payload.get("s") != "ok":
        raise RuntimeError(f"Finnhub error: {payload}")

    timestamps = payload.get("t", [])
    closes = payload.get("c", [])
    labels = [datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") for ts in timestamps]

    # Finnhub arrays should align; still, be defensive.
    n = min(len(labels), len(closes))
    return {"labels": labels[:n], "closes": closes[:n]}


def mock_candles(symbol: str, days: int = 30) -> dict:
    """
    Deterministic mock series so the UI can be tested even if Finnhub access
    is unavailable (e.g., missing plan permissions).
    """
    now = datetime.datetime.utcnow()
    start = now - datetime.timedelta(days=days)

    labels = []
    closes = []

    # Deterministic "random-looking" series using a sine wave + gentle trend.
    base = 180.0
    for i in range(days):
        dt = start + datetime.timedelta(days=i)
        labels.append(dt.strftime("%Y-%m-%d"))
        wave = 4.0 * __import__("math").sin(i / 3.0)
        trend = i * 0.15
        closes.append(round(base + wave + trend, 2))

    return {"labels": labels, "closes": closes, "mock": True, "symbol": symbol}

