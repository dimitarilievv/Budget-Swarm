"""Structured, reusable ingestion validation (BS-036).

The shared validation layer every ingestion task uses to turn a raw record into a
typed, checked one — schema, type, range, required-field and duplicate detection —
returning **structured results** (not just booleans) and logging rejects, so a bad
row is reported with a reason rather than silently dropped or fatally raised.

It builds on the low-level, pure helpers in ``scripts.validators``: those parse a
single value, this composes them into per-field specs, per-row results, and
whole-dataset checks. Consumers declare a list of :class:`FieldSpec` for their
schema and call :func:`validate_row`; :func:`check_schema` guards required
columns; :func:`detect_duplicates` finds repeated keys.

Used by the curated-destinations ingest (BS-030), the accommodation CSV import
(BS-033), and the accommodation cleaner (BS-034).
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field

from scripts import validators as v

logger = logging.getLogger(__name__)

# Value parsers per declared field kind. 'str' trims to a value or None; the
# numeric kinds return None on unparseable input; 'iso2' yields an alpha-2 code.
_PARSERS = {
    "str": lambda raw: v.clean_str(raw) or None,
    "int": v.parse_int,
    "float": v.parse_float,
    "money": v.parse_money,
    "iso2": v.parse_iso_alpha2,
}


@dataclass(frozen=True)
class Issue:
    """One problem found with a field. ``code`` is a stable machine label."""

    field: str
    code: str  # 'missing' | 'type' | 'range' | 'choice' | 'schema' | 'duplicate'
    message: str


@dataclass
class RowResult:
    """The outcome of validating one row: cleaned values + any issues."""

    ok: bool
    values: dict[str, object] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """A single human-readable reason line joining all issues (for logging)."""
        return "; ".join(f"{i.field}: {i.message}" for i in self.issues)


@dataclass(frozen=True)
class FieldSpec:
    """Declares one expected field: its kind, whether required, and its bounds."""

    name: str
    required: bool = False
    kind: str = "str"  # one of _PARSERS
    min: float | None = None
    max: float | None = None
    choices: frozenset[str] | None = None


def check_schema(fieldnames: Sequence[str] | None, required_columns: Iterable[str]) -> list[Issue]:
    """Return an ``Issue`` per required column missing from ``fieldnames``."""
    present = set(fieldnames or [])
    return [
        Issue(col, "schema", f"missing required column {col!r}")
        for col in sorted(set(required_columns) - present)
    ]


def validate_row(row: dict[str, str], specs: Sequence[FieldSpec]) -> RowResult:
    """Validate one row against ``specs``, collecting cleaned values + issues.

    For each spec: parse by ``kind``; a required field that is absent/blank is a
    ``missing`` issue; a non-blank value that fails to parse is a ``type`` issue;
    an out-of-``[min,max]`` numeric value is a ``range`` issue; a value not in
    ``choices`` is a ``choice`` issue. Optional blank fields become ``None`` with
    no issue.
    """
    values: dict[str, object] = {}
    issues: list[Issue] = []
    for spec in specs:
        raw = row.get(spec.name)
        parser = _PARSERS.get(spec.kind, _PARSERS["str"])
        value = parser(raw)
        values[spec.name] = value

        if value is None:
            if v.non_empty(raw):
                # Present but unparseable for its kind (e.g. "n/a" as a float,
                # "PRT" as an ISO-2 code) — a type problem, whether required or not.
                issues.append(Issue(spec.name, "type", f"not a valid {spec.kind}: {raw!r}"))
            elif spec.required:
                issues.append(Issue(spec.name, "missing", "required value is missing"))
            continue

        if spec.kind in ("int", "float", "money") and (
            spec.min is not None or spec.max is not None
        ):
            low = spec.min if spec.min is not None else float("-inf")
            high = spec.max if spec.max is not None else float("inf")
            if not v.in_range(value, low, high):  # type: ignore[arg-type]
                issues.append(Issue(spec.name, "range", f"{value} out of range [{low}, {high}]"))

        if spec.choices is not None and str(value) not in spec.choices:
            issues.append(Issue(spec.name, "choice", f"{value!r} not in allowed set"))

    return RowResult(ok=not issues, values=values, issues=issues)


def detect_duplicates(keys: Iterable[Hashable]) -> dict[Hashable, int]:
    """Return ``{key: count}`` for keys that appear more than once."""
    counts = Counter(keys)
    return {key: n for key, n in counts.items() if n > 1}


def log_rejects(context: str, rejects: Iterable[tuple[int, str]]) -> None:
    """Log each ``(line_number, reason)`` reject under a shared ``context`` label."""
    for line_no, reason in rejects:
        logger.warning("%s: skipping line %s: %s", context, line_no, reason)
