"""Writes prompts.json: the AI agents' default instructions and the context Tabularia adds
to them as the panel shows it (routes/ai.py _example_context), for each language and with
metadata-only mode off and on; today's date and the rows' cap are placeholders the Go
gateway fills. Run in the gateway's image, from the repository root, whenever a text of
services/ai_prompts.py or a context function changes:

    docker run --rm --user "$(id -u)" -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/aiprompts:/out" \
        -w /app -e HOME=/tmp -e SECURITY__FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_') tabularia-gateway:latest \
        python /out/gen_prompts.py
"""
import json
from types import SimpleNamespace

from app.routes import ai
from app.services import ai_agent, ai_prompts, metadata_only

TODAY = "\x00TODAY\x00"
ROWS = 987654321

ai_prompts.today = lambda: f"Today is {TODAY}."
ai_agent.get_settings = lambda: SimpleNamespace(ai=SimpleNamespace(max_result_rows=ROWS))

out = {"agents": list(ai_prompts.AGENTS), "defaults": dict(ai_prompts.DEFAULTS), "today": TODAY, "rows": str(ROWS),
       "contexts": {}}
for withheld in (False, True):
    metadata_only.on = lambda origin="chat", w=withheld: w
    for code in list(ai_prompts.LANGUAGES):
        for agent in ai_prompts.AGENTS:
            text = ai._example_context(agent, code)
            out["contexts"][f"{agent}|{ai_prompts.LANGUAGES[code]}|{int(withheld)}"] = text
with open("/out/prompts.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
    f.write("\n")
