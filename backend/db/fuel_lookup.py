"""Build the agent-side fuel-price lookup from the ``fuel_prices`` table (BS-057).

The car costing in :mod:`ai_engine.agents.transport_agent` prices fuel through the
DB-agnostic :class:`~ai_engine.agents.transport_agent.FuelPriceLookup` protocol so
``ai_engine`` never imports the ORM. This module is the backend bridge: it reads
the cached ``fuel_prices`` rows (ingested by BS-032 from the EU Oil Bulletin /
fuel-prices.eu) once and materialises them into an in-memory
:class:`~ai_engine.agents.transport_agent.FuelPriceTable` the agent can query.

Provenance is preserved per row: a genuine per-country price (Oil Bulletin or the
live station feed) becomes ``real``; the synthetic EU-average fill for an
uncovered country becomes ``estimated`` — matching the ingestion's own source
labels (see ``backend/scripts/fuel_prices.py``). The agent then honestly flags any
option built on an estimated price (technical-doc §9.2).
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ai_engine.agents.transport_agent import FuelPriceEntry, FuelPriceTable
from ai_engine.domain import QualityFlag
from db.repositories import FuelPriceRepository

logger = logging.getLogger(__name__)


def _row_quality(source: str | None) -> QualityFlag:
    """Classify a ``fuel_prices`` row's firmness from its ingestion source label.

    Only the **EU-average** fill is an estimate — its label is "… (EU avg)" (see
    ``scripts/fuel_prices.py`` ``SOURCE_EU_AVG``), used for the synthetic ``EU`` row
    and for any country the bulletin does not cover. A genuine per-country price —
    the Oil Bulletin itself or the fuel-prices.eu live *station* average — is
    ``real``, so the ``"eu avg"`` match is deliberately narrower than a bare
    ``"avg"`` (which the live "station avg" label also contains).
    """
    return QualityFlag.ESTIMATED if "eu avg" in (source or "").lower() else QualityFlag.REAL


def load_fuel_price_table(session: Session) -> FuelPriceTable:
    """Read every ``fuel_prices`` row and build a :class:`FuelPriceTable`.

    Numeric prices are converted to ``float`` EUR/L; provenance is derived from the
    row's ``source``. The resulting table resolves a country → its price, falling
    back to the stored ``EU`` average and then to the static BS-003 default, so the
    agent can always price a car trip.
    """
    rows = FuelPriceRepository(session).list()
    entries: dict[str, FuelPriceEntry] = {
        row.country_code.upper(): FuelPriceEntry(
            petrol_e95=float(row.petrol_e95) if row.petrol_e95 is not None else None,
            diesel=float(row.diesel) if row.diesel is not None else None,
            quality_flag=_row_quality(row.source),
        )
        for row in rows
    }
    logger.debug("fuel: loaded %d fuel_prices rows into lookup table", len(entries))
    return FuelPriceTable(entries)
