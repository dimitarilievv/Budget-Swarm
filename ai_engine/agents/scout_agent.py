"""ScoutAgent — coarse budget pre-filter (BS-054; technical-doc §8.3 step 1).

Scout is the funnel that narrows "anywhere" to a short list *before* the expensive
per-destination agents run. This module implements the first of its three stages:
the **coarse budget pre-filter**, which drops destinations that clearly cannot fit
inside the user's budget for the given duration and party size. Preference ranking
(§8.3 step 2) and top-k selection (§8.3 step 3) are separate tickets (BS-055).

Design constraints from the ticket and the technical documentation:

* **Cheap, local estimates only — no external calls.** The pre-filter runs before
  TransportAgent/StayAgent/… so it must not hit any provider. It reasons purely
  over data already ingested locally (technical-doc §8.3: "Scout intentionally uses
  cheap, local estimates only. Precise costs come later from the specialist
  agents").
* **Deterministic.** The same request + candidates + reference data always yield
  the same kept/dropped split and ordering.
* **Lenient by construction.** The purpose is to remove *hopeless* candidates, not
  to cost a trip precisely — that is the specialist agents' job. The estimate is a
  rough floor and a configurable ``keep_margin`` biases toward keeping borderline
  candidates, so a viable destination is never pruned on a coarse guess.

Every per-city number the estimate consumes comes from a **real ingested source**,
never a hand-picked figure:

* on-the-ground daily living cost is derived from the destination's
  ``cost_of_living`` index (WhereNext, BS-011/BS-031) via an anchor computed from
  the real WhereNext + ECB datasets (``ReferenceData.from_data_dir``);
* car fuel cost uses the real EU Weekly Oil Bulletin petrol prices
  (``data/fuel_prices_live_summary.json``);
* the air/bus/train per-km coefficients are the same **clearly-labelled
  approximation constants** the distance-fare estimate uses (technical-doc §9.1),
  not ingested prices — they are algorithm parameters of a documented estimate,
  collected in :class:`PrefilterPolicy` and configurable.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_engine.domain import Destination, TransportMode
from ai_engine.providers.flight import FARE_BASE_EUR, FARE_PER_KM_EUR
from ai_engine.providers.route import ROAD_DETOUR_FACTOR
from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# Repository ``data/`` directory (…/ai_engine/agents/scout_agent.py -> repo root).
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_WHERENEXT_FILE = "wherenext_cost_of_living.json"
_ECB_FILE = "ecb_eur_rates.json"
_FUEL_FILE = "fuel_prices_live_summary.json"

# A single vehicle serves the whole party by car (its fuel cost is per-trip, not
# per-person); above this many travellers a second car is assumed.
_CAR_SEATS = 4


@dataclass(frozen=True)
class PrefilterPolicy:
    """Tunable parameters of the coarse estimate — documented approximations, not
    ingested data (technical-doc §8.3/§9.1).

    ``air_*``/``bus_per_km_eur``/``train_per_km_eur`` mirror the distance-fare
    estimate constants (§9.1); they turn a great-circle distance into an indicative
    one-way fare per traveller. ``road_detour_factor`` scales the straight-line
    distance to approximate real road distance for car fuel. ``keep_margin``
    multiplies the budget when deciding affordability: >1 keeps candidates whose
    rough floor is somewhat above budget, so the coarse funnel errs toward keeping.
    ``default_vehicle_consumption`` (L/100 km) is used only when the request does
    not carry the user's own vehicle figure.
    """

    air_base_eur: float = FARE_BASE_EUR
    air_per_km_eur: float = FARE_PER_KM_EUR
    bus_per_km_eur: float = 0.05
    train_per_km_eur: float = 0.11
    road_detour_factor: float = ROAD_DETOUR_FACTOR
    default_vehicle_consumption: float = 7.0  # L/100 km, typical compact car
    keep_margin: float = 1.2


@dataclass(frozen=True)
class ReferenceData:
    """Real, locally-ingested figures the estimate needs, with test-injectable
    values.

    * ``eur_per_cost_index_day`` — EUR of daily living per point of the WhereNext
      ``cost_of_living`` index, derived from the real WhereNext monthly estimates
      converted with the real ECB EUR/USD rate. WhereNext's model is deterministic,
      so this anchor is stable (~1.05) across its 95 countries.
    * ``petrol_by_country`` — petrol (SP95) price in EUR/L per ISO-2 country code
      (EU Weekly Oil Bulletin); ``petrol_median`` is the fallback for a country the
      bulletin does not cover.
    """

    eur_per_cost_index_day: float
    petrol_by_country: Mapping[str, float]
    petrol_median: float

    @classmethod
    def from_data_dir(cls, data_dir: Path | None = None) -> ReferenceData:
        """Build the reference figures from the committed local data files.

        Reads only local files (no network). Raises ``FileNotFoundError`` if a
        required real source is absent rather than substituting a fabricated value.
        """
        base = data_dir or _DATA_DIR
        wherenext = json.loads((base / _WHERENEXT_FILE).read_text(encoding="utf-8"))
        ecb = json.loads((base / _ECB_FILE).read_text(encoding="utf-8"))
        fuel = json.loads((base / _FUEL_FILE).read_text(encoding="utf-8"))

        usd_per_eur = float(ecb["rates"]["USD"])
        eur_per_usd = 1.0 / usd_per_eur
        # EUR/day per index point = (monthly_usd / 30 * eur_per_usd) / cost_index,
        # taken as the median over every WhereNext country (a robust central value).
        ratios = [
            (row["monthly_estimate_usd"] / 30.0 * eur_per_usd) / row["cost_index"]
            for row in wherenext["data"]
            if row.get("cost_index")
        ]
        anchor = statistics.median(ratios)

        petrol: dict[str, float] = {}
        for country in fuel["data"]["countries"]:
            sp95 = (country.get("fuels") or {}).get("sp95")
            if sp95 and sp95.get("avg") is not None:
                petrol[country["country_code"]] = float(sp95["avg"])
        petrol_median = statistics.median(petrol.values()) if petrol else 0.0

        logger.debug(
            "ScoutAgent reference data: eur_per_cost_index_day=%.4f petrol countries=%d median=%.3f",
            anchor,
            len(petrol),
            petrol_median,
        )
        return cls(
            eur_per_cost_index_day=anchor,
            petrol_by_country=petrol,
            petrol_median=petrol_median,
        )


@dataclass(frozen=True)
class ScoutRequest:
    """The user constraints the pre-filter needs (a subset of the engine input).

    Origin is carried as coordinates because the pre-filter's transport term is a
    great-circle distance from the origin; the pipeline resolves the user's origin
    label to a :class:`GeoPoint` before Scout runs. ``vehicle_consumption`` (L/100
    km) is the user's own figure when known; when ``None`` the policy default is
    used. Only ``car`` is an MVP mode; bus/train use coarse per-km coefficients.
    """

    budget: float
    duration_days: int
    travelers: int
    origin: GeoPoint
    transport_mode: TransportMode
    vehicle_consumption: float | None = None

    def __post_init__(self) -> None:
        if self.budget <= 0:
            raise ValueError(f"budget must be positive, got {self.budget!r}")
        if self.duration_days < 1:
            raise ValueError(f"duration_days must be >= 1, got {self.duration_days!r}")
        if self.travelers < 1:
            raise ValueError(f"travelers must be >= 1, got {self.travelers!r}")
        object.__setattr__(
            self,
            "transport_mode",
            (
                self.transport_mode
                if isinstance(self.transport_mode, TransportMode)
                else TransportMode(self.transport_mode)
            ),
        )


@dataclass(frozen=True)
class ScoutEstimate:
    """A candidate's coarse cost breakdown and the affordability verdict.

    ``living_eur`` is ``None`` when the destination has no ``cost_of_living`` value
    to estimate from; such a candidate is kept (never dropped on missing data) and
    flagged via :attr:`cost_known`. All monetary fields are indicative EUR.
    """

    destination: Destination
    distance_km: float
    transport_eur: float
    living_eur: float | None
    total_eur: float
    affordable: bool
    cost_known: bool


@dataclass(frozen=True)
class PrefilterResult:
    """Outcome of the pre-filter: survivors, drops, and every estimate.

    ``kept`` is the short list handed on to preference ranking (BS-055); both
    ``kept`` and ``dropped`` are deterministically ordered by estimated total cost
    then city/country so the output is stable.
    """

    kept: list[Destination]
    dropped: list[Destination]
    estimates: list[ScoutEstimate]


class ScoutAgent:
    """Coarse budget pre-filter over candidate destinations (technical-doc §8.3).

    Construct once (loading the real reference data) and reuse. ``reference`` and
    ``policy`` are injectable so the estimate is unit-testable with fixed figures
    and no file access.
    """

    def __init__(
        self,
        *,
        policy: PrefilterPolicy | None = None,
        reference: ReferenceData | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.policy = policy or PrefilterPolicy()
        self.reference = reference or ReferenceData.from_data_dir(data_dir)

    def _transport_eur(
        self, request: ScoutRequest, destination: Destination
    ) -> tuple[float, float]:
        """Return ``(round_trip_transport_eur, straight_line_distance_km)``.

        Car cost is fuel-only (real bulletin petrol price × the party's vehicles)
        and per-trip, not per-person; air/bus/train scale a per-km fare by the
        number of travellers. All are round-trip (out and back).
        """
        dest_point = GeoPoint(destination.latitude, destination.longitude)
        straight_km = haversine_km(request.origin, dest_point)
        mode = request.transport_mode

        if mode is TransportMode.CAR:
            road_km = straight_km * self.policy.road_detour_factor
            consumption = request.vehicle_consumption or self.policy.default_vehicle_consumption
            price = self.reference.petrol_by_country.get(
                destination.country_code or "", self.reference.petrol_median
            )
            vehicles = -(-request.travelers // _CAR_SEATS)  # ceil division
            litres = (2 * road_km / 100.0) * consumption * vehicles
            return litres * price, straight_km

        if mode is TransportMode.AIRPLANE:
            one_way = self.policy.air_base_eur + self.policy.air_per_km_eur * straight_km
        elif mode is TransportMode.BUS:
            one_way = self.policy.bus_per_km_eur * straight_km
        else:  # TRAIN
            one_way = self.policy.train_per_km_eur * straight_km
        return one_way * 2 * request.travelers, straight_km

    def estimate(self, request: ScoutRequest, destination: Destination) -> ScoutEstimate:
        """Coarse floor-cost estimate + affordability verdict for one candidate."""
        transport_eur, distance_km = self._transport_eur(request, destination)

        cost_index = destination.features.cost_of_living if destination.features else None
        if cost_index is None:
            # No cost-of-living data: cannot estimate on-the-ground spend, so do not
            # drop on a guess — keep it and let the specialist agents cost it.
            living_eur: float | None = None
            total_eur = transport_eur
            cost_known = False
        else:
            daily_person = float(cost_index) * self.reference.eur_per_cost_index_day
            living_eur = daily_person * request.duration_days * request.travelers
            total_eur = transport_eur + living_eur
            cost_known = True

        affordable = total_eur <= request.budget * self.policy.keep_margin
        return ScoutEstimate(
            destination=destination,
            distance_km=round(distance_km, 3),
            transport_eur=round(transport_eur, 2),
            living_eur=None if living_eur is None else round(living_eur, 2),
            total_eur=round(total_eur, 2),
            affordable=affordable,
            cost_known=cost_known,
        )

    def prefilter(
        self, request: ScoutRequest, candidates: Iterable[Destination]
    ) -> PrefilterResult:
        """Drop clearly-unaffordable candidates; return survivors + all estimates.

        Deterministic: estimates are sorted by total cost then city/country, and the
        kept/dropped lists preserve that order.
        """
        estimates = [self.estimate(request, dest) for dest in candidates]
        estimates.sort(key=lambda e: (e.total_eur, e.destination.city, e.destination.country))

        kept = [e.destination for e in estimates if e.affordable]
        dropped = [e.destination for e in estimates if not e.affordable]
        logger.info(
            "ScoutAgent pre-filter: %d candidates -> %d kept, %d dropped (budget=%.2f, %dd, %dpax, %s)",
            len(estimates),
            len(kept),
            len(dropped),
            request.budget,
            request.duration_days,
            request.travelers,
            request.transport_mode.value,
        )
        return PrefilterResult(kept=kept, dropped=dropped, estimates=estimates)


__all__ = [
    "PrefilterPolicy",
    "PrefilterResult",
    "ReferenceData",
    "ScoutAgent",
    "ScoutEstimate",
    "ScoutRequest",
]
