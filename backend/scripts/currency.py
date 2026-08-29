"""EUR currency normalization from real ECB reference rates (BS-034).

Inside Airbnb quotes each city's prices in that city's **local currency** (Prague
in CZK, Budapest in HUF, Istanbul in TRY, eurozone cities in EUR) — and the
per-row ``currency`` field is unreliable (often null), so the currency is decided
per city by the source, and the amount is converted here.

Rates come from the **European Central Bank** via the free, no-key
[Frankfurter](https://frankfurter.dev) API (ECB daily reference rates), expressed
as units-per-EUR (``EUR -> CCY``). A committed snapshot (``data/ecb_eur_rates.json``)
is the offline/CI floor so conversion is reproducible without network, mirroring
the fuel/WhereNext pattern.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = REPO_ROOT / "data" / "ecb_eur_rates.json"

FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest?base=EUR"
VALID_SOURCES = ("auto", "live", "snapshot")


@dataclass
class EurRates:
    """ECB reference rates as units-per-EUR (``EUR -> CCY``)."""

    rates: dict[str, float]  # e.g. {"CZK": 24.148, "HUF": 364.79, ...}
    rate_date: date
    source: str  # "live" | "snapshot"
    meta: dict[str, object] = field(default_factory=dict)

    def to_eur(self, amount: float | None, currency: str) -> float | None:
        """Convert ``amount`` in ``currency`` to EUR, or ``None`` if not possible.

        EUR is identity. An unknown currency (not in the rate table, and not EUR)
        returns ``None`` so the caller can flag/drop rather than store a wrong
        number. A non-positive rate is treated as unusable.
        """
        if amount is None:
            return None
        ccy = (currency or "").strip().upper()
        if ccy in ("", "EUR"):
            return round(amount, 2)
        rate = self.rates.get(ccy)
        if not rate or rate <= 0:
            logger.warning("currency: no ECB rate for %r; cannot convert to EUR", ccy)
            return None
        return round(amount / rate, 2)


def parse_payload(payload: object) -> EurRates:
    """Parse a Frankfurter/ECB payload (``{base, date, rates}``) into ``EurRates``."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rates"), dict):
        raise ValueError("ECB rates payload is not an object with a 'rates' map")
    if str(payload.get("base", "EUR")).upper() != "EUR":
        raise ValueError(f"expected EUR-based rates, got base={payload.get('base')!r}")
    rates: dict[str, float] = {}
    for code, value in payload["rates"].items():
        try:
            rate = float(value)
        except (TypeError, ValueError):
            continue
        if rate > 0:
            rates[str(code).upper()] = rate
    if not rates:
        raise ValueError("ECB rates payload contained no usable rates")
    rate_date = (
        date.fromisoformat(str(payload.get("date"))) if payload.get("date") else date.today()
    )
    return EurRates(rates, rate_date, "?", {"base": "EUR"})


def fetch_live(timeout: float = 15.0) -> dict[str, object]:
    """GET the live ECB rates from Frankfurter (raises on network error)."""
    logger.info("currency: fetching ECB EUR rates from %s", FRANKFURTER_URL)
    request = urllib.request.Request(FRANKFURTER_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (https only)
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("ECB rates response was not a JSON object")
    return payload


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, object]:
    """Load the committed ECB rates snapshot payload."""
    if not path.is_file():
        raise FileNotFoundError(f"ECB rates snapshot not found: {path}")
    with path.open(encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"ECB rates snapshot {path.name} is not a JSON object")
    return payload


def load_eur_rates(
    *, source: str = "auto", timeout: float = 15.0, snapshot_path: Path = SNAPSHOT_PATH
) -> EurRates:
    """Resolve ECB EUR rates (``auto`` = live then snapshot)."""
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    if source == "snapshot":
        result = parse_payload(load_snapshot(snapshot_path))
        result.source = "snapshot"
        return result
    if source == "live":
        result = parse_payload(fetch_live(timeout))
        result.source = "live"
        return result
    try:
        result = parse_payload(fetch_live(timeout))
        result.source = "live"
        return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("currency: live ECB fetch failed (%s); using snapshot", exc)
        result = parse_payload(load_snapshot(snapshot_path))
        result.source = "snapshot"
        return result
