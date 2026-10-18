"""Browser checks for the problems a getting-started run on a clean machine found
(release 1.2.0), on the dev stack (frontend :3000, gateway :8000):

1. a password can be changed: one's own from the settings menu (the current one
   is checked, the other sessions close, an SSO account is told its password lives
   at the identity provider) and another user's from Administration → Users;
2. top bar: nothing invisible swallows the clicks on the right of the page (the
   memory gauge took AppShell's scoped `.shell` style and became a full-height strip);
3. the English interface has no Italian left where the run found it (cron
   description, engine menu, flow version notes, search), and French follows;
4. the editor's status follows the canvas, leaving an unsaved new flow warns, the
   Flows page shows when a scheduled flow runs next.

Section 1 creates a throw-away user (zz-pw-…@example.com) and deletes it; for the
SSO case it empties that user's password in the dev Postgres (container
dataprep-postgres, credentials from infrastructure/.env).

Firefox headless with Marionette (the helper of e2e/dbt_export, imported, not
changed), on its own Marionette port so it does not take another check's Firefox.

    ADMIN_EMAIL=… ADMIN_PASSWORD=… python3 e2e/getting_started/browser_checks.py [section …]
"""
import json, os, subprocess, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dbt_export"))
from marionette import Firefox, ferma  # noqa: E402

UI = os.environ.get("UI", "http://localhost:3000")
API = os.environ.get("API", "http://localhost:8000")
SHOTS = os.environ.get("SHOTS", "/tmp")
PORT = int(os.environ.get("MARIONETTE_PORT", "2841"))
PROFILE = os.path.expanduser("~/snap/firefox/common/zz-getting-started-checks")
results = []


def ok(name, cond, detail=""):
    results.append(bool(cond))
    print(("  ok  " if cond else "  KO  ") + name + ("" if cond else f" — {str(detail)[:600]}"), flush=True)
    return bool(cond)


def api(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            t = r.read()
            return r.status, (json.loads(t) if t else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:400]


def login(email, password):
    st, body = api("POST", "/auth/login", {"email": email, "password": password})
    assert st == 200, (st, body)
    return body["access_token"]


def locale(code):
    ff.js(f"document.cookie = 'tabularia-locale={code}; path=/; max-age=86400; samesite=lax'; return 1")


def sign_in_as(token, code="en"):
    ff.vai(f"{UI}/favicon.svg")
    ff.js("localStorage.clear(); return 1")
    locale(code)
    ff.entra(UI, token)


# ── 1. passwords ─────────────────────────────────────────────────────────────

ENV = os.path.join(HERE, "..", "..", "infrastructure", ".env")


def env(k):
    return next(r.split("=", 1)[1].strip() for r in open(ENV) if r.startswith(k + "="))


def wait_for(cond, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.4)
    return False


def open_settings_item(label):
    # the gear is in the server's HTML before the page is live: click until the menu opens
    for _ in range(20):
        ff.js("document.querySelector('.settings .gear')?.click(); return 1")
        if ff.aspetta("!!document.querySelector('.settings .menu')", 1):
            break
    ff.aspetta(f"[...document.querySelectorAll('.settings .menu .menu-item')].some(b => b.innerText.trim() === {json.dumps(label)})", 10)
    return ff.js(f"""const b = [...document.querySelectorAll('.settings .menu .menu-item')].find(b => b.innerText.trim() === {json.dumps(label)});
      if (!b) return [...document.querySelectorAll('.settings .menu .menu-item')].map(b => b.innerText.trim()); b.click(); return true""")


def fill(label, value):
    return ff.js(f"""const f = [...document.querySelectorAll('.modal-card .pd-field')].find(f => f.querySelector('span').innerText.trim() === {json.dumps(label)});
      if (!f) return false; const i = f.querySelector('input'); i.value = {json.dumps(value)};
      i.dispatchEvent(new Event('input', {{bubbles: true}})); return true""")


def submit_dialog():
    ff.js("document.querySelector('.modal-card .modal-foot button.primary').click(); return 1")


def dialog_error():
    return ff.js("return (document.querySelector('.modal-card .pd-error') || {innerText: ''}).innerText.trim()")


def check_passwords():
    print("== passwords: one's own, and another user's")
    email, first = f"zz-pw-{int(time.time())}@example.com", "zz-first-password"
    st, user = api("POST", "/users", {"email": email, "password": first, "full_name": "zz password"}, admin)
    assert st == 201, (st, user)
    try:
        mine = login(email, first)
        elsewhere = login(email, first)  # another device
        sign_in_as(mine)
        ff.vai(f"{UI}/")
        ff.aspetta("!!document.querySelector('.settings .gear')", 30)
        # the gateway learns the language of the interface: the emails follow it
        ok("at sign-in the gateway learns the interface language",
           wait_for(lambda: api("GET", "/auth/me", token=mine)[1].get("locale") == "en"), api("GET", "/auth/me", token=mine)[1].get("locale"))
        for _ in range(20):
            ff.js("document.querySelector('.settings .gear')?.click(); return 1")
            if ff.aspetta("!!document.querySelector('.settings .menu')", 1):
                break
        ff.js("""const l = [...document.querySelectorAll('.settings .menu .menu-label')].find(l => l.innerText.trim() === 'Language');
          let n = l.nextElementSibling; (n.querySelector('.sel-trigger') || n).click(); return 1""")
        ff.aspetta("document.querySelectorAll('.sel-panel .sel-opt').length > 0", 5)
        ff.js("[...document.querySelectorAll('.sel-panel .sel-opt')].find(o => o.innerText.trim() === 'Français').click(); return 1")
        ok("choosing French in the menu tells the gateway",
           wait_for(lambda: api("GET", "/auth/me", token=mine)[1].get("locale") == "fr"), api("GET", "/auth/me", token=mine)[1].get("locale"))
        locale("en")
        ff.vai(f"{UI}/")
        ff.aspetta("!!document.querySelector('.settings .gear')", 30)
        ok("«Change password» is in the settings menu", open_settings_item("Change password") is True)
        ok("the dialog asks for the current password and the new one twice",
           ff.aspetta("document.querySelectorAll('.modal-card .pd-field').length === 3", 5))
        fill("Current password", "not-the-password"); fill("New password", "zz-second-password"); fill("Repeat the new password", "zz-second-password")
        submit_dialog()
        ff.aspetta("!!document.querySelector('.modal-card .pd-error')", 10)
        ok("a wrong current password is refused, in words", dialog_error() == "The current password is not correct.", dialog_error())
        ok("…and the page stays signed in", ff.js("return location.pathname") == "/")
        fill("Current password", first); fill("Repeat the new password", "zz-other")
        submit_dialog()
        ok("two different new passwords are caught before asking", dialog_error() == "The two new passwords do not match.", dialog_error())
        fill("New password", "short"); fill("Repeat the new password", "short")
        submit_dialog()
        ok("the length rule of every password applies (12 characters)", "at least 12 characters" in dialog_error(), dialog_error())
        fill("New password", "Change-me-2026"); fill("Repeat the new password", "Change-me-2026")
        submit_dialog()
        ok("a published password is refused before asking", "published" in dialog_error(), dialog_error())
        fill("New password", "zz-second-password"); fill("Repeat the new password", "zz-second-password")
        submit_dialog()
        ok("the right one changes it and closes the dialog", ff.aspetta("!document.querySelector('.modal-card')", 10))
        ff.foto(f"{SHOTS}/gs-password-changed.png")
        ok("the other device is signed out", api("GET", "/auth/me", token=elsewhere)[0] == 401)
        ff.vai(f"{UI}/flows")
        ok("this browser stays signed in", ff.aspetta("location.pathname === '/flows' && !!document.querySelector('.settings .gear')", 20))
        ok("the old password no longer signs in", api("POST", "/auth/login", {"email": email, "password": first})[0] == 401)
        ok("the new one does", api("POST", "/auth/login", {"email": email, "password": "zz-second-password"})[0] == 200)
        audit = api("GET", "/audit?action=auth.password_change&limit=5", token=admin)[1]
        ok("the change is in the audit", any(e.get("target_label") == email and e.get("outcome") == "success" for e in audit["items"]), audit)

        # an administrator sets it from Administration → Users
        sign_in_as(admin)
        ff.vai(f"{UI}/admin")
        ff.aspetta(f"document.body.innerText.includes({json.dumps(email)})", 30)
        clicked = ff.js(f"""const row = [...document.querySelectorAll('table.rows tbody tr')].find(r => r.innerText.includes({json.dumps(email)}));
          const b = row && [...row.querySelectorAll('button.mini')].find(b => b.getAttribute('aria-label') === 'Set a new password');
          if (!b) return false; b.click(); return true""")
        ok("each user row has «Set a new password»", clicked)
        ok("the administrator's dialog has no current-password field",
           ff.aspetta("document.querySelectorAll('.modal-card .pd-field').length === 2", 5))
        fill("New password", "zz-third-password"); fill("Repeat the new password", "zz-third-password")
        submit_dialog()
        ok("the administrator sets it", ff.aspetta("!document.querySelector('.modal-card')", 10))
        ok("the user signs in with it", api("POST", "/auth/login", {"email": email, "password": "zz-third-password"})[0] == 200)
        own_disabled = ff.js(f"""const row = [...document.querySelectorAll('table.rows tbody tr')].find(r => r.innerText.includes({json.dumps(os.environ['ADMIN_EMAIL'])}));
          const b = row && [...row.querySelectorAll('button.mini')].find(b => b.getAttribute('aria-label') === 'Set a new password');
          return b ? b.disabled : null""")
        ok("one's own row points to the settings menu instead", own_disabled is True, own_disabled)
        # the user-creation form checks the same rule before sending
        for bad, says in (("eleven-char", "at least 12 characters"), ("Change-me-2026", "published")):
            ff.js(f"""const f = document.querySelector('.card.form');
              const set = (sel, v) => {{ const i = f.querySelector(sel); i.value = v; i.dispatchEvent(new Event('input', {{bubbles: true}})) }};
              set('input[type=email]', 'zz-new@example.com'); set('input[type=password]', {json.dumps(bad)});
              [...f.querySelectorAll('button')].pop().click(); return 1""")
            ok(f"user creation refuses «{bad}» before sending", ff.aspetta(
                f"[...document.querySelectorAll('.toast')].some(t => t.innerText.includes({json.dumps(says)}))", 5),
                ff.js("return [...document.querySelectorAll('.toast')].map(t => t.innerText)"))
        ok("…and nothing was created", not any(u["email"] == "zz-new@example.com" for u in api("GET", "/users", token=admin)[1]))

        # an account that signs in through the identity provider
        sso = login(email, "zz-third-password")
        sql = f"UPDATE users SET hashed_password = NULL WHERE email = '{email}'"
        subprocess.run(["docker", "exec", "-e", f"PGPASSWORD={env('DB__PASSWORD')}", "dataprep-postgres", "psql", "-q",
                        "-U", env("DB__USER"), "-d", env("DB__NAME"), "-c", sql], check=True, capture_output=True)
        sign_in_as(sso)
        ff.vai(f"{UI}/")
        ff.aspetta("!!document.querySelector('.settings .gear')", 30)
        opened = open_settings_item("Change password")
        ok("an SSO account opens «Change password» too", ff.aspetta("!!document.querySelector('.modal-card')", 5), opened)
        text = ff.js("return document.querySelector('.modal-card').innerText")
        ok("an SSO account is told the identity provider manages its password",
           "identity provider" in text and not ff.js("return !!document.querySelector('.modal-card input[type=password]')"), text)
        ff.js("document.querySelector('.modal-card .modal-foot button').click(); return 1")
        sign_in_as(admin)
    finally:
        api("DELETE", f"/users/{user['id']}", token=admin)


# ── 2. the right column of the page: every point hits what is drawn there ────

def check_right_strip():
    print("== top bar: no invisible strip on the right")
    ff.invia("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1440, "height": 900})
    ff.vai(f"{UI}/")
    ok("Explore opens", ff.aspetta("!!document.querySelector('.topbar') && !!document.querySelector('.newwrap .btn-link')", 30))
    ff.aspetta("!!document.querySelector('.memgauge')", 15)
    gauge = ff.js("""const g = document.querySelector('.topbar .memgauge'); if (!g) return null;
      const r = g.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}""")
    ok("the memory gauge is in the top bar", gauge, gauge)
    if gauge:
        ok("the gauge is no taller than the top bar", gauge["h"] <= 52, gauge)
    # every point below the top bar, across the gauge's column, must not land on the gauge
    hits = ff.js("""const out = []; const g = document.querySelector('.topbar .memgauge');
      const r = g.getBoundingClientRect();
      for (let x = Math.floor(r.left); x <= Math.ceil(r.right); x += 4)
        for (let y = 60; y < innerHeight; y += 20) {
          const e = document.elementFromPoint(x, y);
          if (e && e.closest('.memgauge')) out.push([x, y]);
        }
      return out""")
    ok("below the top bar no point of the gauge's column hits the gauge", not hits, hits[:10])
    # the button the strip covered in the getting-started run: «New» → folder «Create»
    ff.js("document.querySelector('.newwrap .btn-link').click(); return 1")
    ff.aspetta("!!document.querySelector('.menu-pop .menu-row button.primary')", 5)
    ff.js("""const i = document.querySelector('.menu-pop .menu-row input'); i.value = 'zz';
      i.dispatchEvent(new Event('input', {bubbles: true})); return 1""")
    hit = ff.js("""const b = document.querySelector('.menu-pop .menu-row button.primary'); const r = b.getBoundingClientRect();
      const pts = [[r.left + r.width / 2, r.top + r.height / 2], [r.right - 3, r.top + r.height / 2], [r.left + 3, r.top + 3]];
      return pts.map(([x, y]) => { const e = document.elementFromPoint(x, y); return !!e && (e === b || b.contains(e)) })""")
    ok("the folder «Create» button gets the click everywhere on it", hit and all(hit), hit)
    ff.js("document.querySelector('.menu-backdrop')?.click(); return 1")
    ff.foto(f"{SHOTS}/gs-right-strip.png")


# ── 3. no Italian in the English interface ─────────────────────────────────────

ITALIAN = ("ogni", "alle", "solo", "lunedì", "creazione", "modifica", "promossa", "righe", "progetto",
           "configurato", "imposta", "motore", "legge", "Errore")


def italian_in(text):
    low = f" {text.lower()} "
    return [w for w in ITALIAN if f" {w.lower()} " in low or f" {w.lower()}," in low]


def check_no_italian():
    print("== English interface: no Italian left")
    flows = api("GET", "/flows", token=admin)[1]
    sched = next(f for f in flows if f["run_schedule"])
    ff.vai(f"{UI}/flows")
    ff.aspetta("document.querySelectorAll('.flow-row').length > 0", 30)
    # the schedule dialog's description of the cron
    ff.js(f"""const row = [...document.querySelectorAll('.flows > .flow')].find(r => r.querySelector('.fname').innerText.trim() === {json.dumps(sched['name'])});
      [...row.querySelectorAll('.flow-actions button')].find(b => b.getAttribute('aria-label') === 'Schedule run' || b.title.startsWith('Schedule')).click(); return 1""")
    ff.aspetta("!!document.querySelector('.sd-desc')", 10)
    desc = ff.js("return document.querySelector('.sd-desc').innerText")
    ok("the cron is described in English", desc and not italian_in(desc) and ("At" in desc or "Every" in desc), desc)
    ff.js("""const i = document.querySelector('.sd-card input[type=text]'); i.value = '0 6 * * 1';
      i.dispatchEvent(new Event('input', {bubbles: true})); return 1""")
    time.sleep(0.3)
    desc = ff.js("return document.querySelector('.sd-desc').innerText")
    ok("a typed cron too (06:00 on Monday)", "Monday" in desc and "06:00" in desc, desc)
    ff.js("document.querySelector('.sd-x').click(); return 1")
    # the next run of the scheduled flow
    badge = ff.js(f"""const row = [...document.querySelectorAll('.flows > .flow')].find(r => r.querySelector('.fname').innerText.trim() === {json.dumps(sched['name'])});
      const b = row.querySelector('.sched-badge'); return b ? [b.innerText.trim(), b.title] : null""")
    from datetime import datetime
    nxt = datetime.fromisoformat(sched["next_run_at"]).astimezone()  # this machine's time, as the browser's
    ok("a scheduled flow shows when it runs next", badge and badge[0].startswith("next run"), badge)
    ok("…in the browser's time zone", badge and f"{nxt.hour:02d}:{nxt.minute:02d}" in badge[0], (badge, nxt.isoformat()))
    # the versions of a flow: notes translated (older ones were written in Italian)
    ff.js(f"""const row = [...document.querySelectorAll('.flows > .flow')].find(r => r.querySelector('.fname').innerText.trim() === {json.dumps(sched['name'])});
      row.querySelector('.flow-head').click(); return 1""")
    ff.aspetta("document.querySelectorAll('.versions .ver').length > 0", 20)
    notes = ff.js("return [...document.querySelectorAll('.versions .vnote')].map(n => n.innerText.trim())")
    ok("the version notes are English", notes and not any(italian_in(n) for n in notes), notes)
    # the engine menu of a new flow
    ff.js("document.querySelector('.newflow .btn-link').click(); return 1")
    ff.aspetta("document.querySelectorAll('.newflow .menu-item').length > 0", 10)
    engines = ff.js("return [...document.querySelectorAll('.newflow .menu-item')].map(b => b.innerText.replace(/\\s+/g, ' ').trim())")
    bq = next((e for e in engines if e.startswith("BigQuery")), "")
    ok("the BigQuery entry is described in English", bq and not italian_in(bq) and "Google BigQuery" in bq, bq)
    ok("no engine is described in Italian", not any(italian_in(e) for e in engines), engines)
    ff.foto(f"{SHOTS}/gs-engine-menu-en.png")
    ff.js("document.querySelector('.newflow .menu-backdrop').click(); return 1")
    # the global search: «N rows», not «N righe»
    ds = next(d for d in api("GET", "/datasources", token=admin)[1] if d.get("rows"))
    ff.js(f"""const i = document.querySelector('.gs input'); i.focus(); i.value = {json.dumps(ds['name'])};
      i.dispatchEvent(new Event('input', {{bubbles: true}})); return 1""")
    ff.aspetta("!!document.querySelector('.gs .hdetail')", 10)
    details = ff.js("return [...document.querySelectorAll('.gs .hdetail')].map(d => d.innerText.trim())")
    ok("a datasource found by the search says its rows in English", any(d.endswith("rows") or d.endswith("row") for d in details)
       and not any(italian_in(d) for d in details), details)
    # French follows the interface
    locale("fr")
    ff.vai(f"{UI}/flows")
    ff.aspetta("document.querySelectorAll('.flow-row').length > 0", 30)
    ff.js(f"""const row = [...document.querySelectorAll('.flows > .flow')].find(r => r.querySelector('.fname').innerText.trim() === {json.dumps(sched['name'])});
      [...row.querySelectorAll('.flow-actions button')].find(b => b.querySelector('.lucide-calendar-clock')).click(); return 1""")
    ff.aspetta("!!document.querySelector('.sd-desc')", 10)
    ff.js("""const i = document.querySelector('.sd-card input[type=text]'); i.value = '0 6 * * 1';
      i.dispatchEvent(new Event('input', {bubbles: true})); return 1""")
    time.sleep(0.3)
    desc = ff.js("return document.querySelector('.sd-desc').innerText")
    ok("in French the cron is described in French", "lundi" in desc.lower(), desc)
    ff.js("document.querySelector('.sd-x').click(); return 1")
    locale("en")


# ── 4. a NEW flow: the status follows the canvas, and leaving it unsaved warns ──

def pick_datasource(name):
    """Select the source node, open the datasource picker in its panel, choose `name`."""
    ff.js("document.querySelector('.vue-flow__node-source').dispatchEvent(new MouseEvent('click', {bubbles: true})); return 1")
    ff.aspetta("!!document.querySelector('.nodepanel .sel-trigger')", 10)
    ff.js("""const t = document.querySelector('.nodepanel .sel-trigger');
      t.click(); return 1""")
    ff.aspetta("document.querySelectorAll('.sel-panel .sel-opt').length > 0", 10)
    return ff.js(f"""const o = [...document.querySelectorAll('.sel-panel .sel-opt')].find(o => o.innerText.trim().startsWith({json.dumps(name)}));
      if (!o) return [...document.querySelectorAll('.sel-panel .sel-opt')].map(o => o.innerText.trim()); o.click(); return true""")


def status_text():
    return ff.js("return (document.querySelector('.toolbar-inner .status') || {innerText: ''}).innerText.trim()")


def check_new_flow():
    print("== a new flow: status and the warning on leaving")
    ds = next(d for d in api("GET", "/datasources", token=admin)[1] if d.get("rows"))
    ff.vai(f"{UI}/editor?engine=polars&project={ds['project_id']}")
    ok("the editor opens", ff.aspetta("!!document.querySelector('.vue-flow__node-source')", 40))
    time.sleep(1)
    st = status_text()
    ok("an empty new flow asks for a datasource or a file", "datasource" in st.lower() and "file" in st.lower(), st)
    picked = pick_datasource(ds["name"])
    ok("a datasource is picked in the source node", picked is True, picked)
    ff.aspetta("!document.querySelector('.status')?.innerText.includes('file')", 10)
    st = status_text()
    ok("after the pick the status no longer asks for a file", "file" not in st.lower() and st, st)
    time.sleep(1)  # the change settles into the history
    ff.js("document.querySelector('a.navbtn[href=\"/\"]').click(); return 1")
    warned = ff.aspetta("[...document.querySelectorAll('.modal-card h2')].some(h => h.innerText.trim().length) && location.pathname === '/editor'", 8)
    ok("leaving the unsaved new flow asks first", warned, ff.js("return location.pathname"))
    ff.foto(f"{SHOTS}/gs-new-flow-leave.png")
    if warned:
        ff.js("[...document.querySelectorAll('.modal-card button')].find(b => b.classList.contains('danger')).click(); return 1")
        ok("«leave without saving» leaves", ff.aspetta("location.pathname === '/'", 10))
    # closing the tab or reloading: the browser's own warning is armed
    ff.vai(f"{UI}/editor?engine=polars&project={ds['project_id']}")
    ff.aspetta("!!document.querySelector('.vue-flow__node-source')", 40)
    time.sleep(1)
    pick_datasource(ds["name"])
    time.sleep(1)
    prevented = ff.js("const e = new Event('beforeunload', {cancelable: true}); window.dispatchEvent(e); return e.defaultPrevented")
    ok("reloading the unsaved new flow is held by the browser's warning", prevented, prevented)
    # leave for good, so the next section starts clean
    ff.js("window.onbeforeunload = null; return 1")


SECTIONS = {"password": check_passwords, "strip": check_right_strip, "italian": check_no_italian, "newflow": check_new_flow}

if __name__ == "__main__":
    wanted = sys.argv[1:] or list(SECTIONS)
    admin = login(os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"])
    subprocess.run(["rm", "-rf", PROFILE])
    os.makedirs(PROFILE)
    open(os.path.join(PROFILE, "user.js"), "w").write(f'user_pref("marionette.port", {PORT});\n')
    proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ff = Firefox(porta=PORT)
        ff.invia("WebDriver:SetWindowRect", {"x": 0, "y": 0, "width": 1440, "height": 900})
        sign_in_as(admin)
        for name in wanted:
            try:
                SECTIONS[name]()
            except Exception as e:  # a section that cannot go on is a failure, not the end of the others
                ok(f"section «{name}» ran to the end", False, e)
                sign_in_as(admin)
    finally:
        ferma(proc)
    print(f"\n{sum(results)}/{len(results)} ok")
    sys.exit(0 if all(results) else 1)
