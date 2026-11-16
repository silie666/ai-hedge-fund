"""Select a DataClient implementation from ``DATA_PROVIDER``.

This fork defaults to Eulerpool. Financial Datasets remains available by
setting ``DATA_PROVIDER=financial_datasets`` (and the matching credential).
"""

from __future__ import annotations

import os

from hedge_fund.data.client import CallMeter, FDClient

DATA_PROVIDER_ENV = "DATA_PROVIDER"
DEFAULT_DATA_PROVIDER = "eulerpool"

# Aliases accepted in env / docs.
_EULERPOOL = frozenset({"eulerpool", "ep"})
_FINANCIAL_DATASETS = frozenset({"financial_datasets", "fd", "financial-datasets"})


def selected_data_provider() -> str:
    """Canonical provider id: ``eulerpool`` or ``financial_datasets``."""
    raw = (os.environ.get(DATA_PROVIDER_ENV) or DEFAULT_DATA_PROVIDER).strip().lower()
    if raw in _EULERPOOL:
        return "eulerpool"
    if raw in _FINANCIAL_DATASETS:
        return "financial_datasets"
    return raw


def make_data_client(
    *,
    meter: CallMeter | None = None,
    api_key: str | None = None,
    timeout: float = 30.0,
):
    """Build the configured production DataClient (context-manager friendly).

    Raises ``ValueError`` for an unknown ``DATA_PROVIDER``.
    """
    provider = selected_data_provider()
    if provider == "eulerpool":
        from hedge_fund.data.eulerpool_client import EulerpoolClient

        return EulerpoolClient(api_key=api_key, timeout=timeout, meter=meter)
    if provider == "financial_datasets":
        return FDClient(api_key=api_key, timeout=timeout, meter=meter)
    raise ValueError(
        f"Unknown {DATA_PROVIDER_ENV}={provider!r}; "
        f"use 'eulerpool' or 'financial_datasets'"
    )
