"""Internal domain models — the contracts agents produce and the coordinator consumes (BS-038).

These are BudgetSwarm's *provider-agnostic* value objects. Each external source
sits behind a provider adapter (technical-doc §21) whose job is to map its
vendor-specific response into exactly one of these shapes; from that point on the
coordinator, scorer and optimizer see only the normalized form and have no
knowledge of which provider produced it (technical-doc §17/§19). That is what
makes providers swappable and the decision core source-agnostic — so **no
provider-specific field is allowed to leak into these models**.

Every value BudgetSwarm reports carries a :class:`QualityFlag`
(``real`` / ``cached`` / ``estimated``) so the system stays honest about how firm
each number is; the flag propagates from here to the optimizer and the UI
(technical-doc §16/§26, glossary "data-quality flag").

The five models the ticket defines:

* :class:`Destination` — a candidate city (ScoutAgent output).
* :class:`TransportOption` — one costed way to reach a destination (TransportAgent).
* :class:`Accommodation` — one normalized lodging option (StayAgent).
* :class:`Activity` — one thing to do at a destination (ExperienceAgent).
* :class:`WeatherProfile` — the per-day forecast for a stay (WeatherAgent).

All are frozen dataclasses: immutable value objects that validate their fields on
construction (ranges, enums, non-negative money), depending only on the Python
standard library so the domain layer stays free of any web/ORM/provider imports.
"""

from __future__ import annotations

from .enums import IndoorOutdoor, QualityFlag, TransportMode
from .models import (
    Accommodation,
    Activity,
    Destination,
    DestinationFeatures,
    TransportOption,
    WeatherDay,
    WeatherProfile,
)

__all__ = [
    # enums
    "QualityFlag",
    "TransportMode",
    "IndoorOutdoor",
    # models
    "Destination",
    "DestinationFeatures",
    "TransportOption",
    "Accommodation",
    "Activity",
    "WeatherProfile",
    "WeatherDay",
]
