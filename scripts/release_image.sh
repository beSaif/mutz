#!/usr/bin/env bash
# Build and push the self contained image (amd64 + arm64) to ghcr.io/besaif/mutz.
#   scripts/release_image.sh            # push :latest
#   scripts/release_image.sh v3 --load  # build only, for this machine's platform
# Needs a built data/ and the models in the local Hugging Face cache (python -m search.index --download-models).
# Leaves out .env, logs, leads (app.sqlite), the gateway cache and the raw crawl.
set -euo pipefail
cd "$(dirname "$0")/.."
TAG=${1:-latest}
MODE=${2:---push}
IMAGE=ghcr.io/besaif/mutz
HUB=${HF_HUB_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}/hub}

CTX=$(mktemp -d)
trap 'rm -rf "$CTX"' EXIT
rsync -a --exclude-from=.dockerignore --exclude=demo/ --exclude='/*.png' --exclude='Dockerfile*' --exclude=.dockerignore . "$CTX/src/"
mkdir -p "$CTX/data/enrich" "$CTX/data/raw" "$CTX/models"
cp -R data/index data/eval data/chunks.jsonl data/pages.jsonl data/kg.sqlite "$CTX/data/"
cp data/enrich/grants*.jsonl "$CTX/data/enrich/"
cp data/raw/index.jsonl "$CTX/data/raw/"
cp -R "$HUB/models--BAAI--bge-m3" "$HUB/models--BAAI--bge-reranker-v2-m3" "$CTX/models/"
cp Dockerfile.release "$CTX/Dockerfile"

if [ "$MODE" = "--load" ]; then
  docker build -t "$IMAGE:$TAG" "$CTX"
else
  docker buildx inspect mutz-multi >/dev/null 2>&1 || docker buildx create --name mutz-multi --driver docker-container >/dev/null
  docker buildx build --builder mutz-multi --platform linux/amd64,linux/arm64 -t "$IMAGE:$TAG" --push "$CTX"
fi
