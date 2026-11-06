"""The answers of the Python gateway's validate_url, siem.endpoint and urlsplit(...).hostname
for the addresses in URLS: run in the gateway's image, written to urls.json, read by
webhooks_test.go (python urls.py > urls.json). `idna`: httpx's encoding of each host that is
not ASCII it met (None for its InvalidURL), what the Go gateway asks of the AI service."""
import json
from urllib.parse import urlsplit

import httpx
from httpx import _urlparse

from app.services import siem, webhooks

_encode_host = _urlparse.encode_host
_met: dict = {}


def _recording(host):
    if host.isascii():
        return _encode_host(host)
    try:
        _met[host] = _encode_host(host)
    except httpx.InvalidURL:
        _met[host] = None
        raise
    return _met[host]


_urlparse.encode_host = _recording

URLS = [
    "https://example.com", "http://example.com/hook", "  https://Example.COM:8443/x?y#z  ", "ftp://example.com",
    "https://", "http:///path", "example.com", "//example.com", "https://user:pw@host.test/",
    "https://[::1]:8080/", "https://[::1", "https://::1]/", "https://[1.2.3.4]/", "https://[vfe.x]/", "https://[v1]/",
    "https://[fe80::1%25eth0]/", "https://x[::1]/", "https://[::1]x/", "https://[zz]/", "https://1.2.3.4/",
    "https://1.2.3.400/", "https://01.2.3.4/", "https://1.2.3/", "http://host:abc/", "http://host:0/", "http://host:65536/",
    "http://host:65535/", "http://host:+80/", "http://host:8_0/", "http://host:-1/", "https://host:443/", "http://host: 80/",
    "http://host:/", "http://a..b/", "http://.a/", "http://a./", "http://" + "a" * 63 + ".b/", "http://" + "a" * 64 + ".b/",
    "http://b." + "a" * 64 + "/", "http://b." + "a" * 63 + "/", "http://host\tname/", "http://host/pa\x00th",
    "http://host/pa\x7fth", "http://ho st/", "http://ho%20st/", "http://HOST.test/Path", "https://host.test" + "/x" * 1000,
    "https://host.test" + "/x" * 990, "http://hé.test/", "http://host.test/é", "HTTP://HOST.test/",
    "http://host.test:8088", "http://host.test:8088/services/collector", "http://host.test:8088/services/collector/",
    "http://host.test/services/collector/event", "http://host.test/proxy/hec", "http://host.test/?q", "http://host.test#f",
    "javascript:alert(1)", "http:", "http:host", "https://@host/", "https://user@/", "https://[::ffff:127.0.0.1]/",
    "http://host.test:99999999999999999999/", "http://١.test/", "", "   ",
    # hosts that are not ASCII: NFKC (urlsplit), str.lower (hostname), IDNA (httpx)
    "http://BÜCHER.example/", "https://bücher.example/h", "http://İstanbul.test/", "http://ΟΔΟΣ.test/x", "http://ΑΣ.ΣΑ/",
    "http://a\uff0fb.test/", "http://\u2100.test/", "http://ｅｘａｍｐｌｅ.com/", "http://xn--bcher-kva.example/",
    "http://ü:8080/", "https://user:pä@host.test/", "http://☃.net/", "http://ß.de/", "http://a\u200db.test/",
    "http://日本語.jp/", "http://-ü.test/", "http://ü-.test/", "http://" + "ü" * 70 + ".test/", "http://ü..test/",
    "http://[ü]/", "http://ü.test:99999/", "http://Ǆ.test/", "http://ﬀ.test/",
]


def one(url):
    out = {"url": url}
    try:
        out["valid"] = webhooks.validate_url(url)
    except ValueError as e:
        out["error"] = str(e)
    try:
        parts = urlsplit(url)
        out["scheme"], out["hostname"] = parts.scheme, parts.hostname
    except ValueError as e:
        out["split_error"] = str(e)
    if "valid" in out:
        out["endpoint"] = siem.endpoint(url)
    if _met:
        out["idna"] = dict(_met)
        _met.clear()
    return out


print(json.dumps([one(u) for u in URLS], ensure_ascii=False, indent=1))
