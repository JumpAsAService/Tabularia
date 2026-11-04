"""Writes tools.json: what the Python gateway's `tools/list` answers over MCP (routes/mcp.py
_tool_list), for each token level, with metadata-only mode off and on. Run in the gateway's
image, from the repository root, whenever a tool of services/agent changes:

    docker run --rm --user "$(id -u)" -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/mcp:/out" -w /app \
        -e SECURITY__FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_') tabularia-gateway:latest \
        python /out/gen_tools.py
"""
import json
from types import SimpleNamespace

from app.routes import mcp
from app.services import metadata_only
from app.services.agent.registry import REGISTRY

out = {}
for withheld in (False, True):
    metadata_only.on = lambda origin="chat", w=withheld: w  # the switch, as the panel would set it
    for level in ("read", "write"):
        out[f"{level}{'_withheld' if withheld else ''}"] = mcp._tool_list(level)
# the server's instructions (_instructions): level × metadata-only × confirm in Tabularia only
out["instructions"] = {}
for withheld in (False, True):
    metadata_only.on = lambda origin="chat", w=withheld: w
    for confirm in (False, True):
        mcp.get_settings = lambda c=confirm: SimpleNamespace(mcp=SimpleNamespace(confirm_in_tabularia_only=c))
        for level in ("read", "write"):
            out["instructions"][f"{level}|{int(withheld)}|{int(confirm)}"] = mcp._instructions(level)
out["levels"] = {name: {"level": t.level, "only": t.only} for name, t in REGISTRY.items()}
with open("/out/tools.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
    f.write("\n")
