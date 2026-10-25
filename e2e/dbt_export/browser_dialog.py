"""Browser test of the dbt export dialog (piece 3): from the Flows page, Export → dbt
opens the dialog with the options; target, folder for an existing project, prefix,
schema, layers, a source's name, a materialization, no tests nor emails are chosen;
the downloaded zip has exactly those choices; the dialog remembers them; an engine
refusal reads in the dialog; Esc closes.
The Schema field and «Already declared» exist only where they mean something (decisions 9
and 10); an invalid field says so to assistive technology too (#72).
Headless Firefox via Marionette against the throwaway stack on 8088.
Needs Firefox (headless, with Marionette) on the machine that runs the test.
usage: python3 browser_dialog.py"""
import base64, io, json, os, re, subprocess, sys, time, urllib.error, urllib.request, zipfile

from environment import WORKDIR, UI, env
from marionette import Firefox, stop

BASE = UI
PROFILE = os.path.join(WORKDIR, "firefox-dbt-dialogo")
SHOTS = WORKDIR
FLOW = "Margin by Category and Month"      # sources on Postgres: all three targets
FLOW_CRM = "Agent Leaderboard"             # reads the CRM's ClickHouse, which is NOT the engine's
outcomes = []


def ok(name, cond, detail=""):
    outcomes.append(bool(cond))
    print(("  ok " if cond else "  ✗  ") + name + ("" if cond else f"  → {str(detail)[:600]}"), flush=True)
    return bool(cond)




def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + "/api" + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read()
        return json.loads(text) if text else None


admin = api("POST", "/auth/login", {"email": env("AUTH__ADMIN_EMAIL"), "password": env("AUTH__ADMIN_PASSWORD")})["access_token"]
flows = {f["name"]: f for f in api("GET", "/flows", token=admin)}
flow, crm = flows[FLOW], flows[FLOW_CRM]
plan = api("GET", f"/flows/{flow['id']}/export/dbt/plan", token=admin)


def open_export(name):
    ff.js(f"""
      const actions = Array.from(document.querySelectorAll('.flow-actions')).find(a => a.parentElement.innerText.includes({json.dumps(name)}));
      actions.querySelector('button svg.lucide-download').closest('button').click(); return 1""")
    ff.wait_for("!!document.querySelector('[role=dialog] .export-body')", 10)
    ff.js("Array.from(document.querySelectorAll('[role=dialog] .export-opt')).find(b => b.querySelector('strong').innerText.trim() === 'dbt').click(); return 1")
    return ff.wait_for("!!document.querySelector('.dx-card') && !document.querySelector('.export-body') && document.querySelectorAll('.dx-card input[name=dx-target]').length === 3", 30)


def write(selector, value):
    """A value in a field, as a person writes it (Vue listens to `input`)."""
    return ff.js(f"""
      const el = document.querySelector({json.dumps(selector)}); if (!el) return false;
      el.value = {json.dumps(value)}; el.dispatchEvent(new Event('input', {{bubbles: true}})); return true""")


os.makedirs(PROFILE, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "--window-size", "1500,1000", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.send("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1500, "height": 1000})
    # the browser is prepared from a static file, not from /login: with the session cookie
    # of an earlier round (a user deleted since) the app would send back to the sign-in
    ff.go(f"{BASE}/favicon.svg")
    ff.js("localStorage.clear(); localStorage.setItem('tabularia-theme', 'dark'); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    # the administrator's session, in the HttpOnly cookie as the gateway writes it
    ff.sign_in(BASE, admin)
    ff.go(f"{BASE}/flows")
    ok("the Flows page lists the flows", ff.wait_for("document.querySelectorAll('.flow-actions').length > 3", 30))
    # the zip the page sends to the download, captured in base64 without really downloading
    ff.js("""
      window.__downloaded = []; window.__zip = null;
      const create = URL.createObjectURL.bind(URL);
      URL.createObjectURL = (b) => { const r = new FileReader(); r.onload = () => { window.__zip = r.result.split(',')[1] }; r.readAsDataURL(b); return create(b) };
      const click = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () { if (this.download) { window.__downloaded.push(this.download); return } return click.apply(this, arguments) };
      return 1""")

    ok("Export → dbt opens the options dialog, with the three targets", open_export(FLOW))
    ok("it is an accessible dialog, with the focus inside", ff.js("const c = document.querySelector('.dx-card'); return c.getAttribute('role') === 'dialog' && c.getAttribute('aria-modal') === 'true' && c.contains(document.activeElement)"))
    chosen = ff.js("return document.querySelector('.dx-card input[name=dx-target]:checked')?.value")
    ok("the starting target is ClickHouse (the first that works)", chosen == "clickhouse", chosen)
    sources = ff.js("return document.querySelectorAll('.dx-source').length")
    outputs = ff.js("return Array.from(document.querySelectorAll('.dx-output select')).map(s => s.value)")
    expected = next(t for t in plan["targets"] if t["id"] == "clickhouse")
    ok("the dialog shows the plan's sources and outputs", sources == len(expected["sources"]) >= 1 and outputs == [o["materialized"] for o in plan["outputs"]], (sources, outputs))
    time.sleep(0.4)
    ff.shot(f"{SHOTS}/dbt-dialog.png")

    # the choices. The complete project: a Schema field, no «Already declared»
    fields = "document.querySelectorAll('.dx-card .dx-row .dx-field').length"
    declared = "document.querySelectorAll('.dx-card .dx-source .dx-check').length"
    ok("complete project: the Schema field is there, «Already declared» is not",
       ff.js(f"return {fields} === 3 && {declared} === 0"), ff.js(f"return [{fields}, {declared}]"))
    write(".dx-card .dx-row .dx-field:nth-child(3) input", "analisi")
    ff.js("document.querySelector('.dx-card input[name=dx-package][value=folder]').click(); return 1")
    ok("folder package: no Schema field (the team's profile decides it), «Already declared» is there",
       ff.wait_for(f"{fields} === 2 && {declared} >= 1", 5), ff.js(f"return [{fields}, {declared}]"))
    write(".dx-card .dx-row .dx-field:nth-child(1) input", "margini")
    ff.js("Array.from(document.querySelectorAll('.dx-card .dx-check')).find(l => l.innerText.includes('staging')).querySelector('input').click(); return 1")
    # a wrong name shows at once and the download stays off
    write(".dx-card .dx-row .dx-field:nth-child(2) input", "Tab-")
    ok("an invalid prefix is flagged and turns the download off",
       ff.wait_for("!!document.querySelector('.dx-bad') && document.querySelector('.dx-actions .primary').disabled", 5))
    ok("the invalid field is marked for assistive technology and points to its own message",
       ff.js("""const i = document.querySelector('.dx-card .dx-row .dx-field:nth-child(2) input');
                const m = document.getElementById(i.getAttribute('aria-describedby') || '');
                return i.getAttribute('aria-invalid') === 'true' && !!m && m.classList.contains('dx-bad') && m.innerText.trim().length > 0"""))
    write(".dx-card .dx-row .dx-field:nth-child(2) input", "tab_")
    write(".dx-card .dx-source input[type=text]", "erp")
    ff.js("""
      const s = document.querySelector('.dx-card .dx-output select');
      s.value = 'view'; s.dispatchEvent(new Event('change', {bubbles: true}));
      for (const l of document.querySelectorAll('.dx-card .dx-check')) {
        if (/data contract|Who built/.test(l.innerText)) l.querySelector('input').click();
      }
      return 1""")
    ok("with valid values the download turns back on", ff.wait_for("!document.querySelector('.dx-bad') && !document.querySelector('.dx-actions .primary').disabled", 5))
    # back to the complete project the schema typed earlier is still there, then the folder again
    ff.js("document.querySelector('.dx-card input[name=dx-package][value=project]').click(); return 1")
    ok("back to the complete project, the Schema field keeps «analisi»",
       ff.wait_for("document.querySelector('.dx-card .dx-row .dx-field:nth-child(3) input')?.value === 'analisi'", 5))
    ff.js("document.querySelector('.dx-card input[name=dx-package][value=folder]').click(); return 1")
    ff.js("document.querySelector('.dx-actions .primary').click(); return 1")
    downloaded = ff.wait_for("window.__downloaded.length === 1 && window.__zip !== null && !document.querySelector('.dx-card')", 120)
    name = ff.js("return window.__downloaded[0]")
    ok("downloads the zip and closes the dialog", downloaded and name == "Margin_by_Category_and_Month_dbt_clickhouse_folder.zip",
       ff.js("return [window.__downloaded, document.querySelector('.dx-error')?.innerText]"))

    files = {}
    if downloaded:
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(ff.js("return window.__zip")))) as z:
            files = {n: z.read(n).decode() for n in z.namelist()}
    names = sorted(files)
    ok("a folder for an existing project: no profile nor dbt_project.yml, INTEGRATION.md is there",
       "INTEGRATION.md" in files and "profiles.yml" not in files and "dbt_project.yml" not in files, names)
    ok("the models are in models/margini/, by layer, with the tab_ prefix",
       any(re.fullmatch(r"models/margini/marts/tab_\w+\.sql", n) for n in names) and all(n.startswith(("models/margini/", "macros/margini/", "seeds/margini/", "tests/margini/", "INTEGRATION")) for n in names), names)
    sources = files.get("models/margini/sources.yml", "")
    ok("the source has the chosen name", "  - name: erp\n" in sources and "{{ source('erp', " in "".join(v for k, v in files.items() if k.endswith(".sql")), sources[:300])
    schema = files.get("models/margini/schema.yml", "")
    ok("no contract tests nor emails in the metadata", "data_tests" not in schema and "owner:" not in schema and "exported_by" not in schema and "flow_id:" in schema, schema[:400])
    views = [n for n, v in files.items() if n.endswith(".sql") and "materialized='view'" in v]
    ok("the chosen output is a view", len(views) == 1, views)
    guide = files.get("INTEGRATION.md", "")
    ok("INTEGRATION.md says what to copy, join_use_nulls, the command of the federated databases and how to run it",
       "`models/margini/`" in guide and "join_use_nulls: 1" in guide and "create_tabularia_sources" in guide and "dbt build -s +tag:" in guide, guide[:600])

    # the choices stay, for this flow
    ok("reopened, the dialog remembers the choices", open_export(FLOW) and ff.wait_for(
        "document.querySelector('.dx-card input[name=dx-package][value=folder]').checked && document.querySelector('.dx-card .dx-row .dx-field:nth-child(2) input').value === 'tab_'"
        " && document.querySelector('.dx-card .dx-source input[type=text]').value === 'erp'", 10))
    ff.js("document.querySelector('.dx-card').dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true})); return 1")
    ok("Esc closes the dialog", ff.wait_for("!document.querySelector('.dx-card')", 5))

    # a refusal only the engine knows (a ClickHouse that is not its own) reads in the dialog
    ok("the dialog opens for a flow on the CRM too", open_export(FLOW_CRM))
    ff.js("document.querySelector('.dx-card input[name=dx-target][value=clickhouse]').click(); document.querySelector('.dx-actions .primary').click(); return 1")
    ok("the reason of the refusal is in the dialog, in plain words (not «422»)", ff.wait_for("!!document.querySelector('.dx-error')", 60) and "ClickHouse" in (ff.js("return document.querySelector('.dx-error').innerText") or "")
       and "422" not in ff.js("return document.querySelector('.dx-error').innerText"), ff.js("return document.querySelector('.dx-error')?.innerText"))
    time.sleep(0.3)
    ff.shot(f"{SHOTS}/dbt-dialog-refusal.png")
    ok("and the dialog stays open to choose another target", ff.js("return !!document.querySelector('.dx-card')"))

    # the AI (piece 4): the options show only if there is an enabled model; with the descriptions the project carries them
    if plan.get("ai", {}).get("available"):
        ok("with the AI available the dialog shows the two AI options", open_export(FLOW) and ff.js(
            "return Array.from(document.querySelectorAll('.dx-card .dx-check')).filter(l => /AI/.test(l.innerText)).length") == 2)
        ff.js("Array.from(document.querySelectorAll('.dx-card .dx-check')).find(l => /Descriptions written by AI/.test(l.innerText)).querySelector('input').click(); return 1")
        note = ff.js("return Array.from(document.querySelectorAll('.dx-card .dx-hint')).map(e => e.innerText).find(t => /sample values/.test(t)) || ''")
        ok("ticked, a note says the model and what the AI sees", plan["ai"]["model"] in note and "3 sample values" in note, note)
        ff.js("window.__downloaded = []; window.__zip = null; document.querySelector('.dx-actions .primary').click(); return 1")
        downloaded = ff.wait_for("window.__downloaded.length === 1 && window.__zip !== null && !document.querySelector('.dx-card')", 300)
        ok("the zip with the AI descriptions downloads", downloaded, ff.js("return document.querySelector('.dx-error')?.innerText"))
        if downloaded:
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(ff.js("return window.__zip")))) as z:
                everything = {n: z.read(n).decode() for n in z.namelist()}
            guide = everything.get("INTEGRATION.md") or everything.get("README.md") or ""
            ok("the «What the flow does» paragraph and the (AI) descriptions are there", "## What the flow does" in guide
               and any("(AI)" in v for k, v in everything.items() if k.endswith(".yml")), guide[:400])
    else:
        print("  (AI not configured on the stack: AI options not tried)")

    # in Italian: the plan's reasons and the engine's refusal come in Italian
    ff.js("document.cookie = 'tabularia-locale=it; path=/; max-age=86400; samesite=lax'; return 1")
    ff.go(f"{BASE}/flows")
    ff.wait_for("document.querySelectorAll('.flow-actions').length > 3", 30)
    ff.js("""
      window.__downloaded = []; window.__zip = null;
      const click = HTMLAnchorElement.prototype.click;
      HTMLAnchorElement.prototype.click = function () { if (this.download) { window.__downloaded.push(this.download); return } return click.apply(this, arguments) };
      return 1""")
    ok("in Italian the dialog opens", open_export(FLOW_CRM))
    reasons = ff.js("return Array.from(document.querySelectorAll('.dx-why')).map(e => e.innerText)")
    ok("the reasons of the targets not available are in Italian", reasons and all(m.startswith("Non disponibile:") for m in reasons)
       and any("DuckDB non la collega" in m for m in reasons), reasons)
    ff.js("document.querySelector('.dx-card input[name=dx-target][value=clickhouse]').click(); document.querySelector('.dx-actions .primary').click(); return 1")
    ok("the engine's refusal is in Italian", ff.wait_for("!!document.querySelector('.dx-error')", 60)
       and "è un ALTRO ClickHouse" in (ff.js("return document.querySelector('.dx-error').innerText") or ""), ff.js("return document.querySelector('.dx-error')?.innerText"))
    time.sleep(0.3)
    ff.shot(f"{SHOTS}/dbt-dialog-it.png")
finally:
    stop(proc)

# a plain user: no plan nor download
u = api("POST", "/users", {"email": "zzbrowser-dbt@example.com", "password": "Lettore-di-prova-9", "full_name": "zz"}, token=admin)
try:
    api("POST", f"/projects/{flow['project_id']}/permissions", {"capability": "view", "user_id": u["id"]}, token=admin)
    reader = api("POST", "/auth/login", {"email": "zzbrowser-dbt@example.com", "password": "Lettore-di-prova-9"})["access_token"]
    statuses = []
    for method, path, body in (("GET", f"/flows/{flow['id']}/export/dbt/plan", None), ("POST", f"/flows/{flow['id']}/export/dbt", {"target": "duckdb"})):
        try:
            api(method, path, body, token=reader); statuses.append(200)
        except urllib.error.HTTPError as e:
            statuses.append(e.code)
    ok("for a plain user the plan and the download with options answer 403", statuses == [403, 403], statuses)
finally:
    api("DELETE", f"/users/{u['id']}", token=admin)
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
