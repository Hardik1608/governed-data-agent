# Docker check (image build and container smoke test)

Run on 2026-10-15 on the shared GPU host (Docker 29.7.2, buildx 0.36.1, Compose 5.5.0; no GPU used) by the first version of `scripts/docker_check.sh` (same steps; it took the shared host's job lock first): a private buildx builder capped at 2 GB builds the image, the container starts on a synthetic warehouse (`data-agent build-mini`), then everything the script created is removed.

| Check | Result |
|---|---|
| build | ok in 89 s, image 1274967636 bytes |
| uid | 10001 (expected 10001) |
| cli | ok |
| readyz | {"ready":true,"roles":["analyst","finance","viewer"],"agent":"governed-agent","agent_version":"1"} |
| healthz | {"status":"ok","version":"0.1.0"} |
| ai-disclosure header | x-ai-generated: true |
| docker healthcheck | healthy |
| compose config | ok |
| promtool rules | ok |
