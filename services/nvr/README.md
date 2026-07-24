# nvr — Self-Hosted Camera NVR (Wyze → Frigate)

Records your Wyze cameras and runs local object detection on your own Citadel
node, with no cloud dependency for storage. **CPU/iGPU, amd64.**

Two containers, orchestrated as one module:

- **docker-wyze-bridge** — logs into your Wyze account and exposes each camera as
  a local RTSP stream (Wyze TUTK P2P → RTSP). Runs with **host networking** —
  mandatory: TUTK needs LAN broadcast + UDP hole-punching, and Docker bridge NAT
  breaks camera discovery (`wyze: connect failed: discovery timeout`).
- **frigate** ([Frigate NVR](https://frigate.video)) — continuous recording +
  local object detection on the Intel iGPU (OpenVINO) or CPU.

A small `nvr-config` init container (`ghcr.io/aceteam-ai/nvr-config`, built from
`aceteam-ai/citadel-cli`) generates Frigate's `config.yml` from the assignment
config and, for NAS storage, verifies the mount before Frigate starts.

## Install

```
fabric_node_module_set node=<id> module=nvr
```

with config:

| Key | Required | Default | Notes |
|-----|----------|---------|-------|
| `WYZE_EMAIL` / `WYZE_PASSWORD` / `API_ID` / `API_KEY` | ✅ | — | Wyze account + developer API creds (secrets; never logged; only wyze-bridge sees them) |
| `NVR_CAMERAS` | ✅ | — | Comma-separated camera names (`name` or `name=stream-path`) |
| `NVR_RETENTION_DAYS` | | `12` | Continuous-recording days (sized by days — Synology NFS quota is invisible to `df`) |
| `NVR_DETECTOR` | | `openvino` | `openvino` (Intel iGPU) or `cpu` |
| `NVR_STORAGE_MODE` | | `local` | `local` \| `nas` \| `volume` |
| `NVR_STORAGE_TARGET` | | — | per mode: node path / `host:/export` / volume id |

## Storage gotchas (baked into the module)

- `/config` (Frigate's SQLite DB) **always** stays on local disk — SQLite corrupts
  over NFS. Only `/media` (recordings) follows the storage target.
- **`nas` mode:** NFS-mount the export onto `~/.citadel-cli/nvr/media` **before**
  assignment. The export needs `no_root_squash` (Frigate runs as root). The
  `nvr-config` init container statfs-verifies `/media` is a real, root-writable
  network mount and **refuses to start** otherwise — so a failed mount never
  silently writes recordings to the local disk.
- Cameras already emit ~1 Mbit/s H.264; the module records **raw** (no transcode).

## Notes

- One bridge per node/site; only cameras with a routable LAN IP stream (no
  remote-site relay). Wyze cloud can serve stale camera LAN IPs — a camera
  reboot / DHCP reservation fixes it.
- Frigate 0.17 requires an explicit detector `model:` path (handled) and uses the
  migrated `record.continuous.days` retention key (handled).

Source of truth: `aceteam-ai/citadel-cli` `services/nvr-service/` (citadel-cli#597).
