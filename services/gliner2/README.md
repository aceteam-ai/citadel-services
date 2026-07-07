# gliner2 — NER + Relation Extraction

Serves [GLiNER2](https://github.com/fastino-ai/GLiNER2) (Unified Schema-Based
Information Extraction) behind the extraction HTTP contract the AceTeam
**AdaExtract** pipeline expects. It is the provider for the fabric-dispatch
`GLINER_EXTRACTION` capability: entity + relation extraction on your own node,
**CPU-first, no GPU, no external API**.

## Default model

[`fastino/gliner2-base-v1`](https://huggingface.co/fastino/gliner2-base-v1) —
Apache-2.0, 205M params, DeBERTa-v3 encoder. Zero-shot: entity and relation
types are supplied per request, not baked into the model.

Override with `--set MODEL_NAME=<hf-id>` (e.g. `fastino/gliner2-large-v1`).

## Install

```bash
citadel module install gliner2
```

This is the buildable source + manifest for the `ghcr.io/aceteam-ai/gliner2-service`
image referenced by citadel-cli's embedded `services/compose/extraction.yml`.

## HTTP contract

The container serves on `:8100` — the port the AdaExtract `GLiNER2Backend`
client (`python-backend/worker/extraction/backends.py`) hardcodes. citadel
publishes it on host `8202` via `CITADEL_EXTRACTION_HOST_PORT` (kept clear of
vllm's `8100` and the `8100-8199` apps range); a standalone `docker compose up`
defaults the host publish to `8100`.

### `GET /health`

```json
{ "model_loaded": true, "model": "fastino/gliner2-base-v1" }
```

### `POST /extract`

Request (AdaExtract shape — entity/relation type defs from `models/extraction.py`):

```json
{
  "text": "Tim Cook is the CEO of Apple. Apple is headquartered in Cupertino.",
  "entity_types": [
    { "type_id": "person" },
    { "type_id": "company" },
    { "type_id": "location" }
  ],
  "relation_types": [
    { "type_id": "ceo_of", "subject_type": "person", "object_type": "company" },
    { "type_id": "headquartered_in", "subject_type": "company", "object_type": "location" }
  ]
}
```

The Citadel `ExtractionHandler` variant (`{ "text": ..., "schema": {...} }`,
with `entity_types`/`relation_types` nested under `schema`) is also accepted.

Response (validates directly into `Entity` / `Relation`):

```json
{
  "entities": [
    { "id": "…", "text": "Tim Cook", "type": "person", "confidence": 1.0,
      "mentions": [{ "start": 0, "end": 8 }] },
    { "id": "…", "text": "Apple", "type": "company", "confidence": 1.0,
      "mentions": [{ "start": 23, "end": 28 }] }
  ],
  "relations": [
    { "id": "…", "type": "ceo_of", "subject_id": "…", "object_id": "…",
      "confidence": 0.7 }
  ]
}
```

Notes:

- Entities carry character-offset `mentions` (spans) and a per-span
  `confidence` from the model.
- GLiNER2 returns relations as bare `(source, target)` text pairs. The service
  maps each endpoint back to an extracted entity `id` (lenient/substring match),
  synthesising an entity when a relation endpoint isn't in `entity_types`, so
  `subject_id`/`object_id` always reference a real entity. Relation confidence
  defaults to `0.7` (the base model does not emit a per-relation score).

## Usage

```bash
curl http://localhost:8100/extract -X POST -H 'Content-Type: application/json' \
  -d '{"text":"Tim Cook is the CEO of Apple.","entity_types":[{"type_id":"person"},{"type_id":"company"}],"relation_types":[{"type_id":"ceo_of"}]}'
```

## Resources

~1 GB RAM resident (DeBERTa-v3-base + torch CPU). First start downloads the
model (~400 MB) into `~/citadel-cache/huggingface`; subsequent starts are cached.
Extraction latency is well under a second per short document on CPU.

| Config | Default | Notes |
|---|---|---|
| `MODEL_NAME` | `fastino/gliner2-base-v1` | any GLiNER2 HF model (e.g. `…-large-v1`) |
| `EXTRACTION_THRESHOLD` | `0.5` | drop spans/relations below this confidence |
