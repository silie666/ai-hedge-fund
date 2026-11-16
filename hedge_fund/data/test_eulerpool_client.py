"""EulerpoolClient contract tests — mocked HTTP, no live API key required."""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from hedge_fund.data.eulerpool_client import (
    EULERPOOL_API_KEY_ENV,
    EulerpoolClient,
    EulerpoolClientError,
)
from hedge_fund.data.factory import make_data_client, selected_data_provider
from hedge_fund.data.screener import ScreenFilter
from hedge_fund.data import has_data_credential


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = {}

    def json(self):
        return self._payload


def _stub(client: EulerpoolClient, responses: list[_FakeResponse]) -> None:
    client._session.request = MagicMock(side_effect=responses)


@pytest.fixture
def client(monkeypatch) -> EulerpoolClient:
    monkeypatch.setenv(EULERPOOL_API_KEY_ENV, "test-key")
    return EulerpoolClient(api_key="test-key")


def test_factory_defaults_to_eulerpool(monkeypatch):
    monkeypatch.delenv("DATA_PROVIDER", raising=False)
    assert selected_data_provider() == "eulerpool"
    monkeypatch.setenv(EULERPOOL_API_KEY_ENV, "k")
    with make_data_client() as c:
        assert isinstance(c, EulerpoolClient)


def test_factory_selects_financial_datasets(monkeypatch):
    monkeypatch.setenv("DATA_PROVIDER", "financial_datasets")
    from hedge_fund.data.client import FDClient

    with make_data_client(api_key="fd-key") as c:
        assert isinstance(c, FDClient)


def test_has_data_credential_respects_provider(monkeypatch):
    monkeypatch.delenv("EULERPOOL_API_KEY", raising=False)
    monkeypatch.delenv("FINANCIAL_DATASETS_API_KEY", raising=False)
    monkeypatch.delenv("X402_WALLET_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("DATA_PROVIDER", "eulerpool")
    assert not has_data_credential()
    monkeypatch.setenv("EULERPOOL_API_KEY", "ep")
    assert has_data_credential()
    monkeypatch.setenv("DATA_PROVIDER", "financial_datasets")
    assert not has_data_credential()
    monkeypatch.setenv("FINANCIAL_DATASETS_API_KEY", "fd")
    assert has_data_credential()


def test_get_prices_daily_filters_window(client):
    _stub(client, [_FakeResponse(200, [
        {"timestamp": 1_704_067_200_000, "open": 1, "high": 2, "low": 0.5, "close": 1.5},  # 2024-01-01
        {"timestamp": 1_704_153_600_000, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 10},  # 2024-01-02
        {"timestamp": 1_704_326_400_000, "open": 3, "high": 4, "low": 2.5, "close": 3.5},  # 2024-01-04
    ])])
    bars = client.get_prices("AAPL", "2024-01-02", "2024-01-03")
    assert len(bars) == 1
    assert bars[0].time == "2024-01-02"
    assert bars[0].volume == 10
    assert bars[0].close == 2.5


def test_get_prices_missing_volume_defaults_to_zero(client):
    _stub(client, [_FakeResponse(200, [
        {"timestamp": 1_704_153_600_000, "open": 2, "high": 3, "low": 1.5, "close": 2.5},
    ])])
    bars = client.get_prices("AAPL", "2024-01-02", "2024-01-02")
    assert bars[0].volume == 0


def test_auth_failure_raises(client):
    _stub(client, [_FakeResponse(401, text="unauthorized")])
    with pytest.raises(EulerpoolClientError, match="401"):
        client.get_prices("AAPL", "2024-01-01", "2024-01-02")


def test_rate_limit_exhausted_raises(client, monkeypatch):
    monkeypatch.setattr(EulerpoolClient, "_RETRY_DELAYS", ())
    _stub(client, [_FakeResponse(429)])
    with pytest.raises(EulerpoolClientError, match="429"):
        client.get_company_facts("AAPL")


def test_404_is_empty_not_error(client):
    _stub(client, [_FakeResponse(404)])
    assert client.get_company_facts("ZZZZ") is None


def test_financial_metrics_recent_uses_current(client, monkeypatch):
    today = date.today().isoformat()
    _stub(client, [_FakeResponse(200, {
        "ticker": "AAPL",
        "currency": "USD",
        # valuation.marketCap is in millions on the live API.
        "valuation": {"pe": 28.5, "marketCap": 1e6},
        "profitability": {"roe": 100.0},
        "perShare": {"eps": 6.0},
        "leverage": {},
        "growth": {},
        "historical": [],
    })])
    rows = client.get_financial_metrics("AAPL", today, limit=1)
    assert len(rows) == 1
    assert rows[0].price_to_earnings_ratio == 28.5
    assert rows[0].market_cap == 1e12
    assert rows[0].filing_date is None


def test_get_market_cap_scales_millions_series(client):
    today = date.today().isoformat()
    _stub(client, [_FakeResponse(200, {
        "currency": "USD",
        "sharesOutstanding": 15000.0,
        "data": [
            {"timestamp": f"{today}T00:00:00.000Z", "price": 200.0, "marketCap": 3_000_000},
        ],
    })])
    assert client.get_market_cap("AAPL", today) == 3_000_000 * 1_000_000


def test_country_eq_filter_rewrites_to_in_list():
    from hedge_fund.data.eulerpool_client import _to_ep_filter

    body = _to_ep_filter(ScreenFilter(field="country", operator="eq", value="us"))
    assert body == {"id": "country", "value": ["US"], "equality": "IN"}


def test_financial_metrics_historical_without_dates_raises(client):
    old = (date.today() - timedelta(days=120)).isoformat()
    _stub(client, [_FakeResponse(200, {
        "ticker": "AAPL",
        "valuation": {"pe": 28.5},
        "profitability": {},
        "perShare": {},
        "leverage": {},
        "growth": {},
        "historical": [],
    })])
    with pytest.raises(EulerpoolClientError, match="lookahead"):
        client.get_financial_metrics("AAPL", old)


def test_financial_metrics_dated_history_filters(client):
    _stub(client, [_FakeResponse(200, {
        "ticker": "AAPL",
        "historical": [
            {"date": "2024-06-01", "pe": 30.0},
            {"date": "2023-06-01", "pe": 20.0},
            {"date": "2025-01-01", "pe": 40.0},
        ],
    })])
    rows = client.get_financial_metrics("AAPL", "2024-12-31", limit=5)
    assert [r.price_to_earnings_ratio for r in rows] == [30.0, 20.0]


def test_minute_bars_reject_old_window(client):
    with pytest.raises(EulerpoolClientError, match="current/previous"):
        client.get_prices("AAPL", "2024-01-02", "2024-01-02", interval="minute")


def test_minute_bars_aggregate_recent_ticks(client):
    today = date.today().isoformat()
    _stub(client, [_FakeResponse(200, [
        {"timestamp": f"{today}T14:30:10.000Z", "price": 100.0, "volume": 10},
        {"timestamp": f"{today}T14:30:40.000Z", "price": 101.0, "volume": 5},
        {"timestamp": f"{today}T14:31:05.000Z", "price": 99.0, "volume": 7},
    ])])
    bars = client.get_prices("AAPL", today, today, interval="minute")
    assert len(bars) == 2
    assert bars[0].open == 100.0 and bars[0].high == 101.0 and bars[0].close == 101.0
    assert bars[0].volume == 15


def test_screen_historical_as_of_raises(client):
    with pytest.raises(EulerpoolClientError, match="point-in-time"):
        client.screen(
            "2020-01-01",
            [ScreenFilter(field="market_cap", operator="gt", value=1e9)],
        )


def test_screen_recent_maps_rows(client):
    today = date.today().isoformat()
    _stub(client, [_FakeResponse(200, [
        {"ticker": "AAPL", "sector": "Technology", "marketCap": 3e12},
    ])])
    rows = client.screen(
        today,
        [ScreenFilter(field="market_cap", operator="gt", value=1e9)],
        limit=10,
    )
    assert len(rows) == 1
    assert rows[0].ticker == "AAPL"
    assert rows[0].filing_date == today
    assert rows[0].values["market_cap"] == 3e12


def test_earnings_history_maps_surprises_and_8k(client):
    surprise = [{
        "symbol": "AAPL",
        "date": "2025-08-01T00:00:00.000Z",
        "epsEstimate": 1.0,
        "epsActual": 1.2,
        "quarter": 3,
        "year": 2025,
    }]
    filings = [{
        "form": "8-K",
        "acceptedDate": "2025-08-01T16:00:00.000Z",
        "filedDate": "2025-08-01T00:00:00.000Z",
    }]
    _stub(client, [
        _FakeResponse(200, surprise),
        _FakeResponse(200, filings),
    ])
    rows = client.get_earnings_history("AAPL", limit=4)
    assert len(rows) == 1
    assert rows[0].source_type == "8-K"
    assert rows[0].quarterly is not None
    assert rows[0].quarterly.eps_surprise == "BEAT"
    assert rows[0].filing_date == "2025-08-01"


def test_earnings_history_without_8k_uses_calendar_source(client):
    surprise = [{
        "symbol": "AAPL",
        "date": "2025-08-01T00:00:00.000Z",
        "epsEstimate": 1.0,
        "epsActual": 0.8,
        "quarter": 3,
        "year": 2025,
    }]
    _stub(client, [
        _FakeResponse(200, surprise),
        _FakeResponse(404),
    ])
    rows = client.get_earnings_history("AAPL")
    assert rows[0].source_type == "calendar"
    assert rows[0].quarterly.eps_surprise == "MISS"


def test_news_filters_by_date(client):
    _stub(client, [_FakeResponse(200, [
        {"headline": "Old", "source": "X", "datetime": "2024-01-01T10:00:00.000Z"},
        {"headline": "In range", "source": "Y", "datetime": "2024-06-01T10:00:00.000Z", "url": "https://x"},
        {"headline": "Future", "source": "Z", "datetime": "2025-01-01T10:00:00.000Z"},
    ])])
    news = client.get_news("AAPL", end_date="2024-12-31", start_date="2024-05-01")
    assert [n.title for n in news] == ["In range"]


def test_snapshot_from_latest_quotes(client):
    _stub(client, [_FakeResponse(200, {"AAPL": {"price": 190.5, "timestamp": 1_700_000_000_000}})])
    snap = client.get_snapshot("AAPL")
    assert snap is not None
    assert snap.price == 190.5
    assert snap.ticker == "AAPL"
