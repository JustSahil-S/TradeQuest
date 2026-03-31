import yfinance as yf
import time
from datetime import date, datetime, timezone

_SECTOR_CACHE: dict[str, tuple[float, str]] = {}
_SECTOR_TTL_SECONDS = 24 * 60 * 60

_NEWS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_NEWS_TTL_SECONDS = 120


def _normalize_yahoo_news_item(item: object) -> dict | None:
    """
    yfinance may return flat dicts or nested {'content': {...}} story objects.
    """
    if not isinstance(item, dict):
        return None

    title = ""
    link: str | None = None
    publisher = ""
    published_at: str | None = None

    inner = item.get("content")
    if isinstance(inner, dict):
        title = (inner.get("title") or "").strip()
        cu = inner.get("canonicalUrl")
        if isinstance(cu, dict):
            link = (cu.get("url") or "").strip() or None
        if not link:
            preview = inner.get("previewUrl")
            if isinstance(preview, str) and preview.strip():
                link = preview.strip()
        pub_raw = inner.get("pubDate") or inner.get("displayTime")
        if isinstance(pub_raw, str) and pub_raw.strip():
            published_at = pub_raw.strip()
        prov = inner.get("provider")
        if isinstance(prov, dict):
            publisher = (prov.get("displayName") or "").strip()
    else:
        title = (item.get("title") or "").strip()
        link_raw = (item.get("link") or "").strip()
        link = link_raw or None
        publisher = (item.get("publisher") or item.get("source") or "").strip()
        ts_ms = item.get("providerPublishTime")
        if ts_ms is not None:
            try:
                dt = datetime.fromtimestamp(float(ts_ms) / 1000.0, tz=timezone.utc)
                published_at = dt.isoformat()
            except (TypeError, ValueError, OSError):
                published_at = None

    if not title:
        return None

    return {
        "title": title,
        "link": link,
        "publisher": publisher or None,
        "published_at": published_at,
    }


def fetch_sector(symbol: str) -> str:
    """
    Best-effort sector lookup via yfinance. Cached to avoid repeated calls.
    """
    sym = (symbol or "").upper().strip()
    if not sym:
        return "Unknown"

    now = time.time()
    cached = _SECTOR_CACHE.get(sym)
    if cached:
        ts, sector = cached
        if now - ts < _SECTOR_TTL_SECONDS and sector:
            return sector

    try:
        info = yf.Ticker(sym).info or {}
        sector = (info.get("sector") or "").strip() or "Unknown"
    except Exception:
        sector = "Unknown"

    _SECTOR_CACHE[sym] = (now, sector)
    return sector


def fetch_news(symbol: str, limit: int = 8) -> list[dict]:
    """
    Headlines for a ticker via yfinance (Yahoo Finance news feed).
    Cached briefly per symbol to limit upstream calls.
    """
    sym = (symbol or "").upper().strip() or "SPY"
    safe_limit = max(1, min(int(limit), 20))

    now = time.time()
    cached = _NEWS_CACHE.get(sym)
    if cached:
        ts, articles = cached
        if now - ts < _NEWS_TTL_SECONDS:
            return articles[:safe_limit]

    articles: list[dict] = []
    try:
        ticker = yf.Ticker(sym)
        raw = getattr(ticker, "news", None) or []
    except Exception:
        raw = []

    for item in raw:
        norm = _normalize_yahoo_news_item(item)
        if not norm:
            continue
        articles.append(norm)
        if len(articles) >= 20:
            break

    _NEWS_CACHE[sym] = (now, articles)
    return articles[:safe_limit]


def fetch_candles(
    symbol: str = "SPY",
    timeframe: str = "w",
    points: int = 52,
    as_of: date | None = None,
) -> dict:
    """
    Fetch close prices from Yahoo Finance via yfinance.
    No API key required.
    """
    tf = (timeframe or "w").lower()
    interval_map = {
        "d": "1d",
        "w": "1wk",
        "m": "1mo",
        "ytd": "1d",
    }
    label_map = {
        "d": "daily",
        "w": "weekly",
        "m": "monthly",
        "ytd": "year-to-date",
    }
    if tf not in interval_map:
        raise RuntimeError("Invalid timeframe. Use one of: d, w, m.")

    period = "max"
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period=period, interval=interval_map[tf], auto_adjust=False)

    if hist is None or hist.empty:
        raise RuntimeError(f"No price data found for symbol '{symbol}'")

    closes_series = hist["Close"].dropna()
    if closes_series.empty:
        raise RuntimeError(f"No close data found for symbol '{symbol}'")

    if as_of is not None:
        closes_series = closes_series[closes_series.index.date <= as_of]
        if closes_series.empty:
            raise RuntimeError(f"No historical data for '{symbol}' on or before {as_of.isoformat()}")

    # YTD is intrinsically bounded to this year's range.
    if tf == "ytd":
        anchor = as_of or date.today()
        year_start = date(anchor.year, 1, 1)
        closes_series = closes_series[closes_series.index.date >= year_start]
        if closes_series.empty:
            raise RuntimeError(f"No YTD data found for symbol '{symbol}'")
    else:
        safe_points = max(5, min(int(points), 500))
        closes_series = closes_series.tail(safe_points)
    labels = [idx.strftime("%Y-%m-%d") for idx in closes_series.index]
    closes = [round(float(v), 2) for v in closes_series.tolist()]

    return {
        "labels": labels,
        "closes": closes,
        "symbol": symbol.upper(),
        "timeframe": tf,
        "timeframe_label": label_map[tf],
        "source": "yahoo_finance",
    }


def search_symbols(query: str, limit: int = 8) -> dict:
    """
    Search symbols using yfinance's Yahoo search integration.
    No API key required.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return {"results": []}
    try:
        search = yf.Search(query=q, max_results=limit)
        quotes = (search.quotes or [])[:limit]
    except Exception as e:
        # Keep autocomplete resilient; chart loading still works by typed symbol.
        raise RuntimeError(f"Yahoo search unavailable: {e}")
    results = []
    for item in quotes:
        symbol = item.get("symbol")
        if not symbol:
            continue
        results.append(
            {
                "symbol": symbol,
                "name": item.get("shortname") or item.get("longname") or symbol,
                "region": item.get("exchange") or "",
                "currency": item.get("currency") or "",
            }
        )

    return {"results": results}


def fetch_latest_price(symbol: str, as_of: date | None = None) -> float:
    """
    Return latest close price used for buy execution.
    """
    if as_of is None:
        payload = fetch_candles(symbol=symbol, timeframe="d", points=5)
    else:
        # Pull up to as_of and use the nearest available close at/before that date.
        payload = fetch_candles(symbol=symbol, timeframe="d", points=500, as_of=as_of)
    closes = payload.get("closes", [])
    if not closes:
        raise RuntimeError(f"No latest price found for symbol '{symbol}'")
    return float(closes[-1])

