from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Union

from .core import DataRecord, FoundSlot, KQMLContent, KQMLMessage, MissingSlot, PerformativeRegistry


# ── JSON ──────────────────────────────────────────────────────────────────────

class JSONSerializer:

    @staticmethod
    def to_json(message: KQMLMessage, indent: int = 2) -> str:
        return json.dumps(JSONSerializer.to_dict(message), indent=indent, ensure_ascii=False)

    @staticmethod
    def to_dict(message: KQMLMessage) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "performative": message.performative.value,
            "sender": message.sender,
            "receiver": message.receiver,
            "language": message.language.value,
            "ontology": message.ontology.value,
        }
        if message.reply_with is not None:
            d["reply_with"] = message.reply_with
        if message.in_reply_to is not None:
            d["in_reply_to"] = message.in_reply_to
        if message.metadata is not None:
            d["metadata"] = message.metadata.model_dump()
        d["content"] = {
            "missing_slots": [
                {"spatial": s.spatial, "temporal": s.temporal, "attributes": s.attributes}
                for s in message.content.missing_slots
            ],
            "found_slots": [
                {
                    "spatial": s.spatial,
                    "temporal": s.temporal,
                    "attributes": s.attributes,
                    "data": [r.to_flat_dict() for r in s.data],
                }
                for s in message.content.found_slots
            ],
        }
        return d

    @staticmethod
    def from_json(json_str: str) -> KQMLMessage:
        return PerformativeRegistry.from_dict(json.loads(json_str))

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> KQMLMessage:
        return PerformativeRegistry.from_dict(data)


# ── KQML S-expression ─────────────────────────────────────────────────────────

class KQMLTextSerializer:

    _PAD = " "

    @classmethod
    def to_kqml(cls, message: KQMLMessage, indent_level: int = 0) -> str:
        pad = cls._PAD * indent_level
        lines: List[str] = []
        lines.append(f"{pad}({message.performative.value}")
        lines.append(f'{pad} :sender "{message.sender}"')
        lines.append(f'{pad} :receiver "{message.receiver}"')
        if message.reply_with is not None:
            lines.append(f'{pad} :reply-with "{message.reply_with}"')
        if message.in_reply_to is not None:
            lines.append(f'{pad} :in-reply-to "{message.in_reply_to}"')
        lines.append(f'{pad} :language "{message.language.value}"')
        lines.append(f'{pad} :ontology "{message.ontology.value}"')
        lines.append(f"{pad} :content (")
        lines.extend(cls._content_to_kqml(message.content, indent_level + 2))
        lines.append(f"{pad} )")
        lines.append(f"{pad})")
        return "\n".join(lines)

    @classmethod
    def from_kqml(cls, text: str) -> KQMLMessage:
        perf_match = re.search(r"^\s*\((\S+)", text)
        if not perf_match:
            raise ValueError("Cannot find performative in KQML text")

        def extract(keyword: str) -> str | None:
            m = re.search(rf':{keyword}\s+"([^"]+)"', text)
            return m.group(1) if m else None

        content_text = cls._extract_paren_block(text, ":content")
        missing_slots = cls._parse_missing_slots(content_text)
        found_slots = cls._parse_found_slots(content_text)

        return PerformativeRegistry.from_dict({
            "performative": perf_match.group(1),
            "sender": extract("sender") or "",
            "receiver": extract("receiver") or "",
            "language": extract("language") or "GeoSQL",
            "ontology": extract("ontology") or "German-Geostats-v1",
            "reply_with": extract("reply-with"),
            "in_reply_to": extract("in-reply-to"),
            "content": {
                "missing_slots": [
                    {"spatial": s.spatial, "temporal": s.temporal, "attributes": s.attributes}
                    for s in missing_slots
                ],
                "found_slots": [
                    {"spatial": s.spatial, "temporal": s.temporal, "attributes": s.attributes,
                     "data": [r.to_flat_dict() for r in s.data]}
                    for s in found_slots
                ],
            },
        })

    # ── serialisation helpers ─────────────────────────────────────────────────

    @classmethod
    def _content_to_kqml(cls, content: KQMLContent, indent_level: int) -> List[str]:
        pad = cls._PAD * indent_level
        lines: List[str] = []
        if content.missing_slots:
            lines.append(f"{pad}:missing-slots [")
            for i, slot in enumerate(content.missing_slots):
                slot_lines = cls._missing_slot_to_kqml(slot, indent_level + 1)
                if i < len(content.missing_slots) - 1:
                    slot_lines[-1] += ","
                lines.extend(slot_lines)
            lines.append(f"{pad}]")
        if content.found_slots:
            lines.append(f"{pad}:found-slots [")
            for i, slot in enumerate(content.found_slots):
                slot_lines = cls._found_slot_to_kqml(slot, indent_level + 1)
                if i < len(content.found_slots) - 1:
                    slot_lines[-1] += ","
                lines.extend(slot_lines)
            lines.append(f"{pad}]")
        return lines

    @classmethod
    def _missing_slot_to_kqml(cls, slot: MissingSlot, indent_level: int) -> List[str]:
        pad = cls._PAD * indent_level
        return [
            f"{pad}{{spatial: {cls._fmt_spatial(slot.spatial)},",
            f"{pad} temporal: {cls._fmt_temporal(slot.temporal)},",
            f"{pad} attributes: {cls._fmt_attributes(slot.attributes)}}}",
        ]

    @classmethod
    def _found_slot_to_kqml(cls, slot: FoundSlot, indent_level: int) -> List[str]:
        pad = cls._PAD * indent_level
        data_parts = [cls._record_to_kqml(r) for r in slot.data]
        return [
            f"{pad}{{spatial: {cls._fmt_spatial(slot.spatial)},",
            f"{pad} temporal: {cls._fmt_temporal(slot.temporal)},",
            f"{pad} attributes: {cls._fmt_attributes(slot.attributes)},",
            f"{pad} data: [{', '.join(data_parts)}]}}",
        ]

    @classmethod
    def _record_to_kqml(cls, record: DataRecord) -> str:
        parts: List[str] = []
        if record.spatial is not None:
            parts.append(f'spatial: "{record.spatial}"')
        parts.append(f"year: {record.year}")
        for attr in ("population", "married", "live_births"):
            val = getattr(record, attr)
            if val is not None:
                parts.append(f"{attr}: {val}")
        if record.model_extra:
            for k, v in record.model_extra.items():
                parts.append(f"{k}: {v}")
        return "{" + ", ".join(parts) + "}"

    @classmethod
    def _fmt_spatial(cls, spatial: Union[str, List[str]]) -> str:
        if isinstance(spatial, str):
            return f'"{spatial}"'
        return "[" + ", ".join(f'"{s}"' for s in spatial) + "]"

    @classmethod
    def _fmt_temporal(cls, temporal: List[int]) -> str:
        return "[" + ", ".join(str(y) for y in temporal) + "]"

    @classmethod
    def _fmt_attributes(cls, attributes: List[str]) -> str:
        return "[" + ", ".join(attributes) + "]"

    # ── parsing helpers ───────────────────────────────────────────────────────

    @classmethod
    def _extract_paren_block(cls, text: str, keyword: str) -> str:
        m = re.search(re.escape(keyword) + r"\s*\(", text, re.DOTALL)
        if not m:
            return ""
        depth, pos = 1, m.end()
        while pos < len(text) and depth > 0:
            if text[pos] == "(":
                depth += 1
            elif text[pos] == ")":
                depth -= 1
            pos += 1
        return text[m.end(): pos - 1]

    @classmethod
    def _extract_bracket_block(cls, text: str, keyword: str) -> str | None:
        m = re.search(rf":{re.escape(keyword)}\s*\[", text, re.DOTALL)
        if not m:
            return None
        depth, pos = 1, m.end()
        while pos < len(text) and depth > 0:
            if text[pos] == "[":
                depth += 1
            elif text[pos] == "]":
                depth -= 1
            pos += 1
        return text[m.end(): pos - 1]

    @classmethod
    def _parse_missing_slots(cls, content_text: str) -> List[MissingSlot]:
        block = cls._extract_bracket_block(content_text, "missing-slots")
        if block is None:
            return []
        return [
            MissingSlot(
                spatial=cls._parse_spatial(obj),
                temporal=cls._parse_int_list(obj, "temporal"),
                attributes=cls._parse_str_list(obj, "attributes"),
            )
            for obj in cls._split_objects(block)
        ]

    @classmethod
    def _parse_found_slots(cls, content_text: str) -> List[FoundSlot]:
        block = cls._extract_bracket_block(content_text, "found-slots")
        if block is None:
            return []
        slots = []
        for obj in cls._split_objects(block):
            data_block = cls._extract_bracket_block(obj, "data")
            records = [cls._parse_record(r) for r in cls._split_objects(data_block or "")]
            slots.append(FoundSlot(
                spatial=cls._parse_spatial(obj),
                temporal=cls._parse_int_list(obj, "temporal"),
                attributes=cls._parse_str_list(obj, "attributes"),
                data=records,
            ))
        return slots

    @classmethod
    def _split_objects(cls, text: str) -> List[str]:
        objs, depth, buf = [], 0, ""
        for ch in text:
            if ch == "{":
                depth += 1
            if depth > 0:
                buf += ch
            if ch == "}" and depth > 0:
                depth -= 1
                if depth == 0:
                    objs.append(buf.strip())
                    buf = ""
        return [o for o in objs if o]

    @classmethod
    def _parse_spatial(cls, obj: str) -> Union[str, List[str]]:
        m = re.search(r'spatial:\s*"([^"]+)"', obj)
        if m:
            return m.group(1)
        m = re.search(r"spatial:\s*\[([^\]]+)\]", obj)
        if m:
            return [s.strip().strip('"') for s in m.group(1).split(",") if s.strip()]
        return ""

    @classmethod
    def _parse_int_list(cls, obj: str, key: str) -> List[int]:
        m = re.search(rf"{key}:\s*\[([^\]]+)\]", obj)
        if not m:
            return []
        return [int(x.strip()) for x in m.group(1).split(",") if x.strip().lstrip("-").isdigit()]

    @classmethod
    def _parse_str_list(cls, obj: str, key: str) -> List[str]:
        m = re.search(rf"{key}:\s*\[([^\]]+)\]", obj)
        if not m:
            return []
        return [s.strip().strip('"') for s in m.group(1).split(",") if s.strip()]

    @classmethod
    def _parse_record(cls, rec: str) -> DataRecord:
        kwargs: dict = {}
        for m in re.finditer(r'(\w+):\s*(?:"([^"]+)"|(-?\d+))', rec):
            key, str_val, int_val = m.group(1), m.group(2), m.group(3)
            kwargs[key] = str_val if str_val is not None else int(int_val)
        return DataRecord(**kwargs)
