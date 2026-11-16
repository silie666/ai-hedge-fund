"""Eulerpool API client implementing the DataClient protocol.

US equities focused. Uses a thin ``requests`` session (no official SDK
dependency). Fail-loud: auth / rate-limit / network / 5xx raise
``EulerpoolClientError``. Capability gaps (historical minute bars,
point-in-time screener, undated fundamentals used as PIT) raise with a
clear message rather than returning a silent empty list.

Honesty limits (also in ``docs/eulerpool-data-provider.md``):

* Daily candles often omit volume → ``Price.volume`` defaults to 0.
* Financial metrics: current cross-section is returned for recent
  ``end_date`` only; older dates require dated historical rows or SEC
  filing timestamps — otherwise we raise instead of leaking today's
  ratios into a backtest.
* PEAD: built from calendar earnings surprises + nearest 8-K when
  available. Surprise ``date`` is used as ``filing_date`` when no 8-K
  match exists (``source_type=calendar``); default PEAD
  ``announcement_only=True`` then skips those rows.
* Minute bars: Eulerpool intraday quotes cover current/previous session
  only — other windows raise.
* Screener: no historical ``as_of``; only near-today screens are
  allowed, and rows carry a synthetic filing stamp.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import requests

from hedge_fund.data.client import CallMeter, DataClientError
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
    PriceSnapshot,
)
from hedge_fund.data.screener import OrderBy, SCREEN_LIMIT, ScreenerFields, ScreenFilter, ScreenRow

logger = logging.getLogger(__name__)

EULERPOOL_API_KEY_ENV = "EULERPOOL_API_KEY"

# How close ``end_date`` / ``as_of`` must be to "today" to accept current
# cross-section answers (paper trading). Wider than one calendar day so
# weekends and holiday gaps do not break Friday paper ticks on Monday.
_RECENT_DAYS = 5

# Eulerpool ``/equity/market-cap`` and ``metrics.valuation.marketCap`` are
# quoted in millions of currency units (sharesOutstanding is likewise in
# millions). ``metrics.historical[].marketCap`` is absolute dollars — do not
# scale that path.
_MARKET_CAP_MILLIONS = 1_000_000.0

# Match calendar surprise dates to SEC 8-K filings within this window.
_SURPRISE_8K_MATCH_DAYS = 5

# Map DataClient / FD screener field names → Eulerpool screener column ids.
_SCREEN_FIELD_TO_EP = {
    "market_cap": "marketCap",
    "price_to_earnings_ratio": "pe",
    "price_to_book_ratio": "pb",
    "price_to_sales_ratio": "ps",
    "sector": "gsector",
    "industry": "gindustry",
    "country": "country",
}

# Eulerpool equality set has no strict > / <; map gt/lt to inclusive forms.
_SCREEN_OP_TO_EP = {
    "gt": ">=",
    "gte": ">=",
    "lt": "<=",
    "lte": "<=",
    "eq": "=",
    "in": "IN",
}

_CANDLE_RANGES = (
    ("1m", 31),
    ("3m", 93),
    ("6m", 186),
    ("1y", 370),
    ("2y", 740),
    ("5y", 5 * 370),
    ("max", 10_000),
)


class EulerpoolClientError(DataClientError):
    """Infrastructure or capability failure talking to Eulerpool."""


class EulerpoolClient:
    """Eulerpool DataClient for US equities.

    Usage::

        with EulerpoolClient() as ep:
            prices = ep.get_prices("AAPL", "2024-01-01", "2024-12-31")
    """

    BASE_URL = "https://api.eulerpool.com/api/1"
    _RETRY_DELAYS = (5, 15, 30)

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = 30.0,
        meter: CallMeter | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get(EULERPOOL_API_KEY_ENV, "")
        self._timeout = timeout
        self.meter = meter if meter is not None else CallMeter()
        self._session = requests.Session()
        if self._api_key:
            self._session.headers["Authorization"] = f"Bearer {self._api_key}"
        self._session.headers["Accept"] = "application/json"

    def __enter__(self) -> EulerpoolClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    def get_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval: str = "day",
        interval_multiplier: int = 1,
    ) -> list[Price]:
        if interval == "minute":
            return self._get_minute_prices(ticker, start_date, end_date, interval_multiplier)
        if interval != "day":
            raise EulerpoolClientError(
                f"Eulerpool get_prices does not support interval={interval!r}; "
                f"use 'day' or 'minute'",
                path="/equity/candles",
            )
        if interval_multiplier != 1:
            raise EulerpoolClientError(
                f"Eulerpool daily candles only support interval_multiplier=1 "
                f"(got {interval_multiplier})",
                path="/equity/candles",
            )
        span_days = (_parse_day(end_date) - _parse_day(start_date)).days
        if span_days < 0:
            return []
        range_key = _candle_range_for_span(span_days)
        data = self._get_json(f"/equity/candles/{_enc(ticker)}", {"range": range_key})
        rows = _as_list(data)
        out: list[Price] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = _ts_to_day(row.get("timestamp") or row.get("date") or row.get("time"))
            if day is None or day < start_date[:10] or day > end_date[:10]:
                continue
            try:
                out.append(Price(
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume") or 0),
                    time=day,
                ))
            except (KeyError, TypeError, ValueError) as exc:
                raise EulerpoolClientError(
                    f"Malformed candle for {ticker}: {exc}",
                    path="/equity/candles",
                ) from exc
        out.sort(key=lambda p: p.time)
        return out

    def _get_minute_prices(
        self,
        ticker: str,
        start_date: str,
        end_date: str,
        interval_multiplier: int,
    ) -> list[Price]:
        """Best-effort minute bars from intraday ticks (current/prev session).

        Eulerpool's documented intraday endpoint does not expose arbitrary
        historical minute windows. Requests whose date window is not the
        current or previous NY session raise instead of returning [].
        """
        if interval_multiplier < 1:
            raise EulerpoolClientError(
                f"interval_multiplier must be >= 1 (got {interval_multiplier})",
                path="/market/quotes/intraday",
            )
        today = date.today()
        start_d = _parse_day(start_date)
        end_d = _parse_day(end_date)
        if start_d > end_d:
            return []
        # Allow only windows that touch today or yesterday (prev session).
        if end_d < today - timedelta(days=1) or start_d > today:
            raise EulerpoolClientError(
                "Eulerpool minute bars are only available for the current/"
                "previous trading session via /market/quotes/intraday; "
                f"requested {start_date}→{end_date}. Use daily bars or "
                "DATA_PROVIDER=financial_datasets for historical minute data.",
                path="/market/quotes/intraday",
            )
        data = self._get_json(f"/market/quotes/intraday/{_enc(ticker)}")
        ticks = _as_list(data)
        if not ticks and isinstance(data, dict):
            ticks = _as_list(data.get("data") or data.get("quotes") or data.get("ticks"))
        buckets: dict[str, dict] = {}
        for tick in ticks:
            if not isinstance(tick, dict):
                continue
            ts = tick.get("timestamp") or tick.get("time") or tick.get("datetime")
            day = _ts_to_day(ts)
            if day is None or day < start_date[:10] or day > end_date[:10]:
                continue
            minute_key = _ts_to_minute(ts)
            if minute_key is None:
                continue
            # Floor to interval_multiplier minutes.
            floored = _floor_minute(minute_key, interval_multiplier)
            price = tick.get("price")
            if price is None:
                continue
            try:
                px = float(price)
            except (TypeError, ValueError):
                continue
            vol = int(tick.get("volume") or 0)
            bucket = buckets.get(floored)
            if bucket is None:
                buckets[floored] = {
                    "open": px, "high": px, "low": px, "close": px, "volume": vol,
                }
            else:
                bucket["high"] = max(bucket["high"], px)
                bucket["low"] = min(bucket["low"], px)
                bucket["close"] = px
                bucket["volume"] += vol
        out = [
            Price(open=b["open"], high=b["high"], low=b["low"], close=b["close"],
                  volume=b["volume"], time=t)
            for t, b in sorted(buckets.items())
        ]
        return out

    def get_snapshot(self, ticker: str) -> PriceSnapshot | None:
        data = self._get_json("/market/quotes/latest", {"stocks": ticker})
        row = _extract_latest_quote(data, ticker)
        if row is None:
            # Fallback: last equity quote point.
            quotes = self._get_json(f"/equity/quotes/{_enc(ticker)}")
            qlist = _as_list(quotes)
            if not qlist:
                return None
            last = qlist[-1] if isinstance(qlist[-1], dict) else None
            if not last or last.get("price") is None:
                return None
            return PriceSnapshot(
                ticker=ticker.upper(),
                price=float(last["price"]),
                time=_ts_to_iso(last.get("timestamp")) or datetime.now(timezone.utc).isoformat(),
            )
        price = row.get("price") or row.get("last") or row.get("close")
        if price is None:
            return None
        return PriceSnapshot(
            ticker=str(row.get("ticker") or row.get("symbol") or ticker).upper(),
            price=float(price),
            time=_ts_to_iso(row.get("timestamp") or row.get("time"))
            or datetime.now(timezone.utc).isoformat(),
            day_change=_as_float(row.get("day_change") or row.get("change")),
            day_change_percent=_as_float(
                row.get("day_change_percent") or row.get("changePercent") or row.get("change_percent")
            ),
        )

    # ------------------------------------------------------------------
    # Financial metrics (point-in-time best effort)
    # ------------------------------------------------------------------

    def get_financial_metrics(
        self,
        ticker: str,
        end_date: str,
        period: str = "ttm",
        limit: int = 10,
    ) -> list[FinancialMetrics]:
        """Return metrics knowable on ``end_date``.

        Uses ``/equity/metrics`` historical rows when they carry dates.
        Current cross-section values are returned only when ``end_date`` is
        recent (paper). Older ``end_date`` without dated history raises —
        returning today's ratios would lookahead into a backtest.
        """
        data = self._get_json(f"/equity/metrics/{_enc(ticker)}")
        if data is None:
            return []
        if not isinstance(data, dict):
            raise EulerpoolClientError(
                f"Unexpected metrics payload type for {ticker}: {type(data).__name__}",
                path="/equity/metrics",
            )

        historical = data.get("historical") or data.get("history") or []
        dated_rows: list[FinancialMetrics] = []
        if isinstance(historical, list):
            for row in historical:
                if not isinstance(row, dict):
                    continue
                mapped = _metrics_from_historical_row(ticker, row, period)
                if mapped is None:
                    continue
                stamp = mapped.filing_date or mapped.report_period
                if stamp and stamp[:10] <= end_date[:10]:
                    dated_rows.append(mapped)

        if dated_rows:
            dated_rows.sort(key=lambda m: m.filing_date or m.report_period, reverse=True)
            return dated_rows[:limit]

        if not _is_recent(end_date):
            raise EulerpoolClientError(
                "Eulerpool /equity/metrics has no dated historical rows for "
                f"{ticker}, and end_date={end_date} is not recent enough to "
                "use the current cross-section without lookahead. Honest "
                "point-in-time backtests need filing-dated fundamentals; use "
                "paper (near-today end_date), DATA_PROVIDER=financial_datasets, "
                "or wait until Eulerpool exposes dated metric history.",
                path="/equity/metrics",
            )

        current = _metrics_from_current(ticker, data, period, end_date)
        return [current][:limit] if current else []

    # ------------------------------------------------------------------
    # News
    # ------------------------------------------------------------------

    def get_news(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[CompanyNews]:
        data = self._get_json(f"/research/news/{_enc(ticker)}")
        rows = _as_list(data)
        out: list[CompanyNews] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = _ts_to_day(row.get("datetime") or row.get("date") or row.get("published"))
            if day is None:
                continue
            if day > end_date[:10]:
                continue
            if start_date and day < start_date[:10]:
                continue
            title = row.get("headline") or row.get("title")
            if not title:
                continue
            out.append(CompanyNews(
                ticker=ticker.upper(),
                title=str(title),
                source=str(row.get("source") or row.get("publisher") or "unknown"),
                date=day,
                url=row.get("url") or row.get("link"),
            ))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # Insider trades
    # ------------------------------------------------------------------

    def get_insider_trades(
        self,
        ticker: str,
        end_date: str,
        start_date: str | None = None,
        limit: int = 1000,
    ) -> list[InsiderTrade]:
        data = self._get_json(f"/equity/insider-trades/{_enc(ticker)}")
        rows = _as_list(data)
        if not rows and isinstance(data, dict):
            rows = _as_list(
                data.get("trades") or data.get("insiderTrades") or data.get("data") or data.get("transactions")
            )
        out: list[InsiderTrade] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            filing = _ts_to_day(
                row.get("filing_date") or row.get("filingDate") or row.get("filedDate")
                or row.get("acceptedDate") or row.get("date")
            )
            if filing is None:
                continue
            if filing > end_date[:10]:
                continue
            if start_date and filing < start_date[:10]:
                continue
            name = row.get("name") or row.get("insiderName") or row.get("reportingName") or "unknown"
            out.append(InsiderTrade(
                ticker=ticker.upper(),
                name=str(name),
                filing_date=filing,
                is_board_director=bool(
                    row.get("is_board_director") or row.get("isDirector") or row.get("director")
                ),
                issuer=row.get("issuer"),
                title=row.get("title") or row.get("position") or row.get("relation"),
                transaction_date=_ts_to_day(
                    row.get("transaction_date") or row.get("transactionDate") or row.get("tradeDate")
                ),
                transaction_type=row.get("transaction_type") or row.get("transactionType") or row.get("type"),
                transaction_shares=_as_float(
                    row.get("transaction_shares") or row.get("shares") or row.get("transactionShares")
                ),
                transaction_price_per_share=_as_float(
                    row.get("transaction_price_per_share")
                    or row.get("price")
                    or row.get("transactionPrice")
                ),
                transaction_value=_as_float(
                    row.get("transaction_value") or row.get("value") or row.get("transactionValue")
                ),
                shares_owned_after_transaction=_as_float(
                    row.get("shares_owned_after_transaction") or row.get("sharesOwnedAfter")
                ),
                shares_owned_before_transaction=_as_float(
                    row.get("shares_owned_before_transaction") or row.get("sharesOwnedBefore")
                ),
                security_title=row.get("security_title") or row.get("securityTitle"),
            ))
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # Company facts
    # ------------------------------------------------------------------

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        data = self._get_json(f"/equity/profile/{_enc(ticker)}", {"language": "en"})
        if not data:
            return None
        if not isinstance(data, dict):
            raise EulerpoolClientError(
                f"Unexpected profile payload for {ticker}",
                path="/equity/profile",
            )
        # Empty object from a known ticker still counts as a profile miss.
        if not data.get("ticker") and not data.get("name") and not data.get("isin"):
            # Some payloads nest under "profile" / "data".
            nested = data.get("profile") or data.get("data")
            if isinstance(nested, dict):
                data = nested
            elif not any(data.values()):
                return None
        return CompanyFacts(
            ticker=str(data.get("ticker") or data.get("symbol") or ticker).upper(),
            is_active=bool(data.get("is_active", data.get("isActive", True))),
            name=data.get("name") or data.get("companyName"),
            cik=str(data["cik"]) if data.get("cik") is not None else None,
            sector=data.get("sector") or data.get("gsector") or data.get("gSector"),
            industry=data.get("industry") or data.get("gindustry") or data.get("gIndustry"),
            category=data.get("category"),
            exchange=data.get("exchange") or data.get("exchangeCode"),
            location=data.get("location") or data.get("country"),
            sec_filings_url=data.get("sec_filings_url") or data.get("secFilingsUrl"),
            sic_code=str(data["sic_code"]) if data.get("sic_code") is not None else (
                str(data["sic"]) if data.get("sic") is not None else None
            ),
            sic_industry=data.get("sic_industry") or data.get("sicIndustry"),
            sic_sector=data.get("sic_sector") or data.get("sicSector"),
        )

    # ------------------------------------------------------------------
    # Earnings / PEAD inputs
    # ------------------------------------------------------------------

    def get_earnings(self, ticker: str) -> Earnings | None:
        history = self.get_earnings_history(ticker, limit=1)
        if not history:
            return None
        row = history[0]
        return Earnings(
            ticker=row.ticker,
            report_period=row.report_period,
            fiscal_period=row.fiscal_period,
            currency=row.currency,
            quarterly=row.quarterly,
            annual=row.annual,
        )

    def get_earnings_history(
        self,
        ticker: str,
        limit: int = 12,
    ) -> list[EarningsRecord]:
        """Build PEAD-oriented earnings history from calendar surprises.

        Joins ``/calendar/earnings-surprises/{symbol}`` with nearby SEC 8-K
        filings when present so default ``announcement_only`` PEAD can fire.
        Unmatched rows use ``source_type='calendar'`` and the surprise date
        as ``filing_date`` (see module docstring honesty note).
        """
        surprises = self._get_json(f"/calendar/earnings-surprises/{_enc(ticker)}")
        rows = _as_list(surprises)
        if not rows:
            # Fail-loud only when the endpoint itself is broken; empty means
            # the ticker has no surprise history.
            return []

        filings_8k = self._sec_8k_dates(ticker)
        out: list[EarningsRecord] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            surprise_day = _ts_to_day(row.get("date") or row.get("filingDate") or row.get("reportDate"))
            if surprise_day is None:
                continue
            actual = _as_float(row.get("epsActual") or row.get("eps_actual") or row.get("actual"))
            estimate = _as_float(
                row.get("epsEstimate") or row.get("eps_estimate") or row.get("estimate")
            )
            label = _surprise_label(actual, estimate, row.get("surprisePercent") or row.get("epsDifference"))
            if label is None:
                continue
            report_period = _report_period_from_surprise(row, surprise_day)
            matched_8k = _nearest_date(surprise_day, filings_8k, _SURPRISE_8K_MATCH_DAYS)
            source_type = "8-K" if matched_8k else "calendar"
            filing_date = matched_8k or surprise_day
            out.append(EarningsRecord(
                ticker=ticker.upper(),
                report_period=report_period,
                source_type=source_type,
                filing_date=filing_date,
                fiscal_period=_fiscal_period(row),
                quarterly=EarningsData(
                    earnings_per_share=actual,
                    estimated_earnings_per_share=estimate,
                    eps_surprise=label,
                ),
            ))
            if len(out) >= limit:
                break
        return out

    def _sec_8k_dates(self, ticker: str) -> list[str]:
        try:
            data = self._get_json(f"/equity-extended/sec-filings/{_enc(ticker)}")
        except EulerpoolClientError as exc:
            # Missing entitlement / 404 → no 8-K join, not a hard failure for PEAD.
            if exc.status_code in (404, None) or (exc.status_code and exc.status_code < 500):
                logger.info("SEC filings unavailable for %s: %s", ticker, exc)
                return []
            raise
        rows = _as_list(data)
        dates: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            form = str(row.get("form") or row.get("filing_type") or row.get("type") or "").upper()
            if "8-K" not in form and form != "8K":
                continue
            day = _ts_to_day(
                row.get("acceptedDate") or row.get("filedDate") or row.get("filing_date") or row.get("date")
            )
            if day:
                dates.append(day)
        return dates

    # ------------------------------------------------------------------
    # Market cap
    # ------------------------------------------------------------------

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        # Prefer historical series filtered to end_date.
        try:
            span = max(0, (date.today() - _parse_day(end_date)).days)
            range_key = _candle_range_for_span(span + 7)
            if range_key not in {"1m", "3m", "6m", "ytd", "1y", "2y", "5y"}:
                range_key = "5y"
            data = self._get_json(f"/equity/market-cap/{_enc(ticker)}", {"range": range_key})
        except EulerpoolClientError:
            data = None
        series = []
        if isinstance(data, dict):
            series = _as_list(data.get("data") or data.get("series") or data.get("history"))
        elif isinstance(data, list):
            series = data
        best: tuple[str, float] | None = None
        for row in series:
            if not isinstance(row, dict):
                continue
            day = _ts_to_day(row.get("timestamp") or row.get("date"))
            cap = _as_float(row.get("marketCap") or row.get("market_cap"))
            if day is None or cap is None or day > end_date[:10]:
                continue
            # Series values are in millions; expose absolute currency units.
            cap_abs = cap * _MARKET_CAP_MILLIONS
            if best is None or day > best[0]:
                best = (day, cap_abs)
        if best is not None:
            return best[1]

        if not _is_recent(end_date):
            # Avoid returning today's metrics.marketCap for a historical as-of.
            return None
        try:
            metrics = self.get_financial_metrics(ticker, end_date, limit=1)
        except EulerpoolClientError:
            return None
        if metrics and metrics[0].market_cap is not None:
            return metrics[0].market_cap
        return None

    # ------------------------------------------------------------------
    # Screener
    # ------------------------------------------------------------------

    def screen(
        self,
        as_of: str,
        filters: list[ScreenFilter],
        *,
        order_by: OrderBy | None = None,
        limit: int = SCREEN_LIMIT,
    ) -> list[ScreenRow]:
        if not as_of:
            raise ValueError("screen needs an as_of date: a screen without one reads today's numbers")
        if not filters:
            raise ValueError("screen needs at least one filter")
        if not _is_recent(as_of):
            raise EulerpoolClientError(
                "Eulerpool screener has no historical as_of / point-in-time "
                f"snapshot; refusing as_of={as_of}. Use a near-today as_of for "
                "paper screens, or DATA_PROVIDER=financial_datasets for PIT "
                "backtest screening.",
                path="/screener/screen",
            )

        body: dict = {
            "filters": [_to_ep_filter(f) for f in filters],
            "page": 0,
            "size": min(max(limit, 1), 100),
        }
        if order_by is not None:
            body["order"] = {
                "field": _SCREEN_FIELD_TO_EP.get(order_by.field, order_by.field),
                "direction": order_by.direction,
            }
        # US equities default when the caller did not constrain country.
        if not any(f.field in {"country", "ticker"} for f in filters):
            body["filters"].append({"id": "country", "value": ["US"], "equality": "IN"})

        resp = self._request("POST", "/screener/screen", json=body)
        if resp is None:
            raise EulerpoolClientError(
                "POST /screener/screen returned 404",
                status_code=404,
                path="/screener/screen",
            )
        payload = resp.json()
        rows = _as_list(payload)
        if not rows and isinstance(payload, dict):
            rows = _as_list(payload.get("results") or payload.get("content") or payload.get("data"))

        stamp = as_of[:10]
        out: list[ScreenRow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker") or row.get("symbol")
            if not ticker:
                continue
            values = {
                key: float(val)
                for key, val in row.items()
                if key not in {
                    "ticker", "symbol", "isin", "name", "country", "sector",
                    "industry", "gsector", "gindustry",
                } and isinstance(val, (int, float)) and not isinstance(val, bool)
            }
            # Normalize common aliases into FD field names used upstream.
            if "marketCap" in values and "market_cap" not in values:
                values["market_cap"] = values["marketCap"]
            out.append(ScreenRow(
                ticker=str(ticker).upper(),
                filing_date=stamp,
                report_period=stamp,
                currency=row.get("currency"),
                sector=row.get("sector") or row.get("gsector"),
                industry=row.get("industry") or row.get("gindustry"),
                values=values,
            ))
            if len(out) >= limit:
                break
        return out

    def screener_fields(self) -> ScreenerFields:
        data = self._get_json("/screener/metadata")
        if data is None:
            raise EulerpoolClientError(
                "GET /screener/metadata returned 404",
                status_code=404,
                path="/screener/metadata",
            )
        if not isinstance(data, dict):
            raise EulerpoolClientError(
                "Unexpected screener metadata payload",
                path="/screener/metadata",
            )
        sectors = {
            str(item.get("sector") or item)
            for item in _as_list(data.get("sectors"))
            if item
        }
        industries = {
            str(item.get("industry") or item)
            for item in _as_list(data.get("industries"))
            if item
        }
        # Eulerpool metadata does not publish a full numeric field catalog;
        # advertise the columns we know how to map from ScreenFilter.
        numeric = frozenset({
            "market_cap", "price_to_earnings_ratio", "price_to_book_ratio",
            "price_to_sales_ratio", "marketCap", "pe", "pb", "ps",
        })
        return ScreenerFields(
            numeric=numeric,
            sectors=frozenset(sectors),
            industries=frozenset(industries),
            operators=frozenset({"gt", "lt", "gte", "lte", "eq", "in"}),
            unavailable_as_of=frozenset({"stock_price"}),
        )

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get_json(self, path: str, params: dict | None = None):
        resp = self._request("GET", path, params=params or {})
        if resp is None:
            return None
        return resp.json()

    def _request(self, method: str, path: str, **kwargs) -> requests.Response | None:
        url = path if path.startswith("http") else self.BASE_URL + path
        # Also accept token query for environments that prefer ?token=.
        params = dict(kwargs.pop("params", None) or {})
        if self._api_key and "token" not in params:
            params["token"] = self._api_key
        for attempt, delay in enumerate((*self._RETRY_DELAYS, None)):
            self.meter.record(url)
            try:
                resp = self._session.request(
                    method, url, params=params, timeout=self._timeout, **kwargs,
                )
            except requests.RequestException as exc:
                raise EulerpoolClientError(
                    f"{method} {path} failed: {exc}", path=path,
                ) from exc

            if resp.status_code in (429, 503) and delay is not None:
                logger.info(
                    "Got %d from Eulerpool, retrying in %ds (attempt %d/%d)",
                    resp.status_code, delay, attempt + 1, len(self._RETRY_DELAYS),
                )
                time.sleep(delay)
                continue

            if resp.status_code == 404:
                return None

            if resp.status_code >= 400:
                raise EulerpoolClientError(
                    f"{method} {path} returned {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                    path=path,
                )
            return resp

        raise EulerpoolClientError(
            f"{method} {path} returned {resp.status_code} after {len(self._RETRY_DELAYS)} retries",
            status_code=resp.status_code,
            path=path,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enc(identifier: str) -> str:
    return quote(identifier, safe="")


def _as_list(data) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return data
    return []


def _as_float(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])


def _is_recent(day: str, *, today: date | None = None) -> bool:
    ref = today or date.today()
    return abs((ref - _parse_day(day)).days) <= _RECENT_DAYS


def _candle_range_for_span(span_days: int) -> str:
    for key, days in _CANDLE_RANGES:
        if span_days <= days:
            return key
    return "max"


def _ts_to_day(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return _ts_to_day(int(text))
    return text[:10]


def _ts_to_iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    text = str(value).strip()
    return text or None


def _ts_to_minute(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")
    text = str(value).strip()
    if not text:
        return None
    # 2026-03-02T14:30:00.000Z → 2026-03-02T14:30
    if "T" in text:
        return text.replace("Z", "")[:16]
    return None


def _floor_minute(minute_key: str, multiplier: int) -> str:
    if multiplier <= 1:
        return minute_key
    dt = datetime.fromisoformat(minute_key)
    floored = dt.minute - (dt.minute % multiplier)
    return dt.replace(minute=floored, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _extract_latest_quote(data, ticker: str) -> dict | None:
    if data is None:
        return None
    upper = ticker.upper()
    if isinstance(data, dict):
        # { "AAPL": {...} } or { "data": [ ... ] }
        if upper in data and isinstance(data[upper], dict):
            return data[upper]
        for key in ("data", "quotes", "results", "stocks"):
            nested = data.get(key)
            if isinstance(nested, dict) and upper in nested:
                return nested[upper]
            if isinstance(nested, list):
                data = nested
                break
        else:
            if "price" in data or "last" in data:
                return data
            return None
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("ticker") or row.get("symbol") or "").upper()
            if sym == upper or len(data) == 1:
                return row
    return None


def _metrics_from_current(
    ticker: str,
    data: dict,
    period: str,
    end_date: str,
) -> FinancialMetrics | None:
    valuation = data.get("valuation") if isinstance(data.get("valuation"), dict) else {}
    profitability = data.get("profitability") if isinstance(data.get("profitability"), dict) else {}
    per_share = data.get("perShare") if isinstance(data.get("perShare"), dict) else {}
    leverage = data.get("leverage") if isinstance(data.get("leverage"), dict) else {}
    growth = data.get("growth") if isinstance(data.get("growth"), dict) else {}
    if not any((valuation, profitability, per_share, leverage, growth)):
        return None
    return FinancialMetrics(
        ticker=str(data.get("ticker") or ticker).upper(),
        report_period=end_date[:10],
        period=period,
        currency=data.get("currency"),
        filing_date=None,
        market_cap=_scale_market_cap_millions(valuation.get("marketCap")),
        # enterpriseValue unit is undocumented / often null; leave unscaled.
        enterprise_value=_as_float(valuation.get("enterpriseValue")),
        price_to_earnings_ratio=_as_float(valuation.get("pe")),
        price_to_book_ratio=_as_float(valuation.get("pb")),
        price_to_sales_ratio=_as_float(valuation.get("ps")),
        enterprise_value_to_ebitda_ratio=_as_float(valuation.get("evEbitda")),
        gross_margin=_as_float(profitability.get("grossMargin")),
        operating_margin=_as_float(profitability.get("operatingMargin")),
        net_margin=_as_float(profitability.get("netMargin")),
        return_on_equity=_as_float(profitability.get("roe")),
        return_on_assets=_as_float(profitability.get("roa")),
        return_on_invested_capital=_as_float(profitability.get("roic")),
        debt_to_equity=_as_float(leverage.get("debtToEquity")),
        current_ratio=_as_float(leverage.get("currentRatio")),
        revenue_growth=_as_float(growth.get("revenueGrowth3Y") or growth.get("revenueGrowth5Y")),
        earnings_growth=_as_float(growth.get("earningsGrowth3Y") or growth.get("earningsGrowth5Y")),
        earnings_per_share=_as_float(per_share.get("eps")),
        book_value_per_share=_as_float(per_share.get("bps")),
    )


def _metrics_from_historical_row(ticker: str, row: dict, period: str) -> FinancialMetrics | None:
    day = _ts_to_day(
        row.get("filing_date") or row.get("filingDate") or row.get("date")
        or row.get("report_period") or row.get("reportPeriod") or row.get("period")
    )
    if day is None:
        return None
    valuation = row.get("valuation") if isinstance(row.get("valuation"), dict) else row
    return FinancialMetrics(
        ticker=ticker.upper(),
        report_period=_ts_to_day(row.get("report_period") or row.get("reportPeriod") or day) or day,
        period=str(row.get("period") or period),
        currency=row.get("currency"),
        filing_date=_ts_to_day(row.get("filing_date") or row.get("filingDate")) or day,
        market_cap=_as_float(valuation.get("marketCap") or valuation.get("market_cap")),
        price_to_earnings_ratio=_as_float(
            valuation.get("pe") or valuation.get("price_to_earnings_ratio") or valuation.get("kgv")
        ),
        price_to_book_ratio=_as_float(
            valuation.get("pb") or valuation.get("price_to_book_ratio")
        ),
        price_to_sales_ratio=_as_float(
            valuation.get("ps") or valuation.get("price_to_sales_ratio") or valuation.get("kuv")
        ),
        earnings_per_share=_as_float(valuation.get("eps") or valuation.get("earnings_per_share")),
    )


def _surprise_label(actual: float | None, estimate: float | None, hint) -> str | None:
    if actual is not None and estimate is not None:
        if actual > estimate:
            return "BEAT"
        if actual < estimate:
            return "MISS"
        return "MEET"
    # Fall back to surprise percent / difference sign when present.
    delta = _as_float(hint)
    if delta is None:
        return None
    if delta > 0:
        return "BEAT"
    if delta < 0:
        return "MISS"
    return "MEET"


def _report_period_from_surprise(row: dict, surprise_day: str) -> str:
    quarter = row.get("quarter")
    year = row.get("year")
    if isinstance(quarter, int) and isinstance(year, int) and 1 <= quarter <= 4:
        month = quarter * 3
        # Fiscal quarter end approximation (calendar quarters).
        day = 30 if month in (6, 9) else (31 if month in (3, 12) else 28)
        if month == 2:
            day = 28
        return f"{year:04d}-{month:02d}-{day:02d}"
    # Fallback: day before the surprise (common for report_period < filing).
    return (_parse_day(surprise_day) - timedelta(days=1)).isoformat()


def _fiscal_period(row: dict) -> str | None:
    quarter = row.get("quarter")
    year = row.get("year")
    if isinstance(quarter, int) and isinstance(year, int):
        return f"Q{quarter} {year}"
    return None


def _nearest_date(target: str, candidates: list[str], max_days: int) -> str | None:
    if not candidates:
        return None
    t = _parse_day(target)
    best: tuple[int, str] | None = None
    for cand in candidates:
        lag = abs((_parse_day(cand) - t).days)
        if lag > max_days:
            continue
        if best is None or lag < best[0]:
            best = (lag, cand)
    return best[1] if best else None


def _scale_market_cap_millions(raw) -> float | None:
    """Convert Eulerpool million-unit market/EV figures to absolute currency."""
    value = _as_float(raw)
    if value is None:
        return None
    return value * _MARKET_CAP_MILLIONS


def _to_ep_filter(item: ScreenFilter) -> dict:
    field = _SCREEN_FIELD_TO_EP.get(item.field, item.field)
    equality = _SCREEN_OP_TO_EP.get(item.operator)
    if equality is None:
        raise EulerpoolClientError(
            f"Unsupported screener operator {item.operator!r} for Eulerpool",
            path="/screener/screen",
        )
    value = item.value
    if item.operator == "in" and isinstance(value, str):
        value = [value]
    if item.field == "country":
        # Live Eulerpool screener returns [] for country equality="=" /
        # scalar value; IN + list works.
        if isinstance(value, str):
            value = [value.upper()]
        elif isinstance(value, list):
            value = [str(v).upper() for v in value]
        if equality == "=":
            equality = "IN"
    return {"id": field, "value": value, "equality": equality}
