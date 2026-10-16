"""Pilota Firefox con Marionette (TCP, non websocket): navigare, ASPETTARE che
la pagina abbia finito, e solo allora fotografare. `--screenshot` scatta al load
event, cioè prima che il client abbia risolto la sessione: le pagine sembrano
vuote anche quando non lo sono."""
import base64, json, socket, time

# WebDriver key codes, for `tasti`
ESC, TAB, ENTER = "\ue00c", "\ue004", "\ue007"


class Firefox:
    def __init__(self, porta=2828, attesa=40):
        scadenza = time.time() + attesa
        while True:
            try:
                self.s = socket.create_connection(("127.0.0.1", porta), 5)
                break
            except OSError:
                if time.time() > scadenza:
                    raise
                time.sleep(0.5)
        self.s.settimeout(120)
        self._leggi()          # handshake spontaneo
        self.id = 0
        self.invia("WebDriver:NewSession", {})

    def _leggi(self):
        n = b""
        while not n.endswith(b":"):
            c = self.s.recv(1)
            if not c:
                raise IOError("connessione chiusa")
            n += c
        resto = int(n[:-1])
        buf = b""
        while len(buf) < resto:
            buf += self.s.recv(resto - len(buf))
        return json.loads(buf)

    def invia(self, comando, parametri):
        self.id += 1
        corpo = json.dumps([0, self.id, comando, parametri]).encode()
        self.s.sendall(str(len(corpo)).encode() + b":" + corpo)
        risposta = self._leggi()
        if risposta[2]:
            raise RuntimeError(f"{comando}: {risposta[2]}")
        return risposta[3]

    def vai(self, url):
        # Il vecchio modo di entrare, il token nel frammento della pagina di ritorno
        # dall'SSO, non c'è più (la sessione è un cookie HttpOnly che scrive il
        # gateway): si traduce in `entra`, così le prove scritte prima funzionano.
        if "/auth/callback#token=" in url:
            base, token = url.split("/auth/callback#token=", 1)
            self.entra(base, token)
            url = base + "/"
        self.invia("WebDriver:Navigate", {"url": url})

    def entra(self, base, token):
        """Apre in questo browser la sessione di `token` come la apre il gateway
        all'accesso: il cookie HttpOnly `tab_token` sul sito dell'interfaccia."""
        qui = self.invia("WebDriver:GetCurrentURL", {})
        if not (qui["value"] if isinstance(qui, dict) else qui).startswith(base):
            self.invia("WebDriver:Navigate", {"url": f"{base}/favicon.svg"})
        self.invia("WebDriver:AddCookie", {"cookie": {
            "name": "tab_token", "value": token, "path": "/", "httpOnly": True, "sameSite": "Lax"}})

    def cookie_sessione(self):
        """Il valore del cookie della sessione (il JavaScript della pagina non lo vede)."""
        risposta = self.invia("WebDriver:GetCookies", {})
        for c in risposta["value"] if isinstance(risposta, dict) else risposta:
            if c["name"] == "tab_token":
                return c["value"]
        return None

    def js(self, script, *args):
        """`script` runs as a function body: `arguments[0]`… are `args` (JSON values)."""
        return self.invia("WebDriver:ExecuteScript", {"script": script, "args": list(args)})["value"]

    def tasti(self, *keys):
        """Real key presses, one after the other, on the element that has the focus."""
        azioni = []
        for k in keys:
            azioni += [{"type": "keyDown", "value": k}, {"type": "keyUp", "value": k}]
        self.invia("WebDriver:PerformActions", {"actions": [{"type": "key", "id": "tastiera", "actions": azioni}]})
        self.invia("WebDriver:ReleaseActions", {})

    def aspetta(self, script, secondi=25):
        """Aspetta che `script` torni vero. Torna False se scade."""
        fine = time.time() + secondi
        while time.time() < fine:
            try:
                if self.js("return " + script):
                    return True
            except RuntimeError:
                pass
            time.sleep(0.4)
        return False

    def foto(self, percorso, intera=False):
        """Di norma si fotografa il VIEWPORT, non la pagina intera: una pagina
        che scorre darebbe fotogrammi di altezza diversa e il montaggio si
        romperebbe."""
        dati = self.invia("WebDriver:TakeScreenshot", {"full": intera})["value"]
        open(percorso, "wb").write(base64.b64decode(dati))


def ferma(proc, attesa=20):
    """Chiude Firefox e ASPETTA che esca. Le prove usano tutte la porta 2828: se la
    prova dopo parte mentre questo Firefox è ancora in chiusura, `Firefox()` si aggancia
    a lui invece che al nuovo (e la prova finisce sulla pagina di login di un'altra)."""
    import subprocess
    proc.terminate()
    try:
        proc.wait(timeout=attesa)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

