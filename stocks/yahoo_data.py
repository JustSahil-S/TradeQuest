import yfinance as yf


def fetch_weekly_candles(symbol: str = "AAPL", points: int = 52) -> dict:
    """
    Fetch weekly close prices from Yahoo Finance via yfinance.
    No API key required.
    """
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="5y", interval="1wk", auto_adjust=False)

    if hist is None or hist.empty:
        raise RuntimeError(f"No weekly data found for symbol '{symbol}'")

    closes_series = hist["Close"].dropna()
    if closes_series.empty:
        raise RuntimeError(f"No weekly close data found for symbol '{symbol}'")

    closes_series = closes_series.tail(points)
    labels = [idx.strftime("%Y-%m-%d") for idx in closes_series.index]
    closes = [round(float(v), 2) for v in closes_series.tolist()]

    return {
        "labels": labels,
        "closes": closes,
        "symbol": symbol.upper(),
        "source": "yahoo_finance_weekly",
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

