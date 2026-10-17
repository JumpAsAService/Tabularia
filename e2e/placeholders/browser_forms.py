"""Browser check of the forms of the placeholders, ranges and «S3 folder» datasource
on the dev stack (frontend :3000, gateway :8000): the datasource dialog for S3, SFTP
and SharePoint (live previews, variables, «Check the files» with the missing values,
append that waits for its keys, create), the Output panel (time zone and previews),
the run's notes on the runs page, and the Italian labels.

Needs the objects of `api_proofs.py main` (its folder: connections «zz lake», «zz drop»,
flow «Giorni fuori») and the throwaway servers. Firefox headless with Marionette, the
helper of e2e/dbt_export (imported, not changed).

    ADMIN_EMAIL=… ADMIN_PASSWORD=… python3 e2e/placeholders/browser_forms.py <folder id>
"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dbt_export"))
from marionette import Firefox, ferma  # noqa: E402

UI = os.environ.get("UI", "http://localhost:3000")
API = os.environ.get("API", "http://localhost:8000")
SHOTS = os.environ.get("SHOTS", "/tmp")
PROFILE = os.path.expanduser("~/snap/firefox/common/zz-ph-forms")
PID = int(sys.argv[1])
results = []
today = datetime.now(timezone.utc).date()
d = lambda n: today - timedelta(days=n)  # noqa: E731


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
flows = api("GET", f"/projects/{PID}/flows", token=token)
for old in api("GET", f"/projects/{PID}/datasources", token=token):  # a second run starts clean
    if old["name"] == "s3 dal browser":
        api("DELETE", f"/datasources/{old['id']}", token=token)
# a SharePoint connection only to see its form (nothing is read through it)
if not any(c["name"] == "zz sp" for c in api("GET", f"/projects/{PID}/connections", token=token)):
    api("POST", f"/projects/{PID}/connections", {"name": "zz sp", "db_type": "sharepoint", "host": "https://contoso.sharepoint.com/sites/F",
                                                 "username": "client", "password": "x", "database": "tenant"}, token=token)


def write(sel, value, root="document"):
    return ff.js(f"""const el = {root}.querySelector({json.dumps(sel)}); if (!el) return false;
      el.value = {json.dumps(value)}; el.dispatchEvent(new Event('input', {{bubbles: true}})); el.dispatchEvent(new Event('change', {{bubbles: true}})); return true""")


def pick(trigger_js, label):
    ff.js(f"const t = {trigger_js}; t.click(); return 1")
    ff.aspetta("document.querySelectorAll('.sel-panel .sel-opt').length > 0", 5)
    return ff.js(f"""const o = Array.from(document.querySelectorAll('.sel-panel .sel-opt')).find(o => o.innerText.trim().startsWith({json.dumps(label)}));
      if (!o) return Array.from(document.querySelectorAll('.sel-panel .sel-opt')).map(o => o.innerText.trim()); o.click(); return true""")


def text(sel):
    return ff.js(f"return (document.querySelector({json.dumps(sel)})||{{innerText:''}}).innerText")


def open_dialog(conn_name, ds_name):
    ff.vai(f"{UI}/?folder={PID}")
    ff.aspetta("document.body.innerText.includes('zz lake')", 30)
    ff.js("Array.from(document.querySelectorAll('button.btn-link')).find(b => /New|Nuov/.test(b.innerText)).click(); return 1")
    ff.aspetta("!!document.querySelector('.menu-pop')", 5)
    ff.js("Array.from(document.querySelectorAll('.menu-pop .menu-item')).find(b => b.querySelector('svg.lucide-database')).click(); return 1")
    ff.aspetta("!!document.querySelector('.dd-card')", 10)
    write(".dd-card input[type=text]", ds_name)
    return pick("Array.from(document.querySelectorAll('.dd-card label'))[1].nextElementSibling", conn_name)


os.makedirs(PROFILE, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "--window-size", "1500,1100", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.invia("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1500, "height": 1100})
    ff.vai(f"{UI}/favicon.svg")
    ff.js("localStorage.clear(); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    ff.entra(UI, token)

    print("— datasource dialog: S3 folder")
    ok("the S3 connection can be chosen", open_dialog("zz lake", "s3 dal browser") is True)
    ok("the S3 fields appear, the bucket says the connection's default",
       ff.aspetta("!!document.querySelector('.dd-s3-bucket') && document.querySelector('.dd-s3-bucket').placeholder.includes('zzph-in')", 5),
       text(".dd-card"))
    write(".dd-card .dd-folder", "daily/{day}")
    write(".dd-card .dd-pattern", "sales.csv")
    write(".dd-card .dyn-variables", "day = today-3 .. today")
    ok("the folder's live preview: 3 values, first … last (the step defaults to 1d)",
       ff.aspetta(f"document.querySelector('[data-preview=folder]') && document.querySelector('[data-preview=folder]').innerText.includes('3 values')"
                  f" && document.querySelector('[data-preview=folder]').innerText.includes('daily/{d(3).isoformat()}')"
                  f" && document.querySelector('[data-preview=folder]').innerText.includes('daily/{d(1).isoformat()}')", 10),
       text("[data-preview=folder]"))
    ff.js("document.querySelector('.dd-check-files').click(); return 1")
    ok("«Check the files»: the two files and the missing day",
       ff.aspetta("document.querySelectorAll('.dd-files li').length >= 2 && !!document.querySelector('[data-missing]')", 30), text(".dd-card"))
    ok("…the missing value named", d(2).isoformat() in text("[data-missing]"), text("[data-missing]"))
    write(".dd-card .dyn-timezone", "Mars/Base")
    ok("a wrong time zone is said at once", ff.aspetta("(document.querySelector('[data-preview=global]')||{innerText:''}).innerText.includes('time zone')", 10),
       text(".dd-card"))
    write(".dd-card .dyn-timezone", "UTC")
    ff.foto(os.path.join(SHOTS, "ph-1-s3-dialog.png"))
    pick("document.querySelector('.dyn-mode .sel-trigger')", "adds the new rows")
    ok("append waits for its key columns", ff.aspetta("!!document.querySelector('.dyn-err') && document.querySelector('.dd-actions button.primary').disabled", 5))
    write(".dd-card .dyn-keys", "_param_day, id")
    ok("…and with them it can be created", ff.aspetta("!document.querySelector('.dd-actions button.primary').disabled", 5))
    ff.js("document.querySelector('.dd-actions button.primary').click(); return 1")
    ok("create closes the dialog", ff.aspetta("!document.querySelector('.dd-card')", 20), text(".dd-err"))
    end, ds = time.time() + 90, None
    while time.time() < end:
        ds = next((x for x in api("GET", f"/projects/{PID}/datasources", token=token) if x["name"] == "s3 dal browser"), None)
        if ds and ds.get("rows"):
            break
        time.sleep(2)
    ref = json.loads(ds["source_ref"]) if ds else {}
    ok("its import ran, saved with the range and the append keys", ds and ds.get("rows") == 4 and ref.get("mode") == "append"
       and ref.get("keys") == ["_param_day", "id"] and ref.get("variables", [{}])[0].get("step") == "1d", (ds, ref))

    print("— datasource dialog: SFTP folder and SharePoint")
    open_dialog("zz drop", "sftp dal browser")
    ff.aspetta("!!document.querySelector('.dd-pattern')", 5)
    write(".dd-card .dd-folder", "ph/in")
    write(".dd-card .dd-pattern", "sales_{day:yyyyMMdd}.csv")
    write(".dd-card .dyn-variables", "day = today-3 .. today step 1d")
    ok("the pattern's preview", ff.aspetta(f"(document.querySelector('[data-preview=pattern]')||{{innerText:''}}).innerText.includes('sales_{d(3):%Y%m%d}.csv')", 10),
       text("[data-preview=pattern]"))
    ff.js("document.querySelector('.dd-check-files').click(); return 1")
    ok("SFTP «Check the files» with the missing day", ff.aspetta("!!document.querySelector('[data-missing]')", 30) and d(2).isoformat() in text("[data-missing]"),
       text(".dd-card"))
    write(".dd-card .dd-pattern", "sales_{tomorrow}.csv")
    ok("an unknown placeholder is said under the field", ff.aspetta("(document.querySelector('[data-preview=pattern]')||{innerText:''}).innerText.includes('unknown placeholder')", 10),
       text("[data-preview=pattern]"))
    ff.foto(os.path.join(SHOTS, "ph-2-sftp-dialog.png"))
    open_dialog("zz sp", "sp dal browser")
    ff.aspetta("!!document.querySelector('.dd-sp-path')", 5)
    write(".dd-card .dd-sp-path", "Chiusure/{month:yyyy-MM}.xlsx")
    write(".dd-card .dyn-variables", "month = start_of_month-3m .. start_of_month step 1m")
    m3 = (today.replace(day=1) - timedelta(days=80)).replace(day=1)
    ok("SharePoint: the path's preview over the months", ff.aspetta("(document.querySelector('[data-preview=path]')||{innerText:''}).innerText.includes('3 values')", 10),
       text("[data-preview=path]"))
    ff.js("Array.from(document.querySelectorAll('.dd-actions button')).find(b => /Cancel|Annulla/.test(b.innerText)).click(); return 1")

    print("— Output panel")
    flow = next(f for f in flows if f["name"] == "Giorni fuori")
    ff.vai(f"{UI}/editor?flow={flow['id']}")
    ff.aspetta("document.querySelectorAll('.node-output').length >= 2", 40)
    ff.js("Array.from(document.querySelectorAll('.node-output')).find(n => n.innerText.includes('giorni')).click(); return 1")
    ok("the SFTP Output shows its zone and the resolved file name",
       ff.aspetta("!!document.querySelector('.out-timezone') && document.querySelector('.out-timezone').value === 'Pacific/Kiritimati'"
                  " && (document.querySelector('#tp-out-sftp-file-name')||{innerText:''}).innerText.includes('giorni_')", 15),
       text("#tp-out-sftp-file-name"))
    far = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Pacific/Kiritimati"))
    ok("…in that zone, {run_id} still to come", f"giorni_{far:%Y%m%d}_{{run_id}}" in text("#tp-out-sftp-file-name"), text("#tp-out-sftp-file-name"))
    ok("…and the dated folder", f"/config/ph/out/{far:%Y}/{far:%m}" in text("#tp-out-sftp-folder"), text("#tp-out-sftp-folder"))
    write(".out-sftp-file-name", "x_{today:yyyy/MM}")
    ok("a slash from a value is refused while typing", ff.aspetta("(document.querySelector('#tp-out-sftp-file-name')||{innerText:''}).innerText.includes(\"'/'\")", 10),
       text("#tp-out-sftp-file-name"))
    ff.foto(os.path.join(SHOTS, "ph-3-output-panel.png"))
    # leave the flow as it was: reload without saving
    ff.vai(f"{UI}/editor?flow={flow['id']}")
    ff.aspetta("document.querySelectorAll('.node-output').length >= 2", 40)
    ff.js("Array.from(document.querySelectorAll('.node-output')).find(n => n.innerText.includes('Giorni_fuori') || n.innerText.includes('flussi')).click(); return 1")
    ok("the S3 Output's key resolved", ff.aspetta(f"(document.querySelector('#tp-out-s3-key')||{{innerText:''}}).innerText.includes('Giorni_fuori_{far:%Y-%m-%d}.parquet')", 15),
       text("#tp-out-s3-key"))

    print("— the run's notes")
    ff.vai(f"{UI}/runs")
    ff.aspetta("document.querySelectorAll('.stpill').length > 0", 30)  # the first page (failures) loaded
    ff.js("Array.from(document.querySelectorAll('button')).find(b => b.innerText.trim() === 'Succeeded').click(); return 1")
    ok("the runs page shows the values without a file", ff.aspetta("!!document.querySelector('.run-note')", 30), ff.js("return document.body.innerText.slice(0, 600)"))
    ff.foto(os.path.join(SHOTS, "ph-4-runs.png"))

    print("— in Italian")
    ff.js("document.cookie = 'tabularia-locale=it; path=/; max-age=86400; samesite=lax'; localStorage.clear(); return 1")
    open_dialog("zz lake", "x")
    ff.aspetta("!!document.querySelector('.dyn-variables')", 10)
    ok("the dynamic section in Italian", ff.aspetta("['Variabili','Fuso orario'].every(w => document.querySelector('.dd-card').innerText.includes(w))", 10),
       text(".dd-card"))
    ff.foto(os.path.join(SHOTS, "ph-5-it.png"))
finally:
    ferma(proc)

print(f"\n{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
