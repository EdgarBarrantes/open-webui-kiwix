#!/bin/sh
# Installs the tool in a throwaway Open WebUI, the way Workspace > Tools >
# "+" does it, and checks that Open WebUI loads it: three functions, the
# settings (valves), and that the settings can be saved.
#   tests/openwebui.sh                # v0.12.0
#   tests/openwebui.sh v0.11.4 v0.10.2
set -eu
cd "$(dirname "$0")/.."
port=${OWUI_PORT:-18091}
name=kiwix-tool-owui-test
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT

for version in "${@:-v0.12.0}"; do
  cleanup
  docker run -d --name "$name" -p "127.0.0.1:$port:8080" -e WEBUI_SECRET_KEY=test-only \
    -e ENABLE_OLLAMA_API=false -e ENABLE_OPENAI_API=false -e OFFLINE_MODE=true \
    "ghcr.io/open-webui/open-webui:$version" >/dev/null
  for _ in $(seq 1 120); do
    curl -sf "http://127.0.0.1:$port/health" >/dev/null && break
    sleep 2
  done
  OWUI="http://127.0.0.1:$port" VERSION="$version" python3 - <<'EOF'
import json, os, sys, urllib.request

base = os.environ["OWUI"]
token = None


def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"null")


token = call("POST", "/api/v1/auths/signup",
             {"name": "Test", "email": "test@example.org", "password": "test-only-password"})["token"]
call("POST", "/api/v1/tools/create", {"id": "kiwix", "name": "Kiwix offline library",
                                      "content": open("kiwix_tool.py").read(), "meta": {"description": "", "manifest": {}}})
tool = call("GET", "/api/v1/tools/id/kiwix")
names = sorted(s["name"] for s in tool["specs"])
assert names == ["list_offline_library", "read_offline_article", "search_offline_library"], names
search = next(s for s in tool["specs"] if s["name"] == "search_offline_library")
assert set(search["parameters"]["properties"]) == {"query", "book"}, search["parameters"]
assert tool["meta"]["manifest"]["version"], tool["meta"]
valves = call("GET", "/api/v1/tools/id/kiwix/valves/spec")["properties"]
assert {"kiwix_url", "public_url", "books", "languages"} <= set(valves), valves
saved = call("POST", "/api/v1/tools/id/kiwix/valves/update", {"kiwix_url": "http://kiwix.test:8080"})
assert saved["kiwix_url"] == "http://kiwix.test:8080", saved
print(f"Open WebUI {os.environ['VERSION']}: tool loads, {len(names)} functions, {len(valves)} settings: ok")
EOF
done
