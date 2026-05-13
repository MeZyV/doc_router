# mineru/

Build context for the `mineru-router` service. The Dockerfile is sourced from
the official [opendatalab/MinerU](https://github.com/opendatalab/MinerU) repo
via a **sparse-checkout git submodule** under `upstream/`, so we don't carry a
local fork.

```
mineru/
├── Dockerfile          # symlink → upstream/docker/global/Dockerfile
├── upstream/           # git submodule (sparse: docker/global/ only, ~150 KB)
│   └── docker/global/
│       └── Dockerfile  # tracked upstream
└── README.md           # this file
```

## Initial clone (fresh checkout)

After cloning the doc_router repo:

```bash
git submodule update --init --depth=1 mineru/upstream
git -C mineru/upstream sparse-checkout init --cone
git -C mineru/upstream sparse-checkout set docker/global
git -C mineru/upstream checkout
```

## Refresh from upstream

```bash
git submodule update --remote --depth=1 mineru/upstream
```

Review the diff (e.g. base image version, model bundle changes) before committing.

## Build

`docker compose build mineru-router` follows the symlink and builds with
context `./mineru/` — the upstream Dockerfile has no relative `COPY`, so the
minimal context is sufficient.
