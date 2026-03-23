import json
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

from django.conf import settings


def fetch_weekly_candles(symbol: str = "AAPL", points: int = 52) -> dict:
    """
    Fetch weekly close prices from Alpha Vantage.
    """
    api_key = getattr(settings, "ALPHAVANTAGE_API_KEY", "") or ""
    if not api_key:
        raise RuntimeError("ALPHAVANTAGE_API_KEY is not set")

    params = {
        "function": "TIME_SERIES_WEEKLY_ADJUSTED",
        "symbol": symbol,
        "apikey": api_key,
    }
    url = f"https://www.alphavantage.co/query?{urllib.parse.urlencode(params)}"

    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(f"Alpha Vantage HTTP {e.code}: {body or e.reason}")
    except URLError as e:
        raise RuntimeError(f"Alpha Vantage network error: {e.reason}")

    payload = json.loads(raw)

    if payload.get("Note"):
        raise RuntimeError(payload["Note"])
    if payload.get("Error Message"):
        raise RuntimeError(payload["Error Message"])

    series = payload.get("Weekly Adjusted Time Series")
    if not series:
        raise RuntimeError(f"Alpha Vantage response missing weekly series: {payload}")

    # API returns newest first; reverse to oldest->newest for charting.
    dates = sorted(series.keys())
    if points > 0:
        dates = dates[-points:]

    labels = dates
    closes = [float(series[d]["4. close"]) for d in dates]

    return {
        "labels": labels,
        "closes": closes,
        "symbol": symbol,
        "source": "alpha_vantage_weekly",
    }

