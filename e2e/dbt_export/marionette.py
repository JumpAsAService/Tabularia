"""Pilota Firefox con Marionette (TCP, non websocket): navigare, ASPETTARE che
la pagina abbia finito, e solo allora fotografare. `--screenshot` scatta al load
event, cioè prima che il client abbia risolto la sessione: le pagine sembrano
vuote anche quando non lo sono."""
import base64, json, socket, time


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
        self.invia("WebDriver:Navigate", {"url": url})

    def js(self, script):
        return self.invia("WebDriver:ExecuteScript", {"script": script, "args": []})["value"]

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

