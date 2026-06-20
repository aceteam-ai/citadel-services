# WeChat

Personal WeChat REST API via [WeChatFerry](https://github.com/lich0821/WeChatFerry).
Exposes an authenticated REST API (`:8000`) for reading and writing personal
WeChat messages from a Windows VM. One account per person — there is no shared
org WeChat.

> **Not a container.** Unlike the inference services in this catalog, WeChat is
> **not** a Docker Compose stack. WeChatFerry injects a DLL into the running
> WeChat desktop client, so the FastAPI server must run on a Windows host. There
> is therefore no `compose.yml`; this catalog entry exists for **discoverability**
> (documenting the port, health endpoint, and auth env var). It is **not**
> installable via `citadel service catalog install` — provision it with the
> PowerShell scripts below instead.

## Architecture

```
Windows VM (Proxmox/QEMU)
  WeChat 3.9.12.51  --DLL inject-->  WeChatFerry (NNG RPC :10086, localhost)
                                          |
                                     FastAPI :8000  (X-API-Key auth)
  Firewall: :8000 open, :10086-10087 blocked from LAN
```

## Requirements

| Requirement | Value |
|-------------|-------|
| OS | **Windows 10/11 only** (DLL injection) |
| WeChat | 3.9.12.51 (pinned; 4.x not supported by WeChatFerry) |
| Python | 3.12+ |
| GPU | No |
| Architecture | amd64 |

## Quick Start

This service is provisioned on a Windows VM, not via `docker compose`:

```powershell
# 1. provision (admin PowerShell on the VM)
powershell -ExecutionPolicy Bypass -File provision\bootstrap.ps1
#    installs WeChat, Python/uv, Defender exclusions, firewall, generates WCF_API_KEY

# 2. log into WeChat (QR scan on the PVE console or RDP; 2FA confirm on phone)

# 3. start the API
provision\start-service.ps1

# 4. test
curl -H "X-API-Key: YOUR_KEY" http://<vm-ip>:8000/health
# {"status":"ok","connected":true,"logged_in":true}
```

See the upstream repo for the full endpoint list and provisioning scripts.

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `WCF_API_KEY` | yes | API key for the `X-API-Key` header. Generated into `api/.env` by `provision/bootstrap.ps1`. |

## Ports

| Host | Container | Description |
|------|-----------|-------------|
| 8000 | 8000 | Authenticated WeChat REST API (`X-API-Key`) |

Raw WeChatFerry RPC ports `10086-10087` are firewalled off the LAN by the
bootstrap script; only the authenticated `:8000` is reachable.

## Health Check

`GET /health` returns connection and login status:

```json
{"status": "ok", "connected": true, "logged_in": true}
```

## Per-person enablement (Citadel)

WeChat accounts are personal, so each person runs this microservice on **their
own** Citadel node and binds it per-person:

1. **Provision the VM** — `provision/bootstrap.ps1` on a Windows 10/11 VM.
2. **QR login** — scan on the PVE console / RDP, confirm on phone (2FA every
   restart; login does not persist).
3. **Start the service** — `provision/start-service.ps1` runs uvicorn on `:8000`.
4. **Node relays** — the Citadel worker on the node co-located with the VM is
   already subscribed to its per-node shell stream, so it relays `HTTP_PROXY`
   calls to the VM over the LAN. No service registration is required.
5. **Backend binds + routes** — call
   `wechat_connect(api_url, api_key, node_id)` (the node's Headscale numeric ID,
   from `terminal_list_nodes`). The backend stores the binding keyed to the
   person and routes every `wechat_*` call to that node's per-node stream.

## Links

- [WeChatFerry](https://github.com/lich0821/WeChatFerry)
- [wechat microservice (provisioning + endpoints)](https://github.com/sunapi386/wechat)
