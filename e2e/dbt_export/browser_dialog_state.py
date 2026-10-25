"""The dbt download dialog in Firefox: its state between openings, what it sends, and how it
speaks to assistive technology. Each check guards a fix:
  #69  a download that is still being prepared is cancelled by Esc, ×, Cancel or by leaving the
       page, and nothing of it reaches the next flow's dialog (busy button, error, plan, zip);
  #70  «Already declared» only in the folder package, never sent for the complete project;
  #77  the Schema field only in the complete project, never sent for the folder package;
  #74  an empty folder is remembered empty (only the resolved default is sent);
  #43 #49 #73  the plan's default folder, refused targets and model names as the plan says;
  #49  a target the plan offers only into the team's own project (`requires`: ClickHouse with an
       Oracle source) can be chosen in the folder package, with its sources «Already declared»
       and locked, and sent declared; in the complete project it stays off with its reason;
  #55 #59  the AI texts kept for the flow version (`ai.stored`) are offered with the AI off, sent,
       remembered, and the hint names the models that wrote them;
  #72  each invalid field says the rule it breaks, with aria-invalid and aria-describedby;
  #48  two sources with the same name and a table in common are flagged; each source's name
       when none is typed is the one the gateway gives (from the plan's unsuffixed default);
  #75  the file name from the gateway's `filename*`, and a fallback that keeps any letter;
  #71  Export → dbt by keyboard: Esc or Cancel give the focus back to the row's Export button.
The export POSTs are answered inside the page by a tiny fake zip (no engine work), except the
last check, which downloads for real. The plans are the gateway's, served from the page WITHOUT
the AI options: no check can make a call to the AI provider (the AI checks serve their own
plans, and their downloads are the fake zip).
Firefox headless via Marionette (port 2828), as browser_dialog.py.
usage: python3 browser_dialog_state.py [part of a check's name, e.g. 69 or 48real]"""
import copy, json, os, subprocess, sys, time, urllib.error, urllib.request

from environment import BASE, WORKDIR, UI, env
from marionette import ENTER, ESC, TAB, Firefox, stop

A_NAME, B_NAME = "Margin by Category and Month", "Cost to Serve by Channel"   # Sample flows
PROFILE = os.path.join(WORKDIR, "firefox-dbt-dialogo-stato")
ONLY = sys.argv[1] if len(sys.argv) > 1 else ""
results = []


def ok(name, cond, detail=""):
    results.append(bool(cond))
    print(("  ok " if cond else "  ✗  ") + name + ("" if cond else f"  → {json.dumps(detail, ensure_ascii=False, default=str)[:700]}"), flush=True)
    return bool(cond)


def api(method, path, body=None, token=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read()
        return json.loads(text) if text and "json" in r.headers.get("content-type", "") else text


# The page's fetch, wrapped: the export POST answers a fake zip (optionally late, failing, with a
# Content-Disposition, or ignoring the abort, as a request already on the wire does), the plans
# come from here, and the flows list can show a flow under another name.
HOOKS = r"""
if (!window.__dx) {
  const S = window.__dx = {posts: [], dl: [], exportDelay: 0, exportFail: false, honourAbort: true, exportHeader: null,
                           realExport: false, planDelayFor: null, planDelay: 0, plans: {}, rename: {}};
  const orig = window.fetch.bind(window);
  const wait = (ms, signal) => new Promise((res, rej) => {
    const t = setTimeout(res, ms);
    if (signal && S.honourAbort) signal.addEventListener('abort', () => { clearTimeout(t); rej(new DOMException('The operation was aborted.', 'AbortError')) });
  });
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    const method = ((init && init.method) || 'GET').toUpperCase();
    const signal = init && init.signal;
    let m = url.match(/\/flows\/(\d+)\/export\/dbt$/);
    if (m && method === 'POST') {
      S.posts.push({flow: +m[1], body: JSON.parse(init.body)});
      if (S.exportDelay) await wait(S.exportDelay, signal);
      if (S.exportFail) throw new TypeError('NetworkError when attempting to fetch resource.');
      if (S.realExport) return orig(input, init);
      const h = {'content-type': 'application/zip'};
      if (S.exportHeader) h['content-disposition'] = S.exportHeader;
      return new Response(new Blob(['PK\x05\x06' + '\0'.repeat(18)]), {status: 200, headers: h});
    }
    m = url.match(/\/flows\/(\d+)\/export\/dbt\/plan$/);
    if (m) {
      if (S.planDelayFor === +m[1]) await wait(S.planDelay, signal);
      const p = S.plans[m[1]];
      if (p) return new Response(JSON.stringify(p), {status: 200, headers: {'content-type': 'application/json'}});
    }
    if (method === 'GET' && /\/flows(\/search)?(\?|$)/.test(url) && Object.keys(S.rename).length) {
      const r = await orig(input, init); const j = await r.json();
      for (const f of (Array.isArray(j) ? j : (j.items || []))) if (S.rename[f.id]) f.name = S.rename[f.id];
      return new Response(JSON.stringify(j), {status: 200, headers: {'content-type': 'application/json'}});
    }
    return orig(input, init);
  };
  const click = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () { if (this.download) { S.dl.push(this.download); return } return click.apply(this, arguments) };
}
return 1
"""

STATE = r"""
const card = document.querySelector('.dx-card');
if (!card) return {open: false};
const prim = card.querySelector('.dx-actions .primary');
const cancel = Array.from(card.querySelectorAll('.dx-actions button')).find(b => !b.classList.contains('primary'));
return {open: true, sub: card.querySelector('.dx-sub').innerText.trim(), primDisabled: prim.disabled, primLabel: prim.innerText.trim(),
        cancelDisabled: cancel.disabled, error: card.querySelector('.dx-error')?.innerText || null,
        loading: !card.querySelector('input[name=dx-target]'),
        outputs: Array.from(card.querySelectorAll('.dx-output-what code')).map(x => x.innerText.trim())}
"""


def reset(**kw):
    """A clean slate between checks: no remembered choices, the gateway's plans, no other stub."""
    ff.js("""
      const S = window.__dx, kw = arguments[0];
      for (const k of Object.keys(localStorage)) if (k.startsWith('tabularia.dbtExport.')) localStorage.removeItem(k);
      Object.assign(S, {posts: [], dl: [], exportDelay: 0, exportFail: false, honourAbort: true, exportHeader: null,
                        realExport: false, planDelayFor: null, planDelay: 0, plans: arguments[1], rename: {}}, kw);
      return 1""", kw, {str(k): v for k, v in PLANS.items()})


def serve_plan(flow_id, plan):
    ff.js("window.__dx.plans[arguments[0]] = arguments[1]; return 1", str(flow_id), plan)


def remember(flow_id, choices):
    ff.js("localStorage.setItem(arguments[0], JSON.stringify(arguments[1])); return 1", f"tabularia.dbtExport.{flow_id}", choices)


def close_if_open():
    if ff.js("return !!document.querySelector('.dx-card')"):
        ff.js("document.querySelector('.dx-card .dx-x').click(); return 1")
        ff.wait_for("!document.querySelector('.dx-card')", 5)
    if ff.js("return !!document.querySelector('.export-body')"):
        ff.js("document.querySelector('.export-cancel').click(); return 1")
        ff.wait_for("!document.querySelector('.export-body')", 5)


def open_dialog(name, loaded=True):
    """Export → dbt from the row of `name`, with the mouse."""
    close_if_open()
    ff.js("""
      const az = Array.from(document.querySelectorAll('.flow-actions')).find(a => a.parentElement.innerText.includes(arguments[0]));
      az.querySelector('button svg.lucide-download').closest('button').click(); return 1""", name)
    assert ff.wait_for("!!document.querySelector('[role=dialog] .export-body')", 10), "export menu"
    ff.js("Array.from(document.querySelectorAll('[role=dialog] .export-opt')).find(b => b.querySelector('strong').innerText.trim() === 'dbt').click(); return 1")
    assert ff.wait_for("!!document.querySelector('.dx-card')", 10), "dbt dialog"
    if loaded:
        assert ff.wait_for("!!document.querySelector('.dx-card input[name=dx-target]')", 30), "plan"
        time.sleep(0.2)


def state():
    return ff.js(STATE)


def download():
    ff.js("document.querySelector('.dx-actions .primary').click(); return 1")


def last_post():
    return ff.js("return window.__dx.posts[window.__dx.posts.length - 1] || null")


def type_in(selector, value):
    return ff.js("""
      const el = document.querySelector(arguments[0]); if (!el) return false;
      el.value = arguments[1]; el.dispatchEvent(new Event('input', {bubbles: true})); return true""", selector, value)


def type_source(i, value):
    ff.js("""const el = document.querySelectorAll('.dx-card .dx-source input[type=text]')[arguments[0]];
             el.value = arguments[1]; el.dispatchEvent(new Event('input', {bubbles: true})); return 1""", i, value)
    time.sleep(0.2)


def placeholders():
    return ff.js("return Array.from(document.querySelectorAll('.dx-card .dx-source input[type=text]')).map(e => e.placeholder)")


def package(value):
    ff.js("document.querySelector(`.dx-card input[name=dx-package][value=${arguments[0]}]`).click(); return 1", value)
    time.sleep(0.15)


def target(value):
    ff.js("document.querySelector(`.dx-card input[name=dx-target][value=${arguments[0]}]`).click(); return 1", value)
    time.sleep(0.15)


def field(label):
    """The input of a field of the Package group, found by its visible label (null if absent)."""
    return f"(Array.from(document.querySelectorAll('.dx-card .dx-field')).find(l => l.querySelector('span')?.innerText.trim() === {json.dumps(label)})?.querySelector('input') || null)"


def invalid(expr):
    """aria-invalid and the visible text aria-describedby points to, for the input `expr`."""
    return ff.js(f"""
      const el = {expr}; if (!el) return null;
      const ids = (el.getAttribute('aria-describedby') || '').split(/\\s+/).filter(Boolean);
      const texts = ids.map(i => document.getElementById(i)).filter(x => x && x.offsetParent !== null).map(x => x.innerText.trim());
      return {{invalid: el.getAttribute('aria-invalid'), described: texts, bad: el.classList.contains('bad'),
               download: !document.querySelector('.dx-actions .primary').disabled}}""")


def source(i):
    return f"document.querySelectorAll('.dx-card .dx-source input[type=text]')[{i}]"


def go(path, text=None):
    """To another page through the app's own link (same document, so the page hooks stay)."""
    ff.js("document.querySelector(`a[href='${arguments[0]}']`).click(); return 1", path)
    cond = f"location.pathname === {json.dumps(path)}"
    if text:
        cond += f" && Array.from(document.querySelectorAll('.flow-actions')).some(a => a.parentElement.innerText.includes({json.dumps(text)}))"
    assert ff.wait_for(cond, 30), f"page {path}"
    time.sleep(0.5)


def plan(fid):
    return copy.deepcopy(PLANS[fid])


def with_sources(fid, sources):
    """The plan of `fid` with these sources on every target."""
    p = plan(fid)
    for t in p["targets"]:
        t["sources"] = copy.deepcopy(sources)
    return p


ORACLE = {"key": "7:FIN", "connection": "ora", "schema": "FIN", "default_name": "db_7_fin", "default_base": "db_7_fin", "tables": ["LEDGER"]}


def oracle_plan(fid):
    """What export_plan gives for `fid` reading an Oracle table next to its Postgres one: no
    target is available, and ClickHouse takes the flow only into the team's own project that
    already declares the Oracle source (folder package)."""
    p = plan(fid)
    pg = copy.deepcopy(next(t for t in p["targets"] if t["id"] == "clickhouse")["sources"][:1])
    for t in p["targets"]:
        if t["id"] == "clickhouse":
            t.update(available=False, reason="zz: ClickHouse cannot read oracle", sources=pg + [copy.deepcopy(ORACLE)],
                     requires={"package": "folder", "declared": [ORACLE["key"]]})
        else:
            t.update(available=False, reason=f"zz: {t['id']} cannot read oracle", sources=[])
    return p


def ai_plan(fid, available, model, descriptions=False, translations=False, models=()):
    """The plan of `fid` with these AI options: what the AI kept for the version and who wrote it."""
    p = plan(fid)
    p["ai"] = {"available": available, "model": model, "stored": {"descriptions": descriptions, "translations": translations, "models": list(models)}}
    return p


TARGET_ROWS = r"""
const r = {};
for (const i of document.querySelectorAll('.dx-card input[name=dx-target]')) {
  const l = i.closest('label');
  r[i.value] = {disabled: i.disabled, checked: i.checked, why: l.querySelector('.dx-why')?.innerText.trim() || null,
                note: l.querySelector('small')?.innerText.trim() || null};
}
return r
"""

# each source row: its «Already declared» box (null when not shown), whether it can be changed, its name field
SOURCE_ROWS = r"""
return Array.from(document.querySelectorAll('.dx-card .dx-source')).map(r => {
  const c = r.querySelector('.dx-check input'), n = r.querySelector('input[type=text]');
  const shown = !!c && c.offsetParent !== null;
  const ids = shown ? (c.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean) : [];
  return {what: r.querySelector('.dx-source-what strong').innerText.trim(), declared: shown ? c.checked : null, locked: shown && c.disabled,
          described: ids.map(i => document.getElementById(i)).filter(x => x && x.offsetParent !== null).map(x => x.innerText.trim()),
          name: n.value, nameEditable: !n.disabled && !n.readOnly}})
"""

# the Include group: its checkboxes (label text, ticked) and the AI hint
INCLUDE_GROUP = r"""
const g = Array.from(document.querySelectorAll('.dx-card fieldset')).find(f => f.querySelector('legend')?.innerText.trim() === 'Include');
if (!g) return null;
return {boxes: Array.from(g.querySelectorAll('.dx-check')).map(l => ({text: l.innerText.trim(), checked: l.querySelector('input').checked})),
        hint: g.querySelector('.dx-hint')?.innerText.trim() || null}
"""


def tick(text):
    """Clicks the Include checkbox whose label holds `text` (False when there is none)."""
    done = ff.js("""
      const l = Array.from(document.querySelectorAll('.dx-card .dx-check')).find(x => x.innerText.includes(arguments[0]));
      if (!l) return false; l.querySelector('input').click(); return true""", text)
    time.sleep(0.15)
    return done


def case(name):
    return not ONLY or ONLY in name


token = api("POST", "/auth/login", {"email": env("AUTH__ADMIN_EMAIL"), "password": env("AUTH__ADMIN_PASSWORD")})["access_token"]
flows = {f["name"]: f for f in api("GET", "/flows", token=token)}
A, B = flows[A_NAME]["id"], flows[B_NAME]["id"]
PLANS, LIVE_AI = {}, {}
for fid in (A, B):
    p = api("GET", f"/flows/{fid}/export/dbt/plan", token=token)
    LIVE_AI[fid] = p.pop("ai", None)        # never the AI options: no paid call from these checks
    PLANS[fid] = p
DUCK_A = next(t for t in PLANS[A]["targets"] if t["id"] == "duckdb")

os.makedirs(PROFILE, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "--window-size", "1500,1000", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.send("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1500, "height": 1000})
    ff.go(f"{UI}/favicon.svg")
    ff.js("localStorage.clear(); localStorage.setItem('tabularia-theme', 'dark'); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    ff.sign_in(UI, token)
    ff.go(f"{UI}/flows")
    assert ff.wait_for("document.querySelectorAll('.flow-actions').length > 3", 40), "flows page"
    ff.js(HOOKS)

    # the gateway's plan gives each source the default it suffixes from (#48)
    if case("48plan"):
        ok("48plan: every source of the plan has its unsuffixed default (default_base) next to its default name",
           all(s.get("default_base") and s["default_name"].startswith(s["default_base"]) for t in PLANS[A]["targets"] for s in t["sources"])
           and DUCK_A["sources"], [t["sources"] for t in PLANS[A]["targets"]])

    # ── #69: every opening is independent of the requests of the one before ───────────────
    if case("69a"):
        reset(exportDelay=5000)
        open_dialog(A_NAME)
        target("duckdb")
        download()
        time.sleep(0.5)
        s = state()
        ff.keys(ESC)
        closed = ff.wait_for("!document.querySelector('.dx-card')", 5)
        open_dialog(B_NAME)
        sb = state()
        ok("69a: after Esc on A's running download, B's Download is free", closed and s.get("primLabel") == "Preparing…"
           and not sb["primDisabled"] and sb["primLabel"] == "Download", {"A busy": s, "B": sb})
        time.sleep(5.5)
        sb2 = state()
        ok("69a: when A's request would have ended, B is still open, no zip of A, no error",
           sb2["open"] and not ff.js("return window.__dx.dl.length") and not sb2.get("error"), {"B": sb2, "dl": ff.js("return window.__dx.dl")})
    if case("69b"):
        # the stale answer arrives all the same (a request that ignores the abort)
        reset(exportDelay=4000, honourAbort=False)
        open_dialog(A_NAME)
        target("duckdb")
        download()
        time.sleep(0.4)
        ff.js("document.querySelector('.dx-card .dx-x').click(); return 1")
        ff.wait_for("!document.querySelector('.dx-card')", 5)
        open_dialog(B_NAME)
        time.sleep(4.5)
        sb = state()
        ok("69b: an answer for A that arrives anyway does not close B nor download", sb["open"] and not ff.js("return window.__dx.dl.length")
           and not sb["primDisabled"], {"B": sb, "dl": ff.js("return window.__dx.dl")})
    if case("69c"):
        reset(exportDelay=3000, exportFail=True, honourAbort=False)
        open_dialog(A_NAME)
        target("duckdb")
        download()
        time.sleep(0.4)
        ff.js("document.querySelector('.dx-card .dx-x').click(); return 1")
        ff.wait_for("!document.querySelector('.dx-card')", 5)
        open_dialog(B_NAME)
        time.sleep(3.5)
        sb = state()
        ok("69c: A's failure is never shown in B's dialog", sb["open"] and not sb["error"] and not sb["primDisabled"], sb)
    if case("69d"):
        reset(planDelayFor=A, planDelay=3500, honourAbort=False)
        open_dialog(A_NAME, loaded=False)
        time.sleep(0.4)
        ff.keys(ESC)
        ff.wait_for("!document.querySelector('.dx-card')", 5)
        open_dialog(B_NAME)
        before = state()["outputs"]
        time.sleep(4)
        after = state()
        expected = [o["model"] for o in PLANS[B]["outputs"]]
        ok("69d: A's slow plan never replaces B's plan", before == expected and after["outputs"] == expected and not after["loading"],
           {"before": before, "after": after, "expected": expected})
    if case("69e"):
        reset(exportDelay=4000)
        open_dialog(A_NAME)
        target("duckdb")
        download()
        time.sleep(0.4)
        s = state()
        ff.js("Array.from(document.querySelectorAll('.dx-card .dx-actions button')).find(b => !b.classList.contains('primary')).click(); return 1")
        closed = ff.wait_for("!document.querySelector('.dx-card')", 3)
        time.sleep(4)
        ok("69e: Cancel works while preparing, and nothing downloads afterwards", not s["cancelDisabled"] and closed and not ff.js("return window.__dx.dl.length"),
           {"busy": s, "closed": closed, "dl": ff.js("return window.__dx.dl")})
    if case("69f"):
        # leaving the page (a sidebar link, the back button) unmounts the dialog: its download goes too
        for honour in (True, False):
            reset(exportDelay=3000, honourAbort=honour)
            open_dialog(A_NAME)
            target("duckdb")
            download()
            time.sleep(0.4)
            busy = state().get("primLabel")
            go("/datasources")
            time.sleep(3.5)
            dl = ff.js("return window.__dx.dl")
            go("/flows", A_NAME)
            ok("69f: leaving the page while the zip is prepared downloads nothing afterwards"
               + ("" if honour else " (also when the answer arrives anyway)"), busy == "Preparing…" and not dl, {"busy": busy, "dl": dl})

    # ── #70 / #77 / #74: options of the other package stay out of the request ─────────────
    if case("70"):
        reset()
        key = DUCK_A["sources"][0]["key"]
        remember(A, {"target": "duckdb", "package": "project", "folder": "", "prefix": "", "layers": False, "materializations": {},
                     "tests": True, "emails": True, "sources": {key: {"name": None, "declared": True}}})
        open_dialog(A_NAME)
        visible = ff.js("return Array.from(document.querySelectorAll('.dx-card .dx-source .dx-check')).filter(e => e.offsetParent !== null).length")
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("70: complete project: no 'Already declared' checkbox, nothing declared in the request",
           visible == 0 and not any(v.get("declared") for v in body.get("sources", {}).values()), {"visible": visible, "sources": body.get("sources")})
        open_dialog(A_NAME)
        package("folder")
        ticks = ff.js("""const c = Array.from(document.querySelectorAll('.dx-card .dx-source')).map(r => r.querySelector('.dx-check input')).filter(x => x && x.offsetParent !== null);
                         return c.map(x => x.checked)""")
        download()
        ff.wait_for("window.__dx.posts.length === 2", 5)
        body = (last_post() or {}).get("body", {})
        ok("70: folder package: the checkbox is there, remembered, and sent", ticks and ticks[0] is True
           and body.get("sources", {}).get(key, {}).get("declared") is True and body.get("package") == "folder", {"checked": ticks, "body": body})
    if case("77"):
        reset()
        remember(B, {"target": "duckdb", "package": "folder", "folder": "", "prefix": "", "layers": False, "schema": "analisi",
                     "materializations": {}, "tests": True, "emails": True, "sources": {}})
        open_dialog(B_NAME)
        visible = ff.js(f"const el = {field('Schema')}; return !!el && el.offsetParent !== null")
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("77: folder package: no Schema field, no schema in the request", not visible and "schema" not in body and body.get("package") == "folder",
           {"visible": visible, "body": body})
        open_dialog(B_NAME)
        package("project")
        value = ff.js(f"const el = {field('Schema')}; return el && el.offsetParent !== null ? el.value : null")
        download()
        ff.wait_for("window.__dx.posts.length === 2", 5)
        body = (last_post() or {}).get("body", {})
        ok("77: complete project: the Schema field is back with its value, and sent", value == "analisi" and body.get("schema") == "analisi", {"value": value, "body": body})
    if case("74"):
        reset()
        open_dialog(B_NAME)
        target("duckdb")
        package("folder")
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        memory = json.loads(ff.js(f"return localStorage.getItem('tabularia.dbtExport.{B}')") or "{}")
        ok("74: an empty folder downloads into the default folder, and is remembered empty",
           body.get("folder") == PLANS[B]["default_folder"] and memory.get("folder") == "", {"sent": body.get("folder"), "remembered": memory.get("folder")})
        open_dialog(B_NAME)
        f = ff.js(f"const el = {field('Folder')}; return el && [el.value, el.placeholder]")
        package("project")
        download()
        ff.wait_for("window.__dx.posts.length === 2", 5)
        body = (last_post() or {}).get("body", {})
        ok("74: reopened, the field is empty with the default as placeholder; the complete project has no folder",
           f == ["", PLANS[B]["default_folder"]] and body.get("folder") == "", {"field": f, "sent": body.get("folder")})
    if case("43"):
        reset()
        p = plan(B)
        p["default_folder"] = "zz_qualita_citta"
        serve_plan(B, p)
        open_dialog(B_NAME)
        target("duckdb")
        package("folder")
        s = state()
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("43: the plan's ASCII default folder is used as is, with no error", not s["primDisabled"] and not ff.js("return !!document.querySelector('.dx-bad')")
           and body.get("folder") == "zz_qualita_citta", {"state": s, "body": body})

    # ── #72: each rule says what it wants, on the field, for assistive technology too ──────
    if case("72"):
        reset()
        open_dialog(A_NAME)
        target("duckdb")
        type_in(".dx-card .dx-source input[type=text]", "Vendite ERP")
        time.sleep(0.2)
        r = invalid(source(0))
        ok("72: a bad source name is marked invalid and says the rule", r and r["invalid"] == "true" and r["described"] and "63" in r["described"][0]
           and not r["download"], r)
        type_in(".dx-card .dx-source input[type=text]", "")
        ff.js(f"const el = {field('Model prefix')}; el.value = '1tab_'; el.dispatchEvent(new Event('input', {{bubbles: true}})); return 1")
        time.sleep(0.2)
        r = invalid(field("Model prefix"))
        ok("72: prefix '1tab_' gets the prefix rule (a letter first, at most 20)", r and r["invalid"] == "true" and r["described"] and "20" in r["described"][0], r)
        ff.js(f"const el = {field('Model prefix')}; el.value = ''; el.dispatchEvent(new Event('input', {{bubbles: true}})); return 1")
        ff.js(f"const el = {field('Folder')}; el.value = '{'a' * 41}'; el.dispatchEvent(new Event('input', {{bubbles: true}})); return 1")
        time.sleep(0.2)
        r = invalid(field("Folder"))
        ok("72: a 41-character folder gets the folder rule (at most 40)", r and r["invalid"] == "true" and r["described"] and "40" in r["described"][0], r)
        ff.js(f"const el = {field('Folder')}; el.value = ''; el.dispatchEvent(new Event('input', {{bubbles: true}})); return 1")
        time.sleep(0.2)
        r = invalid(field("Folder"))
        ok("72: valid values: no aria-invalid, Download on", r and r["invalid"] in (None, "false") and not r["described"] and r["download"], r)

    # ── #48: source names ───────────────────────────────────────────────────────────────────
    if case("48"):
        reset()
        same_table = [{"key": "9:sales", "connection": "ERP", "schema": "sales", "default_name": "db_9_sales", "default_base": "db_9_sales", "tables": ["orders"]},
                      {"key": "9:crm", "connection": "ERP", "schema": "crm", "default_name": "db_9_crm", "default_base": "db_9_crm", "tables": ["orders"]}]
        serve_plan(A, with_sources(A, same_table))
        open_dialog(A_NAME)
        target("duckdb")
        type_source(0, "erp")
        type_source(1, "erp")
        r = [invalid(source(i)) for i in (0, 1)]
        ok("48: the same name on two sources with the same table is flagged on both, Download off",
           all(x and x["invalid"] == "true" and x["described"] and "erp" in x["described"][0] and "orders" in x["described"][0] for x in r) and not r[0]["download"], r)
        reset()
        serve_plan(A, with_sources(A, [dict(same_table[0], tables=["orders"]), dict(same_table[1], tables=["lines"])]))
        open_dialog(A_NAME)
        target("duckdb")
        type_source(0, "erp")
        type_source(1, "erp")
        r = [invalid(source(i)) for i in (0, 1)]
        ok("48: the same name on sources with different tables is accepted (dbt allows it)", all(x and x["invalid"] in (None, "false") for x in r) and r[0]["download"], r)
    if case("48real"):
        # what export_plan sends for the schemas «Sales», «sales» and «altro» of one connection:
        # the gateway already moved the second default aside (_2), and names a source from the
        # unsuffixed default again once a typed name changes the collisions
        reset()
        three = [{"key": "9:Sales", "connection": "ERP", "schema": "Sales", "default_name": "src_sales", "default_base": "src_sales", "tables": ["ordini"]},
                 {"key": "9:sales", "connection": "ERP", "schema": "sales", "default_name": "src_sales_2", "default_base": "src_sales", "tables": ["righe"]},
                 {"key": "9:altro", "connection": "ERP", "schema": "altro", "default_name": "src_altro", "default_base": "src_altro", "tables": ["clienti"]}]
        serve_plan(A, with_sources(A, three))
        open_dialog(A_NAME)
        target("duckdb")
        ok("48real: with nothing typed, the names are the plan's", placeholders() == ["src_sales", "src_sales_2", "src_altro"], placeholders())
        type_source(0, "erp")
        ok("48real: Sales named «erp»: «sales» gets the plain default, as the gateway names it (src_sales)",
           placeholders()[1:] == ["src_sales", "src_altro"], placeholders())
        type_source(0, "")
        type_source(2, "src_sales")
        ok("48real: altro named «src_sales»: Sales and sales move aside to src_sales_2 and src_sales_3, as in the gateway",
           placeholders()[:2] == ["src_sales_2", "src_sales_3"] and all((invalid(source(i)) or {}).get("invalid") in (None, "false") for i in range(3)),
           placeholders())

    # ── #49 / #73: the dialog shows what the plan says ──────────────────────────────────────
    if case("49"):
        reset()
        p = plan(B)
        for t in p["targets"]:
            if t["id"] == "clickhouse":
                t.update(available=False, reason="zz: ClickHouse reads only PostgreSQL, MySQL and MariaDB", sources=[])
        serve_plan(B, p)
        open_dialog(B_NAME)
        chosen = ff.js("return document.querySelector('.dx-card input[name=dx-target]:checked')?.value")
        reasons = ff.js("return Array.from(document.querySelectorAll('.dx-card .dx-why')).map(e => e.innerText)")
        first = next(t["id"] for t in p["targets"] if t["available"])
        ok("49: a target the plan refuses shows its reason and is not preselected", chosen == first and any("zz: ClickHouse reads only" in m for m in reasons),
           {"checked": chosen, "expected": first, "reasons": reasons})
    if case("73"):
        reset()
        p = plan(B)
        p["outputs"][0]["model"] = "m_2024_margin"
        serve_plan(B, p)
        open_dialog(B_NAME)
        ff.js(f"const el = {field('Model prefix')}; el.value = 'tab_'; el.dispatchEvent(new Event('input', {{bubbles: true}})); return 1")
        time.sleep(0.2)
        ok("73: the preview is the prefix followed by the plan's model name", state()["outputs"][0] == "tab_m_2024_margin", state()["outputs"])

    # ── #49: a target the plan offers only into the team's own project (`requires`) ────────
    if case("49folder"):
        reset()
        serve_plan(B, oracle_plan(B))
        open_dialog(B_NAME)
        t, s = ff.js(TARGET_ROWS), state()
        ok("49folder: complete project: ClickHouse stays off with its reason, Download off",
           t["clickhouse"]["disabled"] and "zz: ClickHouse cannot read" in (t["clickhouse"]["why"] or "") and s["primDisabled"], {"targets": t, "state": s})
        package("folder")
        t, rows, s = ff.js(TARGET_ROWS), ff.js(SOURCE_ROWS), state()
        ok("49folder: folder package: ClickHouse can be chosen and is, with no «not available»; the others stay off",
           not t["clickhouse"]["disabled"] and t["clickhouse"]["checked"] and not t["clickhouse"]["why"]
           and "already declare" in (t["clickhouse"]["note"] or "") and t["duckdb"]["disabled"] and t["native"]["disabled"], t)
        ok("49folder: the Oracle source is «Already declared» and locked, saying why; the Postgres one stays the user's; Download on",
           len(rows) == 2 and rows[1]["what"] == "ora · FIN" and rows[1]["declared"] is True and rows[1]["locked"]
           and rows[1]["described"] and "cannot read" in rows[1]["described"][0] and rows[1]["nameEditable"]
           and rows[0]["declared"] is False and not rows[0]["locked"] and not s["primDisabled"], {"rows": rows, "state": s})
        still = ff.js("const c = document.querySelectorAll('.dx-card .dx-source .dx-check input')[1]; const was = c.checked; c.click(); return [was, c.checked]")
        ok("49folder: a click does not untick the locked source", still == [True, True], still)
        package("project")
        t, rows, s = ff.js(TARGET_ROWS), ff.js(SOURCE_ROWS), state()
        ok("49folder: back to the complete project: ClickHouse off again with its reason, no declared box, Download off",
           t["clickhouse"]["disabled"] and "zz: ClickHouse cannot read" in (t["clickhouse"]["why"] or "") and s["primDisabled"]
           and all(r["declared"] is None for r in rows), {"targets": t, "rows": rows, "state": s})
        package("folder")
        type_source(1, "fin_team")
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("49folder: the download sends ClickHouse, the folder package and the Oracle source declared under the typed name",
           body.get("target") == "clickhouse" and body.get("package") == "folder" and "schema" not in body
           and body.get("sources") == {ORACLE["key"]: {"name": "fin_team", "declared": True}}, body)
        open_dialog(B_NAME)
        t, rows, s = ff.js(TARGET_ROWS), ff.js(SOURCE_ROWS), state()
        ok("49folder: reopened, the choice is back: ClickHouse, folder package, the source declared and locked with its name",
           t["clickhouse"]["checked"] and not t["clickhouse"]["disabled"] and rows[1]["declared"] is True and rows[1]["locked"]
           and rows[1]["name"] == "fin_team" and not s["primDisabled"], {"targets": t, "rows": rows, "state": s})
        # control: a target that is simply available keeps its user-made ticks only
        reset()
        open_dialog(A_NAME)
        target("duckdb")
        package("folder")
        rows = ff.js(SOURCE_ROWS)
        ok("49folder: control, an available target locks nothing", rows and all(r["declared"] is False and not r["locked"] for r in rows), rows)

    # ── #55 #59: the AI texts kept for the flow version ────────────────────────────────────
    if case("55plan"):
        ok("55plan: the gateway's plan says what the AI kept for the flow version (ai.stored)",
           all(isinstance(LIVE_AI[f], dict) and isinstance(LIVE_AI[f].get("stored"), dict)
               and {"descriptions", "translations", "models"} <= set(LIVE_AI[f]["stored"]) for f in (A, B)), LIVE_AI)
    if case("55off"):
        reset()
        serve_plan(B, ai_plan(B, False, None, descriptions=True, models=["old-model"]))
        open_dialog(B_NAME)
        inc = ff.js(INCLUDE_GROUP)
        texts = [b["text"] for b in (inc or {}).get("boxes", [])]
        ok("55off: AI off, descriptions kept for the version: offered as such, unticked, and no translations box",
           "Use the AI descriptions kept for this version" in texts and not any("translation" in x.lower() for x in texts)
           and not inc["hint"], inc)
        tick("descriptions kept")
        inc = ff.js(INCLUDE_GROUP)
        ok("55off: ticked, the hint names the model that wrote them", bool(inc["hint"]) and "old-model" in inc["hint"], inc)
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("55off: the download asks for them: ai_descriptions true, no ai_translations",
           body.get("ai_descriptions") is True and "ai_translations" not in body, body)
        open_dialog(B_NAME)
        inc = ff.js(INCLUDE_GROUP)
        ok("55off: reopened, the tick is remembered", any(b["checked"] and "descriptions kept" in b["text"] for b in inc["boxes"]), inc)
        reset()
        serve_plan(B, ai_plan(B, False, None, descriptions=True, translations=True, models=["m-one", "m-two"]))
        open_dialog(B_NAME)
        tick("translations kept")
        inc = ff.js(INCLUDE_GROUP)
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("55off: translations kept by two models: offered, the hint names both, only ai_translations true",
           bool(inc["hint"]) and "m-one, m-two" in inc["hint"] and body.get("ai_translations") is True
           and body.get("ai_descriptions") is False, {"include": inc, "body": body})
        # control: nothing kept and the AI off: no option, nothing sent, a remembered tick ignored
        reset()
        remember(B, {"target": "duckdb", "package": "project", "folder": "", "prefix": "", "layers": False, "materializations": {},
                     "tests": True, "emails": True, "sources": {}, "ai_descriptions": True, "ai_translations": True})
        serve_plan(B, ai_plan(B, False, None))
        open_dialog(B_NAME)
        inc = ff.js(INCLUDE_GROUP)
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("55off: control, AI off with nothing kept: no AI option and none sent, even when remembered",
           len(inc["boxes"]) == 2 and "ai_descriptions" not in body and "ai_translations" not in body, {"include": inc, "body": body})
    if case("55on"):
        reset()
        serve_plan(B, ai_plan(B, True, "new-model", descriptions=True, models=["old-model"]))
        open_dialog(B_NAME)
        tick("Descriptions written by AI")
        hint = (ff.js(INCLUDE_GROUP) or {}).get("hint") or ""
        ok("55on: AI on, descriptions kept by another model: the hint names the model that wrote them, not the configured one",
           "old-model" in hint and "new-model" not in hint, hint)
        tick("Descriptions written by AI")
        tick("An AI translation")
        hint = (ff.js(INCLUDE_GROUP) or {}).get("hint") or ""
        ok("55on: a kind nothing was kept for is written by the configured model: the hint names it",
           "new-model" in hint and "old-model" not in hint, hint)
        download()
        ff.wait_for("window.__dx.posts.length === 1", 5)
        body = (last_post() or {}).get("body", {})
        ok("55on: AI on: both options sent as ticked", body.get("ai_translations") is True and body.get("ai_descriptions") is False, body)
        reset()
        serve_plan(B, ai_plan(B, True, "new-model"))
        open_dialog(B_NAME)
        tick("Descriptions written by AI")
        hint = (ff.js(INCLUDE_GROUP) or {}).get("hint") or ""
        ok("55on: control, AI on with nothing kept: the hint names the configured model", "new-model" in hint, hint)

    # ── #75: the downloaded file keeps the letters of the flow's name ──────────────────────
    if case("75"):
        reset(exportHeader="attachment; filename=\"Qualit_citt_dbt_duckdb.zip\"; filename*=UTF-8''Qualit%C3%A0_citt%C3%A0_%CE%95%CE%BB%CE%BB%CE%AC%CE%B4%CE%B1_dbt_duckdb.zip")
        open_dialog(B_NAME)
        target("duckdb")
        download()
        ff.wait_for("window.__dx.dl.length === 1", 5)
        ok("75: the file name is the gateway's RFC 5987 filename*", ff.js("return window.__dx.dl") == ["Qualità_città_Ελλάδα_dbt_duckdb.zip"], ff.js("return window.__dx.dl"))
        reset(rename={str(B): "zz Qualità città Ελλάδα"})
        go("/datasources")
        go("/flows", "zz Qualità")
        open_dialog("zz Qualità città Ελλάδα")
        target("duckdb")
        download()
        ff.wait_for("window.__dx.dl.length === 1", 5)
        ok("75: without the header the name still keeps accented and non-Latin letters",
           ff.js("return window.__dx.dl") == ["zz_Qualità_città_Ελλάδα_dbt_duckdb.zip"], ff.js("return window.__dx.dl"))
        reset()
        go("/datasources")
        go("/flows", A_NAME)

    # ── #71: focus goes back to the row's Export button ────────────────────────────────────
    def by_keyboard(name):
        close_if_open()
        ff.js("""
          for (const b of document.querySelectorAll('[data-zzdx]')) delete b.dataset.zzdx;
          const az = Array.from(document.querySelectorAll('.flow-actions')).find(a => a.parentElement.innerText.includes(arguments[0]));
          const b = az.querySelector('button svg.lucide-download').closest('button'); b.dataset.zzdx = '1'; b.focus(); return 1""", name)
        ff.keys(ENTER)
        assert ff.wait_for("!!document.querySelector('[role=dialog] .export-body')", 10), "menu"
        for _ in range(6):
            if ff.js("return !!document.activeElement?.classList.contains('export-opt') && document.activeElement.innerText.includes('dbt')"):
                break
            ff.keys(TAB)
        ff.keys(ENTER)
        assert ff.wait_for("!!document.querySelector('.dx-card input[name=dx-target]')", 30), "dbt dialog by keyboard"
        time.sleep(0.3)

    ACTIVE = "return {tag: document.activeElement?.tagName, export: document.activeElement?.dataset?.zzdx === '1'}"
    if case("71"):
        reset()
        by_keyboard(A_NAME)
        inside = ff.js("return document.querySelector('.dx-card').contains(document.activeElement)")
        ff.keys(ESC)
        ff.wait_for("!document.querySelector('.dx-card')", 5)
        time.sleep(0.3)
        a = ff.js(ACTIVE)
        ok("71: Esc closes the dbt dialog and focus is back on the row's Export button", inside and a["export"], a)
        by_keyboard(B_NAME)
        ff.js("Array.from(document.querySelectorAll('.dx-card .dx-actions button')).find(b => !b.classList.contains('primary')).focus(); return 1")
        ff.keys(ENTER)
        ff.wait_for("!document.querySelector('.dx-card')", 5)
        time.sleep(0.3)
        a = ff.js(ACTIVE)
        ok("71: Cancel (Enter) also gives focus back to the row's Export button", a["export"], a)
        # control: the export menu alone still gives focus back
        close_if_open()
        ff.js("const b = document.querySelector('[data-zzdx]'); b.focus(); return 1")
        ff.keys(ENTER)
        ff.wait_for("!!document.querySelector('[role=dialog] .export-body')", 10)
        ff.keys(ESC)
        ff.wait_for("!document.querySelector('.export-body')", 5)
        time.sleep(0.3)
        ok("71: control, the export menu alone gives focus back as before", ff.js(ACTIVE)["export"], ff.js(ACTIVE))
        # control: another dialog of the same composable (Schedule) is unchanged
        ff.js("""
          for (const b of document.querySelectorAll('[data-zzdx]')) delete b.dataset.zzdx;
          const az = Array.from(document.querySelectorAll('.flow-actions')).find(a => a.parentElement.innerText.includes(arguments[0]));
          const b = az.querySelector('button svg.lucide-calendar-clock').closest('button'); b.dataset.zzdx = '1'; b.focus(); return 1""", B_NAME)
        ff.keys(ENTER)
        opened = ff.wait_for("!!document.querySelector('[role=dialog]') && document.querySelector('[role=dialog]').contains(document.activeElement)", 10)
        ff.keys(ESC)
        ff.wait_for("!document.querySelector('[role=dialog]')", 5)
        time.sleep(0.3)
        ok("71: control, the Schedule dialog still gives focus back to its button", opened and ff.js(ACTIVE)["export"], ff.js(ACTIVE))

    # ── a real download, through the real gateway and engine (no AI: the plan has none) ───
    if case("real"):
        reset(realExport=True)
        open_dialog(A_NAME)
        target("duckdb")
        download()
        done = ff.wait_for("window.__dx.dl.length === 1 && !document.querySelector('.dx-card')", 120)
        ok("real: a real export downloads, named after the flow, and closes the dialog", done and ff.js("return window.__dx.dl") == ["Margin_by_Category_and_Month_dbt_duckdb.zip"],
           {"dl": ff.js("return window.__dx.dl"), "state": state()})
finally:
    try:
        ff.js("for (const k of Object.keys(localStorage)) if (k.startsWith('tabularia.dbtExport.')) localStorage.removeItem(k); return 1")
    except Exception:  # noqa: BLE001
        pass
    stop(proc)
    try:
        api("POST", "/auth/logout", token=token)
    except urllib.error.HTTPError:
        pass

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
