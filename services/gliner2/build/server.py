"""GLiNER2 NER + relation extraction HTTP service.

Implements the contract expected by the AceTeam AdaExtract ``GLiNER2Backend``
client (``python-backend/worker/extraction/backends.py``) and the Citadel node
``ExtractionHandler`` (``citadel-cli/internal/jobs/extraction_handler.go``):

    GET  /health   -> {"model_loaded": bool}
    POST /extract  -> {"entities": [...], "relations": [...]}

The response entities/relations validate directly into the AdaExtract
``Entity`` / ``Relation`` pydantic models: entities carry a ``confidence`` in
[0, 1] and character-offset ``mentions`` (spans); relations reference entity
``id``s via ``subject_id`` / ``object_id`` with their own ``confidence``.
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gliner2-service")

MODEL_NAME = os.environ.get("MODEL_NAME", "fastino/gliner2-base-v1")
DEFAULT_THRESHOLD = float(os.environ.get("EXTRACTION_THRESHOLD", "0.5"))
DEFAULT_ENTITY_CONFIDENCE = 0.5
DEFAULT_RELATION_CONFIDENCE = 0.7

# Populated at startup. Kept module-global so /health can report readiness.
_model: Any | None = None


# --- Request schema -------------------------------------------------------


class EntityTypeIn(BaseModel):
    type_id: str
    description: str | None = None


class RelationTypeIn(BaseModel):
    type_id: str
    description: str | None = None
    subject_type: str | None = None
    object_type: str | None = None


class ExtractRequest(BaseModel):
    text: str
    entity_types: list[EntityTypeIn] = Field(default_factory=list)
    relation_types: list[RelationTypeIn] = Field(default_factory=list)
    # Citadel-cli ExtractionHandler forwards an optional JSON `schema` blob;
    # accept the same {entity_types, relation_types} nested under it.
    schema_: dict | None = Field(default=None, alias="schema")

    model_config = {"populate_by_name": True}


# --- Response schema (mirrors models/extraction.py) -----------------------


class Span(BaseModel):
    start: int
    end: int


class Entity(BaseModel):
    id: str
    text: str
    type: str
    confidence: float = Field(ge=0.0, le=1.0)
    mentions: list[Span] | None = None


class Relation(BaseModel):
    id: str
    type: str
    subject_id: str
    object_id: str
    confidence: float = Field(ge=0.0, le=1.0)


class ExtractResponse(BaseModel):
    entities: list[Entity]
    relations: list[Relation]


# --- Helpers --------------------------------------------------------------


def _clamp(value: float | None, default: float) -> float:
    """Clamp a confidence to [0, 1]; the Entity/Relation models reject anything else."""
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


def _norm(text: str) -> str:
    return text.strip().casefold()


def _entity_item(raw: Any) -> tuple[str, int | None, int | None, float | None]:
    """Normalise a GLiNER2 entity item (str or dict) into (text, start, end, confidence)."""
    if isinstance(raw, dict):
        return (
            str(raw.get("text", "")),
            raw.get("start"),
            raw.get("end"),
            raw.get("confidence"),
        )
    return (str(raw), None, None, None)


def _relation_pair(raw: Any) -> tuple[str, str, float | None] | None:
    """Normalise a GLiNER2 relation item into (source, target, confidence).

    GLiNER2 returns bare ``(source, target)`` tuples; some builds may attach a
    confidence or return a dict. Handle all shapes defensively.
    """
    if isinstance(raw, dict):
        src = raw.get("source") or raw.get("subject") or raw.get("head")
        tgt = raw.get("target") or raw.get("object") or raw.get("tail")
        if src is None or tgt is None:
            return None
        return (str(src), str(tgt), raw.get("confidence"))
    if isinstance(raw, (list, tuple)):
        if len(raw) < 2:
            return None
        conf = raw[2] if len(raw) >= 3 and isinstance(raw[2], (int, float)) else None
        return (str(raw[0]), str(raw[1]), conf)
    return None


class _EntityIndex:
    """Resolves relation-endpoint text back to an entity id, synthesising when needed."""

    def __init__(self, source_text: str) -> None:
        self.source_text = source_text
        self.entities: list[Entity] = []
        self._by_norm: dict[str, str] = {}

    def add(self, text: str, type_id: str, start: int | None, end: int | None, confidence: float) -> str:
        eid = str(uuid.uuid4())
        mentions = None
        if start is not None and end is not None:
            mentions = [Span(start=int(start), end=int(end))]
        self.entities.append(
            Entity(id=eid, text=text, type=type_id, confidence=confidence, mentions=mentions)
        )
        self._by_norm.setdefault(_norm(text), eid)
        return eid

    def resolve(self, text: str, fallback_type: str | None) -> str:
        """Return an entity id for a relation endpoint, matching leniently or synthesising one."""
        key = _norm(text)
        if key in self._by_norm:
            return self._by_norm[key]
        # Substring fallback: relation endpoints and entity spans rarely align exactly.
        for existing in self.entities:
            en = _norm(existing.text)
            if en and (en in key or key in en):
                return existing.id
        # No extracted entity matches (its type may not be in entity_types) -> synthesise.
        idx = self.source_text.find(text)
        start = idx if idx >= 0 else None
        end = idx + len(text) if idx >= 0 else None
        return self.add(
            text=text,
            type_id=fallback_type or "entity",
            start=start,
            end=end,
            confidence=DEFAULT_ENTITY_CONFIDENCE,
        )


# --- Extraction core ------------------------------------------------------


def _run_extraction(text: str, req: ExtractRequest) -> ExtractResponse:
    assert _model is not None, "model not loaded"

    entity_types = list(req.entity_types)
    relation_types = list(req.relation_types)
    if req.schema_:
        if not entity_types and req.schema_.get("entity_types"):
            entity_types = [EntityTypeIn.model_validate(e) for e in req.schema_["entity_types"]]
        if not relation_types and req.schema_.get("relation_types"):
            relation_types = [RelationTypeIn.model_validate(r) for r in req.schema_["relation_types"]]

    index = _EntityIndex(text)

    # --- Entities: request spans + confidence so we can build mentions ----
    entity_labels = [et.type_id for et in entity_types]
    if entity_labels:
        ent_result = _model.extract_entities(
            text,
            entity_labels,
            threshold=DEFAULT_THRESHOLD,
            include_confidence=True,
            include_spans=True,
        )
        grouped = ent_result.get("entities", {}) if isinstance(ent_result, dict) else {}
        for type_id, items in grouped.items():
            if not isinstance(items, list):
                items = [items]
            for raw in items:
                ent_text, start, end, conf = _entity_item(raw)
                if not ent_text:
                    continue
                index.add(
                    text=ent_text,
                    type_id=type_id,
                    start=start,
                    end=end,
                    confidence=_clamp(conf, DEFAULT_ENTITY_CONFIDENCE),
                )

    # --- Relations: map (source, target) text back to entity ids ----------
    relations: list[Relation] = []
    relation_labels = [rt.type_id for rt in relation_types]
    rel_type_by_id = {rt.type_id: rt for rt in relation_types}
    if relation_labels:
        rel_result = _model.extract_relations(text, relation_labels, threshold=DEFAULT_THRESHOLD)
        rel_groups = (
            rel_result.get("relation_extraction", {}) if isinstance(rel_result, dict) else {}
        )
        for type_id, pairs in rel_groups.items():
            if not isinstance(pairs, list):
                pairs = [pairs]
            rt = rel_type_by_id.get(type_id)
            for raw in pairs:
                pair = _relation_pair(raw)
                if pair is None:
                    continue
                src_text, tgt_text, conf = pair
                subject_id = index.resolve(src_text, rt.subject_type if rt else None)
                object_id = index.resolve(tgt_text, rt.object_type if rt else None)
                relations.append(
                    Relation(
                        id=str(uuid.uuid4()),
                        type=type_id,
                        subject_id=subject_id,
                        object_id=object_id,
                        confidence=_clamp(conf, DEFAULT_RELATION_CONFIDENCE),
                    )
                )

    return ExtractResponse(entities=index.entities, relations=relations)


# --- App ------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _model
    logger.info("Loading GLiNER2 model %s ...", MODEL_NAME)
    from gliner2 import GLiNER2  # heavy import (torch); defer to startup

    _model = GLiNER2.from_pretrained(MODEL_NAME)
    logger.info("Model %s loaded.", MODEL_NAME)
    yield
    _model = None


app = FastAPI(title="GLiNER2 Extraction Service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"model_loaded": _model is not None, "model": MODEL_NAME}


@app.post("/extract", response_model=ExtractResponse)
async def extract(req: ExtractRequest) -> ExtractResponse:
    import anyio

    return await anyio.to_thread.run_sync(_run_extraction, req.text, req)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8100")))
