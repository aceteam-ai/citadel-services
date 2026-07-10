# LiveKit

WebRTC SFU that hosts AceTeam team-chat voice huddles (audio + screen share)
on your own node. When a chat channel picks this node as its call machine,
call signaling is proxied through the AceTeam platform to this service and
media flows directly between the callers' browsers and your node.

## How it is normally installed

You usually don't install this by hand: the AceTeam platform provisions it
automatically the first time a channel selects your node for calls, generating
the API key/secret pair and keeping the platform-side copy it needs to mint
join tokens and verify webhooks.

## Manual install

```bash
citadel service catalog update
citadel service catalog install livekit \
  --set LIVEKIT_API_KEY=<key> \
  --set LIVEKIT_API_SECRET=<secret>
citadel run livekit
```

Generate a key pair for standalone use with `openssl rand -hex 16` (key) and
`openssl rand -hex 32` (secret), or use `lk` (the LiveKit CLI) for testing.

## Networking

| Port | Protocol | Purpose |
| ---- | -------- | ------- |
| 7880 | TCP | Signaling WebSocket + HTTP API (reached via the platform relay over the mesh) |
| 7881 | TCP | WebRTC ICE/TCP fallback |
| 7882 | UDP | WebRTC media (single-port UDP mux) |

The compose runs with `network_mode: host` so the UDP mux binds a real host
socket (Linux only). No inbound port-forward is required on most NATs:
`use_external_ip` advertises a STUN-discovered public candidate and callers
hole-punch to it. For the most reliable direct connectivity you can forward
UDP 7882 on your router; otherwise calls that cannot punch through fall back
to the platform TURN relay.

Outbound requirements: UDP to the internet (STUN + media), HTTPS to
`aceteam.ai` (webhooks).

## Sizing

Audio for 8 participants is negligible (<2 Mbps total). One screen share
fanned out to 7 viewers is roughly 20–25 Mbps of node upstream — fine on
office fiber, worth knowing on residential uplinks. No GPU is used.
