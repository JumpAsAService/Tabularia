"""Drives Firefox with Marionette (TCP, not websocket): navigate, WAIT until the page
has finished, and only then take the picture. `--screenshot` shoots at the load event,
that is before the client has resolved the session: the pages look empty even when
they are not."""
import base64, json, os, socket, subprocess, time

# the port Marionette listens on (E2E_MARIONETTE_PORT): another one when 2828 is taken by a
# Firefox that is not ours
PORT = int(os.environ.get("E2E_MARIONETTE_PORT") or 2828)

# WebDriver key codes, for `keys`
ESC, TAB, ENTER = "", "", ""


class Firefox:
    def __init__(self, port=PORT, wait=40):
        deadline = time.time() + wait
        while True:
            try:
                self.s = socket.create_connection(("127.0.0.1", port), 5)
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.s.settimeout(120)
        self._read()          # unsolicited handshake
        self.id = 0
        self.send("WebDriver:NewSession", {})
        Firefox.last = self   # the session stop() asks to quit

    def _read(self):
        n = b""
        while not n.endswith(b":"):
            c = self.s.recv(1)
            if not c:
                raise IOError("connection closed")
            n += c
        rest = int(n[:-1])
        buf = b""
        while len(buf) < rest:
            buf += self.s.recv(rest - len(buf))
        return json.loads(buf)

    def send(self, command, params):
        self.id += 1
        body = json.dumps([0, self.id, command, params]).encode()
        self.s.sendall(str(len(body)).encode() + b":" + body)
        answer = self._read()
        if answer[2]:
            raise RuntimeError(f"{command}: {answer[2]}")
        return answer[3]

    def go(self, url):
        # The old way in, the token in the fragment of the page back from the SSO, is
        # gone (the session is an HttpOnly cookie the gateway writes): it becomes
        # `sign_in`, so the tests written before still work.
        if "/auth/callback#token=" in url:
            base, token = url.split("/auth/callback#token=", 1)
            self.sign_in(base, token)
            url = base + "/"
        self.send("WebDriver:Navigate", {"url": url})

    def sign_in(self, base, token):
        """Opens the session of `token` in this browser as the gateway opens it at
        sign-in: the HttpOnly cookie `tab_token` on the interface's site."""
        here = self.send("WebDriver:GetCurrentURL", {})
        if not (here["value"] if isinstance(here, dict) else here).startswith(base):
            self.send("WebDriver:Navigate", {"url": f"{base}/favicon.svg"})
        self.send("WebDriver:AddCookie", {"cookie": {
            "name": "tab_token", "value": token, "path": "/", "httpOnly": True, "sameSite": "Lax"}})

    def session_cookie(self):
        """The value of the session cookie (the page's JavaScript does not see it)."""
        answer = self.send("WebDriver:GetCookies", {})
        for c in answer["value"] if isinstance(answer, dict) else answer:
            if c["name"] == "tab_token":
                return c["value"]
        return None

    def js(self, script, *args):
        """`script` runs as a function body: `arguments[0]`… are `args` (JSON values)."""
        return self.send("WebDriver:ExecuteScript", {"script": script, "args": list(args)})["value"]

    def keys(self, *keys):
        """Real key presses, one after the other, on the element that has the focus."""
        actions = []
        for k in keys:
            actions += [{"type": "keyDown", "value": k}, {"type": "keyUp", "value": k}]
        self.send("WebDriver:PerformActions", {"actions": [{"type": "key", "id": "keyboard", "actions": actions}]})
        self.send("WebDriver:ReleaseActions", {})

    def wait_for(self, script, seconds=25):
        """Waits until `script` returns true. Returns False if time runs out."""
        end = time.time() + seconds
        while time.time() < end:
            try:
                if self.js("return " + script):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.4)
        return False

    def shot(self, path, full=False):
        """As a rule the VIEWPORT is shot, not the whole page: a page that scrolls
        would give frames of different heights and the montage would break."""
        data = self.send("WebDriver:TakeScreenshot", {"full": full})["value"]
        open(path, "wb").write(base64.b64decode(data))


def launch(profile, size="1500,1000"):
    """A headless Firefox with Marionette on PORT, on the profile `profile` (created if missing)."""
    os.makedirs(profile, exist_ok=True)
    prefs = os.path.join(profile, "user.js")
    lines = [l for l in (open(prefs).read().splitlines() if os.path.exists(prefs) else []) if "marionette.port" not in l]
    open(prefs, "w").write("\n".join(lines + [f'user_pref("marionette.port", {PORT});']) + "\n")
    return subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", profile, "--window-size", size, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop(proc, wait=20):
    """Closes Firefox and WAITS for it to exit. The tests all use the same port: if the next
    test starts while this Firefox is still closing, `Firefox()` hooks onto it instead
    of the new one (and the test ends up on another one's sign-in page).
    Firefox is asked to quit through Marionette first: the snap's Firefox does not take a
    signal from the process that started it (AppArmor, «Permission denied»), and a
    terminate() alone used to leave it running."""
    if proc.poll() is None:
        for session in (getattr(Firefox, "last", None), None):
            try:
                (session or Firefox(wait=2)).send("Marionette:Quit", {"flags": ["eForceQuit"]})
                break
            except (OSError, RuntimeError, ValueError, AttributeError):
                continue    # that session is gone: a new one, then the signal below
        Firefox.last = None
    try:
        proc.wait(timeout=wait)
        return
    except subprocess.TimeoutExpired:
        pass
    for signal_it in (proc.terminate, proc.kill):
        try:
            signal_it()
            proc.wait(timeout=wait)
            return
        except PermissionError:
            break
        except subprocess.TimeoutExpired:
            continue
    print(f"  ·  Firefox (pid {proc.pid}) did not exit: close it by hand", flush=True)
