"""Un Microsoft Graph finto, fedele al contratto che usiamo: token con client
credentials, risoluzione del sito, raccolte, figli di una cartella (paginati),
download del contenuto, throttling con Retry-After. In sviluppo non c'è un tenant
vero: questo è ciò contro cui il connettore SharePoint viene provato."""
import io
import json
from urllib.parse import unquote

import xlsxwriter

GRAPH = "https://graph.test/v1.0"
LOGIN = "https://login.test"


def xlsx(rows, sheet="Dati") -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet(sheet)
    for i, r in enumerate(rows):
        for j, v in enumerate(r):
            ws.write(i, j, v)
    wb.close()
    return buf.getvalue()


class _Resp:
    def __init__(self, status=200, body=None, content=b"", headers=None):
        self.status_code = status; self._body = body; self.content = content; self.headers = headers or {}
    def json(self):
        if self._body is None: raise ValueError("no json")
        return self._body
    def iter_content(self, n):
        for i in range(0, len(self.content), n): yield self.content[i:i + n]


class FakeGraph:
    def __init__(self, files: dict[str, bytes], *, site_path="/sites/Finance", libraries=("Documenti",), page_size=100,
                 secret="s3gr3t0", granted=True, modified="2026-09-01T08:30:00Z"):
        self.files = dict(files); self.site_path = site_path; self.libraries = list(libraries)
        self.page_size = page_size; self.secret = secret; self.granted = granted; self.modified = modified
        self.calls: list[tuple[str, str]] = []; self.tokens = 0; self.throttle_next = 0

    # -- il "filesystem" --
    def _children(self, folder: str) -> list[dict]:
        # l'indirizzamento per percorso di SharePoint NON distingue maiuscole e minuscole
        folder = folder.strip("/"); pre = folder + "/" if folder else ""
        out: dict[str, dict] = {}
        for path, data in self.files.items():
            if not path.casefold().startswith(pre.casefold()): continue
            rest = path[len(pre):]
            name = rest.split("/")[0]
            ref = {"parentReference": {"path": "/drives/DRV/root:/" + path[:len(pre)].strip("/")}}
            if "/" in rest: out.setdefault(name, {"id": "d:" + pre + name, "name": name, "folder": {}, **ref})
            else: out[name] = {"id": "f:" + path, "name": name, "file": {}, "size": len(data),
                               "lastModifiedDateTime": self.modified, "eTag": "etag-" + name, **ref}
        return [out[k] for k in sorted(out)]

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if url.startswith(LOGIN):
            self.tokens += 1
            if kw.get("data", {}).get("client_secret") != self.secret:
                return _Resp(401, {"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: x"})
            return _Resp(200, {"access_token": "tok", "expires_in": 3600})
        assert kw.get("headers", {}).get("Authorization") == "Bearer tok", "chiamata Graph senza token"
        if self.throttle_next:
            self.throttle_next -= 1
            return _Resp(429, {"error": {"code": "TooManyRequests", "message": "throttled"}}, headers={"Retry-After": "1"})
        path = unquote(url[len(GRAPH):])
        if not self.granted:
            return _Resp(403, {"error": {"code": "accessDenied", "message": "Access denied"}})
        if path.startswith("/sites/contoso.sharepoint.com:"):
            return _Resp(200, {"id": "SITE1"}) if path.endswith(":" + self.site_path) else _Resp(404, {"error": {"code": "itemNotFound", "message": "not found"}})
        if path == "/sites/SITE1/drive": return _Resp(200, {"id": "DRV-" + self.libraries[0]})
        if path == "/sites/SITE1/drives": return _Resp(200, {"value": [{"id": "DRV-" + n, "name": n} for n in self.libraries]})
        if "/root" in path and path.split("?")[0].endswith("/children"):
            base, _, query = path.partition("?")
            folder = base.split("/root:/", 1)[1].rsplit(":/children", 1)[0] if "/root:/" in base else ""
            items = self._children(folder)
            if folder and not items:
                return _Resp(404, {"error": {"code": "itemNotFound", "message": "folder not found"}})
            start = int(query.split("skip=")[1]) if "skip=" in query else 0
            page = {"value": items[start:start + self.page_size]}
            if start + self.page_size < len(items): page["@odata.nextLink"] = f"{GRAPH}{base}?skip={start + self.page_size}"
            return _Resp(200, page)
        if "/items/" in path and path.endswith("/content"):
            item = path.split("/items/")[1][:-len("/content")]
            return _Resp(200, content=self.files[item[2:]])
        return _Resp(404, {"error": {"code": "itemNotFound", "message": "unknown " + path}})
