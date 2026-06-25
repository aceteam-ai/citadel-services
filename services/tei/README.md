# tei — Text Embeddings Inference

Serves a text-embedding model via Hugging Face [Text-Embeddings-Inference (TEI)](https://github.com/huggingface/text-embeddings-inference)
behind an **OpenAI-compatible `/v1/embeddings`** endpoint (plus TEI-native `/embed`).
The embedding peer of the `vllm`/`ollama` chat engines — for RAG, semantic search,
clustering, and dedup over your own data.

## Default model

[`Alibaba-NLP/gte-multilingual-base`](https://huggingface.co/Alibaba-NLP/gte-multilingual-base)
— Apache-2.0, 305M params, **multilingual** (100+ languages incl. Chinese),
Matryoshka embeddings (768→256/128 truncatable), 8K context. Open (no gated/token).

Override with `--set EMBED_MODEL=<hf-id>` (e.g. `BAAI/bge-m3`, `intfloat/multilingual-e5-base`).

## Install

```bash
citadel module install tei
# pick a different model:
citadel module install tei --set EMBED_MODEL=BAAI/bge-m3
```

## ⚠️ GPU architecture

TEI ships **architecture-specific** GPU images. The default targets **Ampere
(CC 8.6, e.g. RTX 3090)**: `ghcr.io/huggingface/text-embeddings-inference:86-1.9`.
On other hardware, override `TEI_IMAGE`:

| GPU | Tag |
|---|---|
| Ampere (RTX 30xx, A100) ⇐ default | `86-1.9` |
| Ada (RTX 40xx, L4) | `89-1.9` |
| Turing (T4, RTX 20xx) | `turing-1.9` |
| Hopper (H100) | `hopper-1.9` |
| CPU only | `cpu-1.9` |

```bash
citadel module install tei --set TEI_IMAGE=ghcr.io/huggingface/text-embeddings-inference:89-1.9
```

(The default `:1.9` tag is A100-only and will not start on a 3090 — always use an arch-specific tag.)

## Usage

```bash
# OpenAI-compatible
curl http://localhost:8102/v1/embeddings -X POST -H 'Content-Type: application/json' \
  -d '{"model":"gte","input":["hello world","你好世界"]}'

# TEI-native (returns a raw vector array)
curl http://localhost:8102/embed -X POST -H 'Content-Type: application/json' \
  -d '{"inputs":["hello world"]}'
```

## Resources

~1.8 GB VRAM for the default model at fp16; embeds short texts at several thousand/sec
on a 3090, so it co-hosts comfortably alongside a small `vllm` model on 24 GB.

| Config | Default | Notes |
|---|---|---|
| `EMBED_MODEL` | `Alibaba-NLP/gte-multilingual-base` | any TEI-supported HF embedding model |
| `TEI_IMAGE` | `…:86-1.9` | match your GPU arch (table above) |
