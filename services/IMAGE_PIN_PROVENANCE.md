# Catalog image pins for aceteam-ai/aceteam#12088

This source-only change pins the Claude Code and Kokoro catalog Compose images
to published OCI **index** digests. On 2026-10-23, `docker buildx imagetools
inspect` returned the same index digest for each `:latest` and immutable main
commit tag:

| Catalog image | Immutable main tag | OCI index digest | Successful main publish run |
| --- | --- | --- | --- |
| `ghcr.io/aceteam-ai/claudecode-service` | `6fad6be3951e28d6a4ad34a6e39e8c90113328ae` | `sha256:c2285d2bfe1db43086e240c4c95b3f605bbf83f87579fa29d1b97bade77feee2` | [citadel-cli run 39946524944](https://github.com/aceteam-ai/citadel-cli/actions/runs/39946524944) |
| `ghcr.io/aceteam-ai/kokoro-service` | `sha-ad043e8` (source commit `ad043e87546cb294bd9309447a5fec408efa0312`) | `sha256:0f99603bc21c89b5bfbc941eaec0329805ba52b923b05414c26a111636452241` | [citadel-services run 36191665762](https://github.com/aceteam-ai/citadel-services/actions/runs/36191665762) |

Each inspected index contained a `linux/amd64` image manifest and an
`unknown/unknown` attestation manifest; neither contained an arm64 image. The
catalog manifests therefore advertise only `amd64`. These registry observations
do not establish signed provenance or runtime acceptance. Arm64 builds and
installation on actual nodes require separate verification before rollout.

This pin covers the default CPU Kokoro Compose image. Its default Compose file
has no local `build` fallback, so image resolution uses the pinned index. Its
`compose.gpu.yml` uses a separately built `:cuda` image and remains outside this
pin. The other floating catalog images (paw-compile, meeting, gliner2,
nvr-config) and the
AceTeam `tts-speech` provisioning template remain separate consumer work. The
publisher workflow guard in citadel-services PR #34 does not change these
catalog Compose files. No image was built, pulled, published, or deployed for
this source change.
