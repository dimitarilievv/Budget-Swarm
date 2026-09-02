"""ScoutAgent — coarse budget pre-filter + preference/season ranking
(BS-054/BS-055; technical-doc §8.3).

Scout is the funnel that narrows "anywhere" to a short list *before* the expensive
per-destination agents run. This module implements the first two of its three
stages:

1. the **coarse budget pre-filter** (:meth:`ScoutAgent.prefilter`, BS-054) drops
   destinations that clearly cannot fit inside the user's budget for the given
   duration and party size;
2. **preference + season ranking** (:meth:`ScoutAgent.rank`, BS-055) orders the
   survivors by how well their feature scores match the user's preference vector,
   modulated by each destination's seasonal suitability for the travel window;
3. **top-k selection + output contract** (:meth:`ScoutAgent.select`, BS-056) runs
   the two stages end to end and emits the short list the pipeline consumes.

:meth:`ScoutAgent.select` is Scout's public entry point (§8.3 step 3): it returns
a configurable top-k ``list[ai_engine.domain.Destination]`` — the domain contract
the coordinator, scorer (BS-069) and explanation (BS-071) build on — ordered best
first, each destination carrying its normalized ``features`` so downstream stages
have the context they need without re-querying.

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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ai_engine.domain import Destination, DestinationFeatures, TransportMode
from ai_engine.providers.flight import FARE_BASE_EUR, FARE_PER_KM_EUR
from ai_engine.providers.route import ROAD_DETOUR_FACTOR
from ai_engine.utils.geo import GeoPoint, haversine_km

logger = logging.getLogger(__name__)

# Repository ``data/`` directory (…/ai_engine/agents/scout_agent.py -> repo root).
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_WHERENEXT_FILE = "wherenext_cost_of_living.json"
_ECB_FILE = "ecb_eur_rates.json"
_FUEL_FILE = "fuel_prices_live_summary.json"
_SEASON_FILE = "season_profiles.json"

# A single vehicle serves the whole party by car (its fuel cost is per-trip, not
# per-person); above this many travellers a second car is assumed.
_CAR_SEATS = 4

# Default size of the candidate short list handed to the parallel agents
# (technical-doc §8.3 step 3: "keep the top k, e.g. 4-6"). Configurable per call.
DEFAULT_TOP_K = 6

# Maps a preference key (as sent in the request's ``preferences`` map, api.md) to
# the ``DestinationFeatures`` column it scores against. Keys not listed here are
# ignored during ranking (logged), so an unknown preference never raises;
# ``sightseeing`` is treated as a synonym of general touristic appeal (api.md uses
# it in its example alongside the direct feature keys).
PREFERENCE_FEATURE_MAP: Mapping[str, str] = {
    "culture": "culture_score",
    "nature": "nature_score",
    "nightlife": "nightlife_score",
    "food": "food_score",
    "tourism": "tourism_score",
    "sightseeing": "tourism_score",
}


def _load_season_profiles(path: Path) -> dict[tuple[str, str], tuple[float, ...]]:
    """Load per-city 12-month suitability curves from the Open-Meteo snapshot.

    Returns ``{(city, country): (12 floats)}``; an empty mapping when the file is
    absent or unreadable so ranking degrades to the stored ``season_score`` rather
    than failing. Deliberately a small engine-local reader (mirroring the backend
    ingestion helper) so the engine keeps its no-``backend``-import boundary.
    """
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("ScoutAgent: could not read season profiles at %s", path)
        return {}
    profiles: dict[tuple[str, str], tuple[float, ...]] = {}
    for entry in payload.get("data", []) if isinstance(payload, dict) else []:
        suitability = entry.get("suitability") if isinstance(entry, dict) else None
        if isinstance(suitability, list) and len(suitability) == 12:
            profiles[(entry["city"], entry["country"])] = tuple(float(x) for x in suitability)
    return profiles


def _mean_suitability(curve: Sequence[float], months: Sequence[int]) -> float:
    """Mean travel suitability over ``months`` (each 1-12) of a 12-month ``curve``.

    Same semantics as the ingestion-side season model: a summer window and a winter
    window over the same curve yield different scores — this is what makes the
    ranking's season term depend on the travel window.
    """
    return sum(curve[m - 1] for m in months) / len(months)


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
class RankingPolicy:
    """Tunable parameters of the preference/season ranking (technical-doc §8.3 step 2).

    ``season_weight`` controls how strongly seasonal suitability modulates the
    preference match: the overall score is
    ``preference_match * (1 - season_weight + season_weight * season_score)``. At
    ``1.0`` (the default) season is fully multiplicative — a destination out of
    season (low ``season_score``) is pushed down regardless of feature fit, which
    is the doc's "beach destination in winter ranks lower" behaviour; at ``0.0``
    season is ignored. A destination with no season data is treated as neutral (no
    seasonal penalty or boost).
    """

    season_weight: float = 1.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.season_weight <= 1.0:
            raise ValueError(f"season_weight must be within [0, 1], got {self.season_weight!r}")


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
    * ``season_profiles`` — per-``(city, country)`` 12-month travel-suitability
      curve (Open-Meteo ERA5 normals), used to recompute a window-specific
      ``season_score`` during ranking. Empty when the snapshot is unavailable, in
      which case ranking falls back to each destination's stored ``season_score``.
    """

    eur_per_cost_index_day: float
    petrol_by_country: Mapping[str, float]
    petrol_median: float
    season_profiles: Mapping[tuple[str, str], tuple[float, ...]] = field(default_factory=dict)

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

        season_profiles = _load_season_profiles(base / _SEASON_FILE)

        logger.debug(
            "ScoutAgent reference data: eur_per_cost_index_day=%.4f petrol countries=%d "
            "median=%.3f season profiles=%d",
            anchor,
            len(petrol),
            petrol_median,
            len(season_profiles),
        )
        return cls(
            eur_per_cost_index_day=anchor,
            petrol_by_country=petrol,
            petrol_median=petrol_median,
            season_profiles=season_profiles,
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


@dataclass(frozen=True)
class RankedDestination:
    """A ranked candidate with the components behind its position.

    ``preference_match`` is the weighted mean (in ``[0, 1]``) of the destination's
    feature scores against the request's preference vector; ``season_score`` is the
    seasonal suitability applied (window-specific when a curve and travel window are
    available, else the destination's stored value, or ``None`` when unknown);
    ``overall_score`` is the value the list is sorted by.
    """

    destination: Destination
    preference_match: float
    season_score: float | None
    overall_score: float


class ScoutAgent:
    """Scout's coarse pre-filter + preference/season ranking (technical-doc §8.3).

    Construct once (loading the real reference data) and reuse. ``reference``,
    ``policy`` and ``ranking_policy`` are injectable so the estimate and the ranking
    are unit-testable with fixed figures and no file access.
    """

    def __init__(
        self,
        *,
        policy: PrefilterPolicy | None = None,
        ranking_policy: RankingPolicy | None = None,
        reference: ReferenceData | None = None,
        data_dir: Path | None = None,
    ) -> None:
        self.policy = policy or PrefilterPolicy()
        self.ranking_policy = ranking_policy or RankingPolicy()
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

    @staticmethod
    def _preference_match(
        features: DestinationFeatures | None, preferences: Mapping[str, float]
    ) -> float:
        """Weighted mean of a destination's feature scores against ``preferences``.

        Only preference keys that map to a known feature column with a non-``None``
        value contribute (both to the weighted sum and its denominator), so a
        destination is never penalised for a feature it happens to lack. Returns a
        value in ``[0, 1]``; ``0.0`` when no preference is usable (logged).
        """
        if features is None:
            return 0.0
        weighted_sum = 0.0
        total_weight = 0.0
        for key, weight in preferences.items():
            attr = PREFERENCE_FEATURE_MAP.get(key)
            if attr is None:
                logger.debug("ScoutAgent ranking: unknown preference key %r ignored", key)
                continue
            value = getattr(features, attr)
            if value is None:
                continue
            weighted_sum += weight * float(value)
            total_weight += weight
        if total_weight <= 0.0:
            return 0.0
        return weighted_sum / total_weight

    def _season_for(
        self, destination: Destination, travel_months: Sequence[int] | None
    ) -> float | None:
        """The seasonal suitability to apply for this destination.

        When a travel window is given and the destination has an ingested Open-Meteo
        curve, recompute the score for exactly those months (window-specific).
        Otherwise fall back to the destination's stored ``season_score``; ``None``
        when neither is available (ranking then treats season as neutral).
        """
        if travel_months:
            curve = self.reference.season_profiles.get((destination.city, destination.country))
            if curve is not None:
                return _mean_suitability(curve, travel_months)
        if destination.features is not None:
            return destination.features.season_score
        return None

    def rank(
        self,
        preferences: Mapping[str, float],
        candidates: Iterable[Destination],
        *,
        travel_months: Sequence[int] | None = None,
        top_k: int | None = None,
    ) -> list[RankedDestination]:
        """Rank candidates by preference match, modulated by seasonal suitability.

        The overall score is
        ``preference_match * (1 - w + w * season_score)`` (``w`` =
        ``ranking_policy.season_weight``); a destination with no season data is
        scored on preference match alone. The result is sorted by overall score
        descending, ties broken by city then country, so the ranking is stable and
        reproducible. ``top_k`` (when given) keeps only the leading candidates
        (§8.3 step 3, for BS-056). ``travel_months`` (1-12) selects the season
        window; when omitted each destination's stored ``season_score`` is used.
        """
        if travel_months is not None:
            months = tuple(travel_months)
            if not months or any(not 1 <= m <= 12 for m in months):
                raise ValueError(f"travel_months must be integers in 1..12, got {travel_months!r}")
        if not preferences:
            logger.warning("ScoutAgent ranking: empty preference vector — ranking on season only")

        w = self.ranking_policy.season_weight
        ranked: list[RankedDestination] = []
        for dest in candidates:
            match = self._preference_match(dest.features, preferences)
            season = self._season_for(dest, travel_months)
            factor = 1.0 if season is None else (1.0 - w + w * season)
            ranked.append(
                RankedDestination(
                    destination=dest,
                    preference_match=round(match, 6),
                    season_score=None if season is None else round(season, 6),
                    overall_score=round(match * factor, 6),
                )
            )

        ranked.sort(key=lambda r: (-r.overall_score, r.destination.city, r.destination.country))
        if top_k is not None:
            if top_k < 0:
                raise ValueError(f"top_k must be >= 0, got {top_k!r}")
            ranked = ranked[:top_k]
        logger.info(
            "ScoutAgent ranking: %d candidates ranked%s (season_weight=%.2f, window=%s)",
            len(ranked),
            f", top {top_k} kept" if top_k is not None else "",
            w,
            tuple(travel_months) if travel_months else "stored",
        )
        return ranked

    def select(
        self,
        request: ScoutRequest,
        preferences: Mapping[str, float],
        candidates: Iterable[Destination],
        *,
        travel_months: Sequence[int] | None = None,
        k: int = DEFAULT_TOP_K,
    ) -> list[Destination]:
        """Run Scout end to end and return the top-``k`` candidate short list.

        This is Scout's public output contract (technical-doc §8.3): it applies the
        coarse budget pre-filter (BS-054), ranks the survivors by preference +
        season (BS-055), and returns the leading ``k`` as ``Destination`` domain
        models — ordered best first, each still carrying its ``features`` so the
        coordinator, scorer and explanation stages have the context they need.

        ``k`` is configurable (default :data:`DEFAULT_TOP_K`); fewer survivors than
        ``k`` simply yields a shorter list. Raises ``ValueError`` for ``k < 1``.
        """
        if k < 1:
            raise ValueError(f"k must be >= 1, got {k!r}")

        survivors = self.prefilter(request, candidates).kept
        ranked = self.rank(preferences, survivors, travel_months=travel_months, top_k=k)
        shortlist = [r.destination for r in ranked]

        missing_features = [d.city for d in shortlist if d.features is None]
        if missing_features:
            # Selected but lacking feature context the downstream stages rely on.
            logger.warning(
                "ScoutAgent select: %d selected destination(s) have no features: %s",
                len(missing_features),
                ", ".join(missing_features),
            )
        logger.info(
            "ScoutAgent select: %d affordable survivor(s) -> short list of %d (k=%d)",
            len(survivors),
            len(shortlist),
            k,
        )
        return shortlist


__all__ = [
    "DEFAULT_TOP_K",
    "PREFERENCE_FEATURE_MAP",
    "PrefilterPolicy",
    "PrefilterResult",
    "RankedDestination",
    "RankingPolicy",
    "ReferenceData",
    "ScoutAgent",
    "ScoutEstimate",
    "ScoutRequest",
]
