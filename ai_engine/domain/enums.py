"""Enumerations shared by the domain models (BS-038).

Kept in their own module so both the models and the agents/providers that build
them import the same canonical string values. The values are deliberately the
exact lowercase strings used by the database CHECK constraints
(``backend/db/models.py``) and the technical documentation, so a domain object
round-trips to and from a persisted row without translation.
"""

from __future__ import annotations

from enum import StrEnum


class QualityFlag(StrEnum):
    """How firm a reported value is (technical-doc §26, glossary).

    Present on every domain model. It propagates from the producing agent through
    the coordinator and optimizer to the UI, so the system is always transparent
    about provenance ("degrade, don't crash" — a missing piece downgrades quality,
    it does not abort the request).

    * ``REAL`` — freshly fetched from a live external source.
    * ``CACHED`` — served from BudgetSwarm's cache/DB (indicative, not live).
    * ``ESTIMATED`` — produced by a deterministic fallback/estimate (e.g. a
      distance-based fare, or an EU-average fuel price for an uncovered country).
    """

    REAL = "real"
    CACHED = "cached"
    ESTIMATED = "estimated"


class TransportMode(StrEnum):
    """The travel mode a :class:`~ai_engine.domain.models.TransportOption` describes.

    Matches the ``transport_options.mode`` CHECK constraint. Car is the MVP mode;
    bus/train are Post-MVP (technical-doc §9)."""

    AIRPLANE = "airplane"
    CAR = "car"
    BUS = "bus"
    TRAIN = "train"


class IndoorOutdoor(StrEnum):
    """Whether an :class:`~ai_engine.domain.models.Activity` is weather-exposed.

    Used to decide, together with a :class:`~ai_engine.domain.models.WeatherProfile`,
    whether an activity is a poor fit on a wet/cold day. Matches the
    ``attractions.indoor_outdoor`` CHECK constraint."""

    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"
