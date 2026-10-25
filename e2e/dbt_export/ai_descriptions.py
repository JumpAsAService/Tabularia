"""LIVE test of the AI descriptions in the dbt export (piece 4), on the development
stack with the real provider: export with the option, check of the texts, second
identical export (memory per version, no new spend), dbt parse + docs generate.
usage: python3 ai_descriptions.py (needs an enabled AI model; it is a paid call to the provider)"""
import hashlib, io, os, re, subprocess, sys, zipfile
from environment import WORKDIR, IMAGE
from common import api, dbt, sign_in, ok, outcomes, connection_passwords

FLOW = "Margin by Category and Month"
sign_in()
flow = next(f for f in api("GET", "/flows")[1] if f["name"] == FLOW)
plan = api("GET", f"/flows/{flow['id']}/export/dbt/plan")[1]
ok("the plan says the AI is there, with the model", plan.get("ai", {}).get("available"), plan.get("ai"))
print("    model:", plan.get("ai"))
spend_before = api("GET", "/ai/usage")[1] if api("GET", "/ai/usage")[0] == 200 else None


def download():
    st, data = api("POST", f"/flows/{flow['id']}/export/dbt", {"target": "duckdb", "ai_descriptions": True}, raw=True)
    return st, data


st, first = download()
if not ok("the export with the AI descriptions answers with a zip", st == 200 and first[:2] == b"PK", (st, first[:400] if isinstance(first, (bytes, str)) else first)):
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed"); sys.exit(1)
files = {n: zipfile.ZipFile(io.BytesIO(first)).read(n).decode() for n in zipfile.ZipFile(io.BytesIO(first)).namelist()}
readme = files["README.md"]
ai = [l.strip() for f in ("models/schema.yml", "models/sources.yml") for l in files.get(f, "").splitlines() if "(AI)" in l]
ok("the README has «What the flow does» and the warning about the (AI) descriptions", "## What the flow does" in readme and "(AI)" in readme, readme[:800])
ok(f"there are (AI) descriptions on the columns ({len(ai)})", len(ai) >= 3, ai[:5])
ok("no Jinja delimiter in the AI's texts", not any(re.search(r"\{\{|\{%|\{#", l) for l in ai))
print("    " + readme[readme.index("## What the flow does"):][:700].replace("\n", "\n    ") if "## What the flow does" in readme else "")
for l in ai[:6]:
    print("    " + l[:220])
st, second = download()
ok("the second export of the same version is the same zip (memory per version)", st == 200 and hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest())
folder = f"{WORKDIR}/ai_descriptions"
subprocess.run(["docker", "run", "--rm", "-v", f"{WORKDIR}:/x", IMAGE, "sh", "-c", "rm -rf /x/ai_descriptions"], capture_output=True)
os.makedirs(folder)
zipfile.ZipFile(io.BytesIO(first)).extractall(folder)
variables = connection_passwords()
for command in (["parse"], ["docs", "generate"]):
    rc, out = dbt(folder, *command, variables=variables)
    ok(f"dbt {' '.join(command)} on the project with the AI descriptions", rc == 0, out[-800:])
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
