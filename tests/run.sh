#!/bin/sh
# Builds the test ZIMs, serves them with kiwix-serve in Docker and runs the
# tests against it. Needs Docker and Python 3 with requests and pydantic.
#   tests/run.sh                 # kiwix-serve 3.8.1
#   KIWIX_IMAGE=ghcr.io/kiwix/kiwix-serve:latest tests/run.sh
set -eu
cd "$(dirname "$0")/.."
image=${KIWIX_IMAGE:-ghcr.io/kiwix/kiwix-serve:3.8.1}
port=${KIWIX_PORT:-18089}
zims=$PWD/tests/.zims
cleanup() { docker rm -f kiwix-tool-test >/dev/null 2>&1 || true; }
trap cleanup EXIT

mkdir -p "$zims"
docker run --rm -v "$zims":/out -v "$PWD/tests":/tests:ro python:3.12-slim sh -c \
  "rm -f /out/*.zim && pip install -q --root-user-action=ignore --disable-pip-version-check libzim==3.7.0 &&
   python /tests/make_zims.py /out | grep built && chmod 644 /out/*.zim && chown -R $(id -u):$(id -g) /out"
docker run -d --name kiwix-tool-test -p "127.0.0.1:$port:8080" -v "$zims":/data:ro "$image" '*.zim' >/dev/null
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$port/catalog/v2/entries" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$port/catalog/v2/entries" >/dev/null || { docker logs kiwix-tool-test; exit 1; }
KIWIX_URL="http://127.0.0.1:$port" python3 -m unittest -v tests/test_kiwix_tool.py
