# whatsapp-bridge — Multi-tenant WhatsApp (Baileys) bridge

Runs a multi-tenant WhatsApp bridge on your own Citadel node. One shared bridge
serves many tenants; the per-tenant `X-API-Key` selects the tenant, and the
`/admin/*` control plane is guarded by a separate operator-held `ADMIN_API_KEY`.
**CPU-only.**

Two containers, orchestrated as one module:

- **bridge** (`ghcr.io/sunapi386/whatsapp-bridge`) — the Baileys bridge REST API.
- **db** (`postgres:16-alpine`) — stores the Baileys auth state / linked WhatsApp
  session in the `<project>_whatsapp_pgdata` named volume.

## First-class Citadel module (citadel-cli#624)

This entry makes the WhatsApp bridge a first-class Citadel module: its lifecycle
(install / start / stop / update / uninstall) is managed via the module lockfile
and the desired-state reconcile engine / `MODULE_SET`, replacing the bespoke
`citadel whatsapp` deploy (which remains the fallback for lockfile-less/old nodes).
The `citadel whatsapp` provisioning flow (tenant mint, QR pairing, gateway cert)
stays layered on top.

Key declarations this manifest carries (beyond a plain catalog entry):

- **`config[].generate: secret`** — `ADMIN_API_KEY` and `POSTGRES_PASSWORD` are
  minted per node and are sticky across updates (never rotated under a running
  container).
- **`config[].carry: true`** — `TENANT_API_KEY` / `TENANT_ID` / `TENANT_NAME` are
  minted out of band by the bridge admin API, so they cannot be re-minted by the
  node; `carry` preserves them across an update-in-place instead of rotating the
  platform-stored tenant key to nothing.
- **`health_check.compose_service: bridge`** — the real container is
  `<project>-bridge-N` (not `citadel-whatsapp-bridge`), so health is resolved via
  `docker compose -p <project> ps bridge` rather than a container name that never
  matches.
- **`gateway:`** — exposes the bridge under `/modules/whatsapp/` on the node's
  tsnet gateway (the bridge binds an auto-selected free host port not otherwise
  reachable on the mesh).

## Trust tier / install

**Install as a trusted (first-party) catalog module only.** The `sandbox:` block
is declared but IGNORED for a trusted install (the compose is authoritative); an
untrusted install would harden the Node/Postgres pair with an unvalidated
least-privilege override. The bridge image is private
(`ghcr.io/sunapi386/whatsapp-bridge`), so the node needs `docker login`
credentials to pull it.

```bash
citadel catalog install whatsapp-bridge
# or, remotely: fabric_node_module_set node=<id> module=whatsapp-bridge
```
