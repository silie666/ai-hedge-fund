# Eulerpool data provider

This fork defaults to **Eulerpool** as the market-data backend for US
equities. Financial Datasets remains selectable.

## Configuration

| Variable | Role |
|----------|------|
| `EULERPOOL_API_KEY` | **Required** for `DATA_PROVIDER=eulerpool` (user-supplied) |
| `DATA_PROVIDER` | `eulerpool` (default) or `financial_datasets` |
| `FINANCIAL_DATASETS_API_KEY` / `X402_WALLET_PRIVATE_KEY` | Only when using Financial Datasets |

LLM provider keys (`ANTHROPIC_API_KEY`, …) are optional for the user when the
Cursor / platform runtime already injects them. The TUI skips model-key
prompts if the selected provider’s key is present in the process environment
and does not write those injected keys into `~/.hedge-fund/.env` unless you
paste one into the prompt (BYO).

Wiring: `make_data_client()` in `hedge_fund/data/factory.py` builds
`EulerpoolClient` or `FDClient`. All CLI / TUI call sites use the factory.

## Capability matrix

| DataClient method | Eulerpool support | Notes |
|-------------------|-------------------|-------|
| `get_prices` (day) | Yes | Candles via `/equity/candles`; volume often missing → `0` |
| `get_prices` (minute) | Best-effort | `/market/quotes/intraday` is current/previous session only; older windows **raise** |
| `get_snapshot` | Yes | `/market/quotes/latest`, fallback `/equity/quotes` |
| `get_financial_metrics` | Partial PIT | Dated `historical[]` rows when present; current cross-section only if `end_date` is recent (~5 days); otherwise **raises** (no silent lookahead) |
| `get_company_facts` | Yes | `/equity/profile` |
| `get_news` | Yes | `/research/news/{ticker}`; client-side date filter |
| `get_insider_trades` | Best-effort | `/equity/insider-trades`; local date filter; field shapes vary |
| `get_market_cap` | Yes | `/equity/market-cap` history filtered by `end_date` |
| `get_earnings` / `get_earnings_history` | Best-effort PEAD | Calendar surprises + nearest SEC 8-K when available |
| `screen` / `screener_fields` | Best-effort | No historical `as_of`; near-today only, else **raises** |

Empty list / `None` still means “genuinely no rows”. Auth, 429, 5xx, network,
and documented capability gaps raise `EulerpoolClientError` (a
`DataClientError`).

## Honesty limits (backtest / PEAD / minute / screener)

### Point-in-time fundamentals

Upstream FD filters metrics on `filing_date_lte`. Eulerpool’s public metrics
endpoint is primarily a **current** cross-section; `historical` may lack
`filing_date`. This client:

- Uses dated historical rows when the payload provides dates.
- Allows the current cross-section only for **recent** `end_date` (paper).
- **Raises** for older `end_date` without dated history instead of leaking
  today’s ratios into a backtest.

Until Eulerpool exposes filing-dated metric history end-to-end, treat LLM
agent backtests as **approximate**. Prefer paper for live-ish metrics, or
`DATA_PROVIDER=financial_datasets` for stricter PIT.

### PEAD / earnings-drift

Built from `/calendar/earnings-surprises/{symbol}` joined to nearby
`/equity-extended/sec-filings` 8-K rows when possible:

- Matched 8-K → `source_type=8-K`, `filing_date` from the filing (works with
  default `announcement_only=True` PEAD).
- No 8-K match → `source_type=calendar`, `filing_date` = surprise `date`.
  Default PEAD **skips** those rows; set `announcement_only=False` only if you
  accept calendar dates as event times.

Calendar `date` is an announcement/report date, not necessarily the SEC
`filing_date` / `acceptedDate`. Document this when reading PEAD results.

### Minute bars

Eulerpool intraday quotes cover the **current/previous** session. Historical
minute windows raise a clear error. Minute funds over multi-day history need
Financial Datasets (or another minute vendor).

### Screener

Eulerpool `/screener/screen` has **no** historical `as_of`. Near-today
screens run (US country filter added by default); rows carry a synthetic
`filing_date=as_of`. Historical `as_of` **raises**. Point-in-time universe
construction for backtests should use Financial Datasets or a local PIT
universe file.

## Tests

Fixture / mocked HTTP tests live in
`hedge_fund/data/test_eulerpool_client.py` (no live key). Live smoke against
Eulerpool requires `EULERPOOL_API_KEY` in the environment and is not part of
the default CI path.
