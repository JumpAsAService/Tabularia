"""Browser check of the three SFTP forms on the dev stack (frontend :3000, gateway :8000):
the connection dialog (type SFTP, test → host key shown, create → that key accepted),
the datasource dialog («Check files», create, import), and the Output node's panel.

Needs the objects of `api_proofs.py main` (folder, connection «drop», flow «Export SFTP»)
and the throwaway SFTP server. Firefox headless with Marionette, the helper of
e2e/dbt_export (imported, not changed). The profile lives under ~/snap/firefox/common
(Firefox from snap reads nothing else).

    ADMIN_EMAIL=… ADMIN_PASSWORD=… SFTP_PASSWORD=… python3 e2e/sftp/browser_forms.py <folder id>
"""
import json, os, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dbt_export"))
from marionette import Firefox, ferma  # noqa: E402

UI = os.environ.get("UI", "http://localhost:3000")
API = os.environ.get("API", "http://localhost:8000")
SHOTS = os.environ.get("SHOTS", "/tmp")
PROFILE = os.path.expanduser("~/snap/firefox/common/zz-sftp-forms")
PID = int(sys.argv[1])
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
conns = api("GET", f"/projects/{PID}/connections", token=token)
flows = api("GET", f"/projects/{PID}/flows", token=token)


def write(sel, value, root="document"):
    return ff.js(f"""const el = {root}.querySelector({json.dumps(sel)}); if (!el) return false;
      el.value = {json.dumps(value)}; el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}})); return true""")


def pick(trigger_js, label):
    """Opens a Select (its trigger found by `trigger_js`) and clicks the option `label`."""
    ff.js(f"const t = {trigger_js}; t.click(); return 1")
    ff.aspetta("document.querySelectorAll('.sel-panel .sel-opt').length > 0", 5)
    return ff.js(f"""const o = Array.from(document.querySelectorAll('.sel-panel .sel-opt')).find(o => o.innerText.trim().startsWith({json.dumps(label)}));
      if (!o) return Array.from(document.querySelectorAll('.sel-panel .sel-opt')).map(o => o.innerText.trim()); o.click(); return true""")


def field_trigger(card, label):
    """The Select trigger in the field whose label starts with `label`."""
    return f"""Array.from(document.querySelectorAll('{card} label')).find(l => l.innerText.trim().startsWith({json.dumps(label)}))
      .parentElement.querySelector('.sel-trigger')"""


os.makedirs(PROFILE, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "--window-size", "1500,1000", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.invia("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1500, "height": 1000})
    ff.vai(f"{UI}/favicon.svg")
    ff.js("localStorage.clear(); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    ff.entra(UI, token)
    ff.vai(f"{UI}/?folder={PID}")
    ok("the folder opens in Explore", ff.aspetta("document.body.innerText.includes('drop')", 30))

    print("— connection dialog: new SFTP connection")
    ff.js("Array.from(document.querySelectorAll('button.btn-link')).find(b => b.innerText.includes('New')).click(); return 1")
    ff.aspetta("!!document.querySelector('.menu-pop')", 5)
    ff.js("Array.from(document.querySelectorAll('.menu-pop .menu-item')).find(b => b.querySelector('svg.lucide-plug')).click(); return 1")
    ok("the dialog opens", ff.aspetta("!!document.querySelector('.cd-card')", 10))
    write(".cd-card input[type=text]", "drop-ui")
    ok("type SFTP can be chosen", pick(field_trigger(".cd-card", "Connection type"), "SFTP (files)") is True)
    ff.aspetta("!!document.querySelector('.cd-hostkey')", 5)
    ok("SFTP fields: sign-in method and host key panel", ff.js("return document.querySelector('.cd-card').innerText") and
       ff.aspetta("document.querySelector('.cd-card').innerText.includes('Sign in with') && document.querySelector('.cd-hostkey.none') !== null", 5),
       ff.js("return document.querySelector('.cd-card').innerText"))
    write(".cd-card input[placeholder='sftp.example.com']", "zzsftp-a")
    write(".cd-card input[placeholder='22']", "2222")
    ff.js("""const f = Array.from(document.querySelectorAll('.cd-card .cd-field')).find(f => f.querySelector('label').innerText.trim().startsWith('User'));
      const i = f.querySelector('input'); i.value = 'tab'; i.dispatchEvent(new Event('input', {bubbles: true})); return 1""")
    write(".cd-card input[type=password]", os.environ["SFTP_PASSWORD"])
    ff.foto(os.path.join(SHOTS, "sftp-1-connection-form.png"))
    ff.js("Array.from(document.querySelectorAll('.cd-actions button')).find(b => b.innerText.includes('Test')).click(); return 1")
    ok("the test shows the server's key, to compare, with an Accept button", ff.aspetta("!!document.querySelector('.cd-pending .cd-accept')", 30),
       ff.js("return (document.querySelector('.cd-test')||{innerText:'no result'}).innerText"))
    shown = ff.js("return (document.querySelector('.cd-pending')||{innerText:''}).innerText.match(/SHA256:[A-Za-z0-9+\\/]+/)?.[0]")
    ok("…and says nothing was sent yet", ff.js("return document.querySelector('.cd-pending').innerText.includes('Nothing has been sent')"))
    ff.foto(os.path.join(SHOTS, "sftp-2a-connection-key-shown.png"))
    ff.js("document.querySelector('.cd-pending .cd-accept').click(); return 1")
    ok("accepting it tests the login with that key", ff.aspetta("(document.querySelector('.cd-test.ok')||{innerText:''}).innerText.includes('stored when you save')", 30),
       ff.js("return (document.querySelector('.cd-test')||{innerText:'no result'}).innerText"))
    ff.foto(os.path.join(SHOTS, "sftp-2b-connection-key-accepted.png"))
    ff.js("document.querySelector('.cd-actions button.primary').click(); return 1")
    ok("create closes the dialog", ff.aspetta("!document.querySelector('.cd-card')", 20), ff.js("return (document.querySelector('.cd-err')||{innerText:''}).innerText"))
    time.sleep(1)
    created = next((c for c in api("GET", f"/projects/{PID}/connections", token=token) if c["name"] == "drop-ui"), None)
    ok("the saved connection carries exactly the key the test showed", created and shown and created["host_key_fingerprint"] == shown,
       f"{created} / {shown}")

    print("— connection dialog: editing it")
    ff.vai(f"{UI}/?folder={PID}")
    ff.aspetta("document.body.innerText.includes('drop-ui')", 20)
    ff.js("""const row = Array.from(document.querySelectorAll('tr')).find(r => r.innerText.includes('drop-ui'));
      row.querySelector('button svg.lucide-pencil').closest('button').click(); return 1""")
    ff.aspetta("!!document.querySelector('.cd-card')", 10)
    ok("the accepted key is shown", ff.aspetta(f"(document.querySelector('.cd-hostkey.ok')||{{innerText:''}}).innerText.includes({json.dumps(shown or '?')})", 5))
    ff.js("Array.from(document.querySelectorAll('.cd-actions button')).find(b => b.innerText.includes('Test')).click(); return 1")
    ok("testing the saved connection verifies the accepted key", ff.aspetta("(document.querySelector('.cd-test.ok')||{innerText:''}).innerText.includes('presented the accepted key')", 30),
       ff.js("return (document.querySelector('.cd-test')||{innerText:'no result'}).innerText"))
    write(".cd-card input[placeholder='22']", "2200")
    ok("a new port asks for the password again", ff.aspetta("document.querySelector('.cd-card').innerText.includes('typed again') && document.querySelector('.cd-actions button.primary').disabled", 5))
    write(".cd-card input[placeholder='22']", "2222")
    pick(field_trigger(".cd-card", "Sign in with"), "Private key")
    ok("the key method shows the key and passphrase fields", ff.aspetta("!!document.querySelector('.cd-card textarea.cd-key')", 5))
    ff.foto(os.path.join(SHOTS, "sftp-3-connection-edit.png"))
    ff.js("Array.from(document.querySelectorAll('.cd-actions button')).find(b => b.innerText.trim() === 'Cancel').click(); return 1")

    print("— datasource dialog")
    ff.js("Array.from(document.querySelectorAll('button.btn-link')).find(b => b.innerText.includes('New')).click(); return 1")
    ff.aspetta("!!document.querySelector('.menu-pop')", 5)
    ff.js("Array.from(document.querySelectorAll('.menu-pop .menu-item')).find(b => b.querySelector('svg.lucide-database')).click(); return 1")
    ff.aspetta("!!document.querySelector('.dd-card')", 10)
    write(".dd-card input[type=text]", "vendite dal browser")
    pick("Array.from(document.querySelectorAll('.dd-card label')).find(l => l.innerText.startsWith('Connection')).nextElementSibling", "drop-ui")
    ok("the SFTP fields appear", ff.aspetta("document.querySelector('.dd-card').innerText.includes('Folder on the server')", 5))
    write(".dd-card input.dd-mono", "in")
    write(".dd-card input[placeholder='vendite_*.csv']", "vendite_*")
    ff.js("Array.from(document.querySelectorAll('.dd-card button')).find(b => b.innerText.includes('Check files')).click(); return 1")
    ok("«Check files» lists the three files by name", ff.aspetta("document.querySelectorAll('.dd-files li').length === 3", 20),
       ff.js("return document.querySelector('.dd-card').innerText"))
    listed = ff.js("return Array.from(document.querySelectorAll('.dd-files li')).map(l => l.innerText.trim())")
    ok("names below the folder, as `_file` will hold them", listed == ["vendite_01.csv", "vendite_02.xlsx", "vendite_03.parquet"], listed)
    ff.foto(os.path.join(SHOTS, "sftp-4-datasource-form.png"))
    ff.js("document.querySelector('.dd-actions button.primary').click(); return 1")
    ok("create closes the dialog", ff.aspetta("!document.querySelector('.dd-card')", 20), ff.js("return (document.querySelector('.dd-err')||{innerText:''}).innerText"))
    end = time.time() + 60
    ds = None
    while time.time() < end:
        ds = next((d for d in api("GET", f"/projects/{PID}/datasources", token=token) if d["name"] == "vendite dal browser"), None)
        if ds and ds.get("rows"):
            break
        time.sleep(2)
    ok("its import ran: 5 rows", ds and ds.get("rows") == 5, ds)

    print("— Output node panel")
    flow = next(f for f in flows if f["name"] == "Export SFTP")
    ff.vai(f"{UI}/editor?flow={flow['id']}")
    ok("the editor shows the SFTP Output node", ff.aspetta("(document.querySelector('.node-output')||{innerText:''}).innerText.includes('SFTP output')", 40),
       ff.js("return (document.querySelector('.node-output')||{innerText:'no node'}).innerText"))
    ok("…with its file name and format", ff.js("return document.querySelector('.node-output').innerText").find("vendite_{date}_{run_id} (xlsx)") >= 0,
       ff.js("return document.querySelector('.node-output').innerText"))
    ff.js("document.querySelector('.node-output').click(); return 1")
    ok("the panel shows destination SFTP with folder, file name and format",
       ff.aspetta("Array.from(document.querySelectorAll('input')).some(i => i.value === 'vendite_{date}_{run_id}') && document.body.innerText.includes('SFTP server (file)')", 10))
    ok("the placeholders are explained", ff.js("return document.body.innerText.includes('{run_id} the run') && document.body.innerText.includes('{datetime} 2026-10-17_143005')"))
    ff.foto(os.path.join(SHOTS, "sftp-5-output-panel.png"))
finally:
    ferma(proc)

print(f"\n{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
