"""Shared vacancy data models used by every source adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(slots=True)
class JobRecord:
    """Canonical vacancy record produced by a collector.

    Values are kept as strings because source pages often expose incomplete,
    inconsistent, or non-normalized values. Normalization and classification
    belong to later pipeline stages.
    """

    job_id: str = ""
    source: str = ""
    date_collected: str = ""
    date_published: str = ""
    title: str = ""
    company: str = ""
    city_region: str = ""
    city: str = ""
    region: str = ""
    country: str = ""
    work_format: str = ""
    source_work_format: str = ""
    contract_type: str = ""
    salary: str = ""
    salary_usd_equivalent: str = ""
    url: str = ""
    full_text: str = ""
    status: str = ""
    availability_status: str = "unknown"
    note: str = ""
    search_query: str = ""
    job_category: str = "other"
    role_family: str = ""
    role_subcategory: str = ""
    seniority: str = ""
    years_experience_min: str = ""
    years_experience_max: str = ""
    skills_all: str = ""
    required_skills: str = ""
    preferred_skills: str = ""
    analysis_note: str = ""
    fit_score: str = ""
    fit_reasoning: str = ""
    duplicate_group: str = ""

    def to_dict(self) -> dict[str, str]:
        """Return the record in the canonical export schema."""
        return asdict(self)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "JobRecord":
        """Build a record from a mapping while ignoring unknown source fields."""
        known_fields = {field for field in cls.__dataclass_fields__}

        def as_text(value: Any) -> str:
            if value is None:
                return ""
            try:
                if value != value:  # NaN values loaded from spreadsheet cells.
                    return ""
            except (TypeError, ValueError):
                pass
            return str(value)

        normalized = {
            field: as_text(value)
            for field, value in values.items()
            if field in known_fields
        }
        return cls(**normalized)


JOB_RECORD_COLUMNS: tuple[str, ...] = tuple(JobRecord.__dataclass_fields__)
