# meeting

The meeting notetaker **media stack** for a Citadel node: one Linux container
bundling Chromium + Xvfb + PulseAudio + ffmpeg + a session supervisor
(`meetingd`). A node can join a call and record clean per-session audio with
**no host media stack** — no host Chrome, PulseAudio, Xvfb, or ffmpeg required.

Source and image build live in
[aceteam-ai/citadel-cli `services/meeting-service`](https://github.com/aceteam-ai/citadel-cli/tree/main/services/meeting-service);
this directory is the catalog copy `citadel module install meeting` resolves.

## Install

```bash
citadel module install meeting
```

## How it works

The citadel `MEETING_JOIN` handler stays in-process on the host and drives the
meeting browser over the Chrome DevTools Protocol (CDP) on a loopback-published
port; this container owns the browser and the entire audio path (a per-session
PulseAudio null sink, Chrome routed into it, ffmpeg recording the sink monitor to
a mono 16 kHz WAV under the shared workspace mount).

`GET /health` runs a **canary-tone capture** and returns 503 unless it records
non-silent audio, so the module is only "healthy" when it can actually capture
sound — guarding the autoplay-silence failure class.

## Ports (host loopback only)

| Host | Container | Purpose |
|------|-----------|---------|
| 8207 | 8102 | meetingd session/control + `/health` API |
| 8208 | 9223 | Chrome CDP for the active session |

## Config

| Var | Default | Purpose |
|-----|---------|---------|
| `MEETING_PROFILE_DIR` | `~/.citadel-cli/meeting-profile` | Host dir for the persistent signed-in Chrome profile (must survive reinstall) |
| `XVFB_RESOLUTION` | `1280x720x24` | Virtual display geometry |

## Scope

- **v1 is amd64-only** (`requires.arch: [amd64]`); the published image is
  `linux/amd64`. arm64 is a deferred follow-up.
- The signed-in Google profile is a **host bind-mount**, never a named volume, so
  it survives restart/upgrade/reinstall. Profile seeding is a later PR.
