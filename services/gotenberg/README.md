# gotenberg — Document Conversion (Office/HTML → PDF)

Serves [Gotenberg](https://gotenberg.dev), a stateless HTTP API that wraps
LibreOffice (Office documents → PDF) and headless Chromium (HTML → PDF), on
your own Citadel node. **No GPU, CPU-only, amd64 + arm64.**

## Sovereign Sign use case

This module is the P2 dependency for **Sovereign Sign**
([aceteam-ai/aceteam#5793](https://github.com/aceteam-ai/aceteam/issues/5793),
"P2: DOCX source support via Citadel-hosted LibreOffice"). Sovereign Sign's P1
signs PDF sources only; DOCX needs a layout-faithful renderer first, because
the executed PDF has to look exactly like what the signer agreed to (a
markdown/structured-extraction parser like `docling` is not sufficient for a
legal signing artifact).

Instead of converting DOCX centrally, the fabric dispatches the conversion to
this module running **on the customer's own Citadel node**. The document is
uploaded once to the node, converted to PDF locally, and only the resulting
PDF (plus its SHA-256 hash) continues through the normal signing flow — the
source DOCX never crosses the network to AceTeam's cloud. For orgs with no
Citadel node online, a central worker-image fallback (LibreOffice bundled into
the worker) covers the same conversion contract, mirroring the pattern already
shipped for `web_fetch` (#5785): node-preferred, central-fallback.

Gotenberg's Chromium route is also useful independently of Sovereign Sign for
high-fidelity HTML → PDF page rendering.

## Install

```bash
citadel module install gotenberg
```

Or run directly with Docker Compose:

```bash
cd services/gotenberg
docker compose up -d
```

## Quick start: convert a DOCX to PDF

```bash
curl --request POST http://localhost:3000/forms/libreoffice/convert \
  --form files=@contract.docx \
  -o contract.pdf
```

(When citadel manages this module, it publishes on the registry host port
8209 instead of the standalone default 3000 — see **Ports** below.)

Multiple files can be merged into one PDF:

```bash
curl --request POST http://localhost:3000/forms/libreoffice/convert \
  --form files=@cover.docx --form files=@body.docx --form merge=true \
  -o merged.pdf
```

## HTML → PDF (Chromium)

```bash
curl --request POST http://localhost:3000/forms/chromium/convert/html \
  --form files=@index.html \
  -o page.pdf
```

## Health check

```bash
curl http://localhost:3000/health
```

```json
{"status":"up","details":{"chromium":{"status":"up",...},"libreoffice":{"status":"up",...}}}
```

## Requirements

| Requirement | Value |
|-------------|-------|
| GPU | No |
| Architecture | amd64, arm64 |

## Configuration

| Variable | Default | Description |
|----------|---------|--------------|
| `API_TIMEOUT` | `30s` | Per-request time limit. Raise for large DOCX files. |
| `LIBREOFFICE_MAX_QUEUE_SIZE` | `0` (unlimited) | Max queued LibreOffice conversions before Gotenberg returns 503. |
| `CHROMIUM_MAX_QUEUE_SIZE` | `0` (unlimited) | Max queued Chromium (HTML→PDF) conversions before Gotenberg returns 503. |

Gotenberg supports many more flags/env vars (route disabling, basic auth,
per-engine restart intervals, etc.) — see the [configuration
docs](https://gotenberg.dev/docs/configuration) for the full list; add them to
`compose.yml`'s `environment:` block as needed.

## Ports

| Host | Container | Description |
|------|-----------|--------------|
| 127.0.0.1:3000 (standalone default) / 127.0.0.1:8209 (citadel-managed, `CITADEL_GOTENBERG_HOST_PORT`) | 3000 | Gotenberg HTTP API, **loopback only** |

Published to `127.0.0.1` deliberately, not `0.0.0.0`: Gotenberg has no
authentication of its own, LibreOffice parses untrusted input, and the
Chromium route can fetch arbitrary URLs, so it must never be reachable from
the mesh or LAN. The only intended consumer is the co-located citadel worker
dispatching conversion jobs on the same node.

8209 is the next free slot in citadel-cli's `services/ports.go` 8200+ host-port
registry block (after the `meeting` module's 8207/8208). **This module does
not yet have a `CITADEL_GOTENBERG_HOST_PORT` entry in that registry** —
registering `EnvGotenbergHostPort` / `GotenbergHostPort = 8209` there is a
required follow-up PR against `citadel-cli` before `citadel module install
gotenberg` will inject the citadel-managed port. Until then, only the
standalone `docker compose up` path (default host port 3000) works; the
`node_tags` below also only start routing fabric jobs once Sovereign Sign's
dispatch logic targets them.

## Volumes

None. Gotenberg is stateless — conversions happen in a per-request temp
workspace inside the container and are cleaned up automatically.

## Links

- [Gotenberg documentation](https://gotenberg.dev)
- [Gotenberg GitHub](https://github.com/gotenberg/gotenberg)
- [Sovereign Sign — aceteam-ai/aceteam#5793](https://github.com/aceteam-ai/aceteam/issues/5793)
