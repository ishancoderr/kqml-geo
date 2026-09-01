from __future__ import annotations

import threading
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional, Type, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Enums ─────────────────────────────────────────────────────────────────────

class Performative(str, Enum):
    ASK = "ask"
    TELL = "tell"

class Language(str, Enum):
    GEOSQL = "GeoSQL"
    GEOKQML = "GeoKQML"

class Encoding(str, Enum):
    JSON = "JSON"

class Ontology(str, Enum):
    GERMAN_GEOSTATS_V1 = "German-Geostats-v1"
    GEO_MISSINGNESS_V2 = "geo-missingness-v2"

class MissingnessType(str, Enum):
    ATTRIBUTE = "attribute"
    TEMPORAL = "temporal"
    SPATIAL = "spatial"
    MIXED = "mixed"

class EntityType(str, Enum):
    CITY = "city"
    STATE = "state"

class GeometryMissMode(str, Enum):
    UNKNOWN_FEATURE = "unknown-feature"   # no row at all
    GEOMETRY_NULL = "geometry-null"       # row exists, shape column empty

class SpatialOperation(str, Enum):
    UNION = "Union"
    INTERSECTION = "Intersection"
    DIFFERENCE = "Difference"
    SYM_DIFFERENCE = "SymDifference"

class SpatialRelationship(str, Enum):
    TOUCHES = "touches"
    NORTH_OF = "north_of"
    WITHIN_DISTANCE = "within_distance"

class TernaryResult(int, Enum):
    """Three-valued relationship outcome: a null input must not collapse into False."""
    UNKNOWN = -1
    FALSE = 0
    TRUE = 1


# ── Data models ───────────────────────────────────────────────────────────────

class DataRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    year: int
    spatial: Optional[str] = None
    population: Optional[int] = None
    married: Optional[int] = None
    live_births: Optional[int] = None

    def get(self, key: str, default: Any = None) -> Any:
        known = ("year", "spatial", "population", "married", "live_births")
        if key in known:
            return getattr(self, key, default)
        return (self.model_extra or {}).get(key, default)

    def to_flat_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"year": self.year}
        if self.spatial is not None:
            d["spatial"] = self.spatial
        for attr in ("population", "married", "live_births"):
            val = getattr(self, attr)
            if val is not None:
                d[attr] = val
        if self.model_extra:
            d.update(self.model_extra)
        return d


class MissingSlot(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    spatial: Union[str, List[str]]
    temporal: List[int]
    attributes: List[str]
    missingness_type: Optional[MissingnessType] = None

    @property
    def spatial_list(self) -> List[str]:
        return [self.spatial] if isinstance(self.spatial, str) else list(self.spatial)


class FoundSlot(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    spatial: Union[str, List[str]]
    temporal: List[int]
    attributes: List[str]
    data: List[DataRecord] = Field(default_factory=list)

    @property
    def spatial_list(self) -> List[str]:
        return [self.spatial] if isinstance(self.spatial, str) else list(self.spatial)


class MissingGeometrySlot(BaseModel):
    spatial_entity: str
    entity_type: EntityType
    miss_mode: Optional[GeometryMissMode] = None  # local diagnosis only, never required on the wire


class FoundGeometrySlot(BaseModel):
    spatial_entity: str
    entity_type: EntityType
    geometry: str   # WKT e.g. "POINT(11.58 48.14)"
    srid: int = 4326


class SpatialQuery(BaseModel):
    """The :spatial-query content of an `ask` message (Scenario 21): a shape is sent
    because the targets that satisfy it cannot be named in advance."""
    model_config = ConfigDict(populate_by_name=True)

    topic: str                       # e.g. "Within"
    geometry: str                    # WKT of the constructed shape, e.g. a buffer zone
    srid: int = 4326
    target_entity: EntityType = Field(alias="target-entity")
    exclude: List[str] = Field(default_factory=list)


class KQMLContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    missing_slots: List[MissingSlot] = Field(default_factory=list, alias="missing-slots")
    found_slots: List[FoundSlot] = Field(default_factory=list, alias="found-slots")
    missing_geometries: List[MissingGeometrySlot] = Field(default_factory=list, alias="missing-geometries")
    found_geometries: List[FoundGeometrySlot] = Field(default_factory=list, alias="found-geometries")
    spatial_query: Optional[SpatialQuery] = Field(default=None, alias="spatial-query")

    @property
    def status(self) -> str:
        has_found = bool(self.found_slots)
        has_missing = bool(self.missing_slots)
        if has_found and not has_missing:
            return "complete"
        if has_found and has_missing:
            return "partial-complete"
        if not has_found and has_missing:
            return "not-found"
        return "empty"


# ── Metadata ─────────────────────────────────────────────────────────────────

class MessageMetadata(BaseModel):
    token_usage: int


# ── Messages ──────────────────────────────────────────────────────────────────

class KQMLMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    performative: Performative
    sender: str
    receiver: str
    language: Language = Language.GEOKQML
    encoding: Encoding = Encoding.JSON
    ontology: Ontology = Ontology.GEO_MISSINGNESS_V2
    content: KQMLContent = Field(default_factory=KQMLContent)
    reply_with: Optional[str] = None
    in_reply_to: Optional[str] = None
    metadata: Optional[MessageMetadata] = None


class AskMessage(KQMLMessage):
    performative: Performative = Performative.ASK
    reply_with: str = Field(...)

    @model_validator(mode="after")
    def _require_content(self) -> AskMessage:
        # Scenario 21 sends a :spatial-query instead of a named missing-slot/geometry,
        # when the targets that would satisfy the query cannot be named in advance.
        if (
            not self.content.missing_slots
            and not self.content.missing_geometries
            and self.content.spatial_query is None
        ):
            raise ValueError(
                "AskMessage must have at least one missing-slot, missing-geometry, or spatial-query"
            )
        return self


class TellMessage(KQMLMessage):
    performative: Performative = Performative.TELL
    in_reply_to: str = Field(...)


# ── ID generation ─────────────────────────────────────────────────────────────

_counter_lock = threading.Lock()
_counter: int = 0


def generate_request_id(prefix: str = "req") -> str:
    global _counter
    with _counter_lock:
        _counter += 1
        return f"{prefix}-{_counter:03d}"


def generate_uuid_id(prefix: str = "req") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── Factory ───────────────────────────────────────────────────────────────────

class MessageFactory:

    @staticmethod
    def ask(
        sender: str,
        receiver: str,
        missing_slots: Optional[List[MissingSlot]] = None,
        *,
        missing_geometries: Optional[List[MissingGeometrySlot]] = None,
        reply_with: Optional[str] = None,
        language: Language = Language.GEOKQML,
        ontology: Ontology = Ontology.GEO_MISSINGNESS_V2,
    ) -> AskMessage:
        return AskMessage(
            sender=sender,
            receiver=receiver,
            reply_with=reply_with or generate_request_id(),
            language=language,
            ontology=ontology,
            content=KQMLContent(
                missing_slots=missing_slots or [],
                missing_geometries=missing_geometries or [],
            ),
        )

    @staticmethod
    def tell(
        sender: str,
        receiver: str,
        in_reply_to: str,
        *,
        found_slots: Optional[List[FoundSlot]] = None,
        missing_slots: Optional[List[MissingSlot]] = None,
        found_geometries: Optional[List[FoundGeometrySlot]] = None,
        missing_geometries: Optional[List[MissingGeometrySlot]] = None,
        language: Language = Language.GEOKQML,
        ontology: Ontology = Ontology.GEO_MISSINGNESS_V2,
        metadata: Optional[MessageMetadata] = None,
    ) -> TellMessage:
        return TellMessage(
            sender=sender,
            receiver=receiver,
            in_reply_to=in_reply_to,
            language=language,
            ontology=ontology,
            content=KQMLContent(
                found_slots=found_slots or [],
                missing_slots=missing_slots or [],
                found_geometries=found_geometries or [],
                missing_geometries=missing_geometries or [],
            ),
            metadata=metadata,
        )

    @staticmethod
    def ask_spatial_query(
        sender: str,
        receiver: str,
        spatial_query: SpatialQuery,
        *,
        reply_with: Optional[str] = None,
        language: Language = Language.GEOKQML,
        ontology: Ontology = Ontology.GEO_MISSINGNESS_V2,
    ) -> AskMessage:
        """Scenario 21: ask the peer to test its own catalogue against a constructed
        shape, because the targets that satisfy it cannot be named in advance."""
        return AskMessage(
            sender=sender,
            receiver=receiver,
            reply_with=reply_with or generate_request_id(),
            language=language,
            ontology=ontology,
            content=KQMLContent(spatial_query=spatial_query),
        )

    @staticmethod
    def spatial_query(
        topic: str,
        geometry: str,
        target_entity: EntityType,
        *,
        srid: int = 4326,
        exclude: Optional[List[str]] = None,
    ) -> SpatialQuery:
        return SpatialQuery(
            topic=topic, geometry=geometry, srid=srid,
            target_entity=target_entity, exclude=exclude or [],
        )

    @staticmethod
    def missing_slot(
        spatial: Union[str, List[str]],
        temporal: List[int],
        attributes: List[str],
        missingness_type: Optional[MissingnessType] = None,
    ) -> MissingSlot:
        return MissingSlot(spatial=spatial, temporal=temporal, attributes=attributes, missingness_type=missingness_type)

    @staticmethod
    def found_slot(
        spatial: Union[str, List[str]],
        temporal: List[int],
        attributes: List[str],
        data: List[Dict[str, Any]],
    ) -> FoundSlot:
        return FoundSlot(spatial=spatial, temporal=temporal, attributes=attributes, data=[DataRecord(**r) for r in data])

    @staticmethod
    def data_record(year: int, spatial: Optional[str] = None, **attrs: Any) -> DataRecord:
        return DataRecord(year=year, spatial=spatial, **attrs)

    @staticmethod
    def missing_geometry_slot(
        spatial_entity: str,
        entity_type: EntityType,
        miss_mode: Optional[GeometryMissMode] = None,
    ) -> MissingGeometrySlot:
        return MissingGeometrySlot(spatial_entity=spatial_entity, entity_type=entity_type, miss_mode=miss_mode)

    @staticmethod
    def found_geometry_slot(spatial_entity: str, entity_type: EntityType, geometry: str, srid: int = 4326) -> FoundGeometrySlot:
        return FoundGeometrySlot(spatial_entity=spatial_entity, entity_type=entity_type, geometry=geometry, srid=srid)


# ── Spatial operation / relationship helpers ────────────────────────────────
# Both agents run the same code, so an operation or relationship is never itself
# missing (Section 3.5) — only an input shape can be. These helpers assume the
# two shapes are already present locally.

def check_srid_agreement(local_srid: int, received_srid: int) -> None:
    """An operation on shapes held in different reference systems is an error.
    Checked before the operation runs, per Section 3, rather than after it fails."""
    if local_srid != received_srid:
        raise ValueError(
            f"SRID mismatch: local geometry is {local_srid}, received geometry is {received_srid}"
        )


def gap_signature(spatial: bool, temporal: bool, thematic: bool) -> tuple[int, int, int]:
    """The (S, T, A) flag pattern of Section 1.5, one flag per dimension."""
    return (int(spatial), int(temporal), int(thematic))


# ── Registry ──────────────────────────────────────────────────────────────────

class PerformativeRegistry:
    _registry: Dict[str, Type[KQMLMessage]] = {}
    _bootstrapped: bool = False

    @classmethod
    def register(cls, performative: str, message_class: Type[KQMLMessage]) -> None:
        cls._registry[performative] = message_class

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> KQMLMessage:
        if not cls._bootstrapped:
            cls._registry.update({"ask": AskMessage, "tell": TellMessage})
            cls._bootstrapped = True

        perf = data.get("performative", "")
        msg_class = cls._registry.get(perf)
        content = cls._parse_content(data.get("content", {}))
        common = dict(
            sender=data.get("sender", ""),
            receiver=data.get("receiver", ""),
            language=data.get("language", "GeoKQML"),
            encoding=data.get("encoding", "JSON"),
            ontology=data.get("ontology", "geo-missingness-v2"),
            content=content,
            reply_with=data.get("reply_with"),
            in_reply_to=data.get("in_reply_to"),
            metadata=MessageMetadata(**data["metadata"]) if data.get("metadata") else None,
        )
        if msg_class is None:
            return KQMLMessage.model_construct(performative=perf, **common)
        return msg_class(performative=perf, **common)

    @classmethod
    def _parse_content(cls, raw: Dict[str, Any]) -> KQMLContent:
        missing = [MissingSlot(**s) for s in raw.get("missing_slots", [])]
        found = [
            FoundSlot(
                spatial=s["spatial"], temporal=s["temporal"], attributes=s["attributes"],
                data=[DataRecord(**r) for r in s.get("data", [])],
            )
            for s in raw.get("found_slots", [])
        ]
        missing_geom = [MissingGeometrySlot(**s) for s in raw.get("missing_geometries", [])]
        found_geom = [FoundGeometrySlot(**s) for s in raw.get("found_geometries", [])]
        spatial_query_raw = raw.get("spatial_query") or raw.get("spatial-query")
        spatial_query = SpatialQuery(**spatial_query_raw) if spatial_query_raw else None
        return KQMLContent(
            missing_slots=missing,
            found_slots=found,
            missing_geometries=missing_geom,
            found_geometries=found_geom,
            spatial_query=spatial_query,
        )
