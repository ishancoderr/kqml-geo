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

class Ontology(str, Enum):
    GERMAN_GEOSTATS_V1 = "German-Geostats-v1"

class MissingnessType(str, Enum):
    ATTRIBUTE = "attribute"
    TEMPORAL = "temporal"
    SPATIAL = "spatial"
    MIXED = "mixed"


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


class KQMLContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    missing_slots: List[MissingSlot] = Field(default_factory=list, alias="missing-slots")
    found_slots: List[FoundSlot] = Field(default_factory=list, alias="found-slots")

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


# ── Messages ──────────────────────────────────────────────────────────────────

class KQMLMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    performative: Performative
    sender: str
    receiver: str
    language: Language = Language.GEOSQL
    ontology: Ontology = Ontology.GERMAN_GEOSTATS_V1
    content: KQMLContent = Field(default_factory=KQMLContent)
    reply_with: Optional[str] = None
    in_reply_to: Optional[str] = None


class AskMessage(KQMLMessage):
    performative: Performative = Performative.ASK
    reply_with: str = Field(...)

    @model_validator(mode="after")
    def _require_missing_slots(self) -> AskMessage:
        if not self.content.missing_slots:
            raise ValueError("AskMessage must have at least one missing-slot")
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
        missing_slots: List[MissingSlot],
        *,
        reply_with: Optional[str] = None,
        language: Language = Language.GEOSQL,
        ontology: Ontology = Ontology.GERMAN_GEOSTATS_V1,
    ) -> AskMessage:
        return AskMessage(
            sender=sender,
            receiver=receiver,
            reply_with=reply_with or generate_request_id(),
            language=language,
            ontology=ontology,
            content=KQMLContent(missing_slots=missing_slots),
        )

    @staticmethod
    def tell(
        sender: str,
        receiver: str,
        in_reply_to: str,
        *,
        found_slots: Optional[List[FoundSlot]] = None,
        missing_slots: Optional[List[MissingSlot]] = None,
        language: Language = Language.GEOSQL,
        ontology: Ontology = Ontology.GERMAN_GEOSTATS_V1,
    ) -> TellMessage:
        return TellMessage(
            sender=sender,
            receiver=receiver,
            in_reply_to=in_reply_to,
            language=language,
            ontology=ontology,
            content=KQMLContent(found_slots=found_slots or [], missing_slots=missing_slots or []),
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
            language=data.get("language", "GeoSQL"),
            ontology=data.get("ontology", "German-Geostats-v1"),
            content=content,
            reply_with=data.get("reply_with"),
            in_reply_to=data.get("in_reply_to"),
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
        return KQMLContent(missing_slots=missing, found_slots=found)
