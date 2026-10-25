"""Browser check of the two «HTTP API» forms on the dev stack (frontend :3000, gateway :8000):
the connection dialog (type HTTP API, authentication fields, test, create, edit that moves
the host) and the datasource dialog (the call, «Try the call» with its table, create,
import), in English and in Italian.

Needs the folder of `api_proofs.py main` and the fake API (container zzapi-fake).
Firefox headless with Marionette, the helper of e2e/dbt_export (imported, not changed).

    ADMIN_EMAIL=… ADMIN_PASSWORD=… python3 e2e/api/browser_forms.py <folder id>
"""
import json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dbt_export"))
from marionette import Firefox, stop  # noqa: E402

UI = os.environ.get("UI", "http://localhost:3000")
API = os.environ.get("API", "http://localhost:8000")
SHOTS = os.environ.get("SHOTS", "/tmp")
PROFILE = os.path.expanduser("~/snap/firefox/common/zz-api-forms")
PID = int(sys.argv[1])
KEY = "zz-api-key-123456"
results = []


def ok(name, cond, detail=""):
    results.append(bool(cond))
    print(("  ok  " if cond else "  KO  ") + name + ("" if cond else f" — {str(detail)[:500]}"), flush=True)
    return bool(cond)


def api(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        t = r.read()
        return json.loads(t) if t else None


token = api("POST", "/auth/login", {"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]})["access_token"]


def write(sel, value, root="document"):
    return ff.js(f"""const el = {root}.querySelector({json.dumps(sel)}); if (!el) return false;
      el.value = {json.dumps(value)}; el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}})); return true""")


def field_input(card, label, value, tag="input"):
    return ff.js(f"""const f = Array.from(document.querySelectorAll('{card} .cd-field')).find(f => f.querySelector('label').innerText.trim().startsWith({json.dumps(label)}));
      if (!f) return false; const i = f.querySelector('{tag}'); i.value = {json.dumps(value)}; i.dispatchEvent(new Event('input', {{bubbles: true}})); return true""")


def pick(trigger_js, label):
    ff.js(f"const t = {trigger_js}; t.click(); return 1")
    ff.wait_for("document.querySelectorAll('.sel-panel .sel-opt').length > 0", 5)
    return ff.js(f"""const o = Array.from(document.querySelectorAll('.sel-panel .sel-opt')).find(o => o.innerText.trim().startsWith({json.dumps(label)}));
      if (!o) return Array.from(document.querySelectorAll('.sel-panel .sel-opt')).map(o => o.innerText.trim()); o.click(); return true""")


def field_trigger(card, label):
    return f"""Array.from(document.querySelectorAll('{card} label')).find(l => l.innerText.trim().startsWith({json.dumps(label)}))
      .parentElement.querySelector('.sel-trigger')"""


def card_text(sel):
    return ff.js(f"return (document.querySelector({json.dumps(sel)})||{{innerText:''}}).innerText")


os.makedirs(PROFILE, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "--window-size", "1500,1100", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.send("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1500, "height": 1100})
    ff.go(f"{UI}/favicon.svg")
    ff.js("localStorage.clear(); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    ff.sign_in(UI, token)
    ff.go(f"{UI}/?folder={PID}")
    ok("the folder opens in Explore", ff.wait_for("document.body.innerText.includes('zz none')", 30))

    print("— connection dialog: new HTTP API connection")
    ff.js("Array.from(document.querySelectorAll('button.btn-link')).find(b => b.innerText.includes('New')).click(); return 1")
    ff.wait_for("!!document.querySelector('.menu-pop')", 5)
    ff.js("Array.from(document.querySelectorAll('.menu-pop .menu-item')).find(b => b.querySelector('svg.lucide-plug')).click(); return 1")
    ok("the dialog opens", ff.wait_for("!!document.querySelector('.cd-card')", 10))
    write(".cd-card input[type=text]", "broker-ui")
    ok("type HTTP API can be chosen", pick(field_trigger(".cd-card", "Connection type"), "HTTP API") is True)
    ok("API fields: base URL, authentication, key name and where it goes",
       ff.wait_for("document.querySelector('.cd-card').innerText.includes('Base URL') && document.querySelector('.cd-card').innerText.includes('Key name')"
                  " && document.querySelector('.cd-card').innerText.includes('Sent in')", 5), card_text(".cd-card"))
    ok("no port field for an API", not ff.js("return !!document.querySelector(\".cd-card input[placeholder='22'], .cd-card input[inputmode=numeric]\")"))
    write(".cd-card input[placeholder='https://api.example.com/v1']", "http://zzapi-a:8080/v1")
    field_input(".cd-card", "Key name", "X-Api-Key")
    write(".cd-card input[type=password]", KEY)
    field_input(".cd-card", "Test path", "/key-header/items")
    field_input(".cd-card", "Default headers", "Accept: application/json", tag="textarea")
    ff.shot(os.path.join(SHOTS, "api-1-connection-form.png"))
    ff.js("Array.from(document.querySelectorAll('.cd-actions button')).find(b => b.innerText.includes('Test')).click(); return 1")
    ok("the test succeeds", ff.wait_for("!!document.querySelector('.cd-test.ok')", 30), card_text(".cd-test"))
    pick(field_trigger(".cd-card", "Authentication"), "OAuth2")
    ok("OAuth2 shows client id, token URL, scope", ff.wait_for("['Client ID','Token URL','Scope','Client secret'].every(w => document.querySelector('.cd-card').innerText.includes(w))", 5),
       card_text(".cd-card"))
    pick(field_trigger(".cd-card", "Authentication"), "None")
    ok("None hides the secret", ff.wait_for("!document.querySelector('.cd-card input[type=password]')", 5))
    pick(field_trigger(".cd-card", "Authentication"), "API key")
    write(".cd-card input[type=password]", KEY)
    field_input(".cd-card", "Key name", "X-Api-Key")
    ff.js("document.querySelector('.cd-actions button.primary').click(); return 1")
    ok("create closes the dialog", ff.wait_for("!document.querySelector('.cd-card')", 20), card_text(".cd-err"))
    time.sleep(1)
    created = next((c for c in api("GET", f"/projects/{PID}/connections", token=token) if c["name"] == "broker-ui"), None)
    extra = json.loads(created["extra"]) if created else {}
    ok("saved with its options and the secret stored (never returned)",
       created and created["has_password"] and KEY not in json.dumps(created) and extra.get("key_name") == "X-Api-Key"
       and extra.get("headers") == {"Accept": "application/json"} and extra.get("test_path") == "/key-header/items", created)

    print("— connection dialog: editing it")
    ff.go(f"{UI}/?folder={PID}")
    ff.wait_for("document.body.innerText.includes('broker-ui')", 20)
    ff.js("""const row = Array.from(document.querySelectorAll('tr')).find(r => r.innerText.includes('broker-ui'));
      row.querySelector('button svg.lucide-pencil').closest('button').click(); return 1""")
    ff.wait_for("!!document.querySelector('.cd-card')", 10)
    ok("the form comes back filled", ff.js("return Array.from(document.querySelectorAll('.cd-card input')).some(i => i.value === 'X-Api-Key')"))
    write(".cd-card input[placeholder='https://api.example.com/v1']", "http://zzapi-b:8080/v1")
    ok("another host asks for the secret again", ff.wait_for("document.querySelector('.cd-card').innerText.includes('typed again') && document.querySelector('.cd-actions button.primary').disabled", 5),
       card_text(".cd-card"))
    write(".cd-card input[placeholder='https://api.example.com/v1']", "http://zzapi-a:8080/v2")
    ok("another path on the same host does not", ff.wait_for("!document.querySelector('.cd-card').innerText.includes('typed again') && !document.querySelector('.cd-actions button.primary').disabled", 5))
    ff.shot(os.path.join(SHOTS, "api-2-connection-edit.png"))
    ff.js("Array.from(document.querySelectorAll('.cd-actions button')).find(b => b.innerText.trim() === 'Cancel').click(); return 1")

    print("— datasource dialog")
    ff.js("Array.from(document.querySelectorAll('button.btn-link')).find(b => b.innerText.includes('New')).click(); return 1")
    ff.wait_for("!!document.querySelector('.menu-pop')", 5)
    ff.js("Array.from(document.querySelectorAll('.menu-pop .menu-item')).find(b => b.querySelector('svg.lucide-database')).click(); return 1")
    ff.wait_for("!!document.querySelector('.dd-card')", 10)
    write(".dd-card input[type=text]", "quotes from the browser")
    pick("Array.from(document.querySelectorAll('.dd-card label')).find(l => l.innerText.startsWith('Connection')).nextElementSibling", "broker-ui")
    ok("the call fields appear and the dialog widens", ff.wait_for("!!document.querySelector('.dd-card.wide .ac')", 5), card_text(".dd-card"))
    ok("no table, SQL or sort keys for an API", not ff.js("return document.querySelector('.dd-card').innerText.includes('Sort keys') || !!document.querySelector('.dd-mode')"))
    write(".ac input[placeholder='/v1/rates/{today-30}..']", "/quote/{symbol}")
    write(".ac textarea.dyn-variables", "symbol = AAPL, MSFT")
    write(".ac input[placeholder='data.items']", "prices")
    ff.js("document.querySelector('.ac-check input').click(); return 1")
    ff.js("document.querySelector('.ac-try button').click(); return 1")
    ok("«Try the call» shows the request, the status and a table of the first rows",
       ff.wait_for("document.querySelectorAll('.ac-table tbody tr').length === 3", 30), card_text(".ac-result") or card_text(".ac-err"))
    ok("…the request resolved, with the first value of the variable",
       "/v1/quote/AAPL" in card_text(".ac-req") and "2 calls per refresh" in card_text(".ac-result"), card_text(".ac-result"))
    ok("…the key masked", KEY not in card_text(".ac-result") and "X-Api-Key: ***" in card_text(".ac-headers"), card_text(".ac-headers"))
    heads = ff.js("return Array.from(document.querySelectorAll('.ac-table th')).map(t => t.innerText.trim())")
    ok("…columns _key first, the variable last", heads == ["_key", "close", "_param_symbol"], heads)
    ff.shot(os.path.join(SHOTS, "api-3-datasource-try.png"))
    pick("Array.from(document.querySelectorAll('.ac-box')).find(b => b.innerText.startsWith('Each refresh')).querySelector('.sel-trigger')", "adds the new rows")
    ok("append asks for the key columns, and creating waits for them",
       ff.wait_for("document.querySelector('.ac').innerText.includes('Key columns') && document.querySelector('.dd-actions button.primary').disabled", 5))
    write(".ac input[placeholder='_key, _param_symbol']", "_key, _param_symbol")
    ff.shot(os.path.join(SHOTS, "api-4-datasource-append.png"))
    ff.js("document.querySelector('.dd-actions button.primary').click(); return 1")
    ok("create closes the dialog", ff.wait_for("!document.querySelector('.dd-card')", 20), card_text(".dd-err"))
    end, ds = time.time() + 60, None
    while time.time() < end:
        ds = next((d for d in api("GET", f"/projects/{PID}/datasources", token=token) if d["name"] == "quotes from the browser"), None)
        if ds and ds.get("rows"):
            break
        time.sleep(2)
    ref = json.loads(ds["source_ref"]) if ds else {}
    ok("its import ran: 6 rows, append with the keys", ds and ds.get("rows") == 6 and ref.get("mode") == "append"
       and ref.get("keys") == ["_key", "_param_symbol"] and ref["response"]["keyed"] is True, ds)

    print("— in Italian")
    ff.js("document.cookie = 'tabularia-locale=it; path=/; max-age=86400; samesite=lax'; localStorage.clear(); return 1")
    ff.go(f"{UI}/?folder={PID}")
    ff.wait_for("document.body.innerText.includes('broker-ui')", 20)
    ff.js("Array.from(document.querySelectorAll('button.btn-link')).find(b => b.innerText.includes('Nuov')).click(); return 1")
    ff.wait_for("!!document.querySelector('.menu-pop')", 5)
    ff.js("Array.from(document.querySelectorAll('.menu-pop .menu-item')).find(b => b.querySelector('svg.lucide-database')).click(); return 1")
    ff.wait_for("!!document.querySelector('.dd-card')", 10)
    pick("Array.from(document.querySelectorAll('.dd-card label'))[1].nextElementSibling", "broker-ui")
    ok("the call form in Italian", ff.wait_for("['Prova la chiamata','Paginazione','Variabili','Percorso dei record'].every(w => document.querySelector('.dd-card').innerText.includes(w))", 10),
       card_text(".dd-card"))
    ff.shot(os.path.join(SHOTS, "api-5-datasource-it.png"))
finally:
    stop(proc)

print(f"\n{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
