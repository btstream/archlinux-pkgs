#!/usr/bin/env bash
set -euo pipefail

image=btstream-ci-build
mkdir -p ci-output/packages
docker build --build-arg "BUILDER_UID=$(id -u)" --tag "$image" --file .ci/Dockerfile .ci
docker run --rm \
  --mount "type=bind,src=$PWD,dst=/source,readonly" \
  --mount "type=bind,src=$PWD/ci-output,dst=/output" \
  --workdir /source --env BEFORE_SHA --env FULL_BUILD \
  --env GIT_CONFIG_COUNT=1 --env GIT_CONFIG_KEY_0=safe.directory \
  --env GIT_CONFIG_VALUE_0=/source \
  "$image" python scripts/resolve.py --output /output/plan.json

python scripts/build.py "$image"
