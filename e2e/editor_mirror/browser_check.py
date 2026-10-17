"""Browser check: a run launched from the flow EDITOR sends the Output's S3 copy
(«mirror») too — before, only scheduled and run-now runs did (flow_resolver).

Needs, in the folder given, the objects of e2e/placeholders/api_proofs.py `main`
(the S3 connection «zz lake» to a throwaway MinIO and the datasource «vendite
giornaliere») and that MinIO (container `zzph-minio`, bucket `zzph-out`). The
object written is checked from the backend container (boto3).

    ADMIN_EMAIL=… ADMIN_PASSWORD=… S3_SECRET=… python3 e2e/editor_mirror/browser_check.py <folder id>
"""
import json, os, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dbt_export"))
from marionette import Firefox, ferma  # noqa: E402

UI = os.environ.get("UI", "http://localhost:3000")
API = os.environ.get("API", "http://localhost:8000")
SHOTS = os.environ.get("SHOTS", "/tmp")
PROFILE = os.path.expanduser("~/snap/firefox/common/zz-editor-mirror")
PID = int(sys.argv[1])
ZONE = "Pacific/Kiritimati"
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
lake = next(c for c in api("GET", f"/projects/{PID}/connections", token=token) if c["name"] == "zz lake")
ds = next(d for d in api("GET", f"/projects/{PID}/datasources", token=token) if d["name"] == "vendite giornaliere")
engines = [e["id"] for e in api("GET", "/engines", token=token) if e.get("available") and e.get("allowed", True)]
definition = {"nodes": [
    {"id": "s", "type": "source", "position": {"x": 0, "y": 0}, "data": {"datasourceId": ds["id"], "bucket": ds["bucket"], "parquetKey": ds["key"]}},
    {"id": "o", "type": "output", "position": {"x": 320, "y": 0},
     "data": {"destType": "datasource", "name": "copia dal editor", "projectId": PID, "mirrorEnabled": True,
              "mirrorConnectionId": lake["id"], "mirrorBucket": "zzph-out", "mirrorKey": "editor/{flow}_{date}.parquet",
              "timezone": ZONE}},
], "edges": [{"id": "e", "source": "s", "target": "o", "targetHandle": "left"}]}
flow = api("POST", f"/projects/{PID}/flows", {"name": "Copia editor", "definition": json.dumps(definition),
                                              "engine": "polars" if "polars" in engines else engines[0]}, token=token)

os.makedirs(PROFILE, exist_ok=True)
proc = subprocess.Popen(["firefox", "--headless", "--marionette", "--profile", PROFILE, "--window-size", "1500,1000", "about:blank"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    ff = Firefox()
    ff.vai(f"{UI}/favicon.svg")
    ff.js("localStorage.clear(); document.cookie = 'tabularia-locale=en; path=/; max-age=86400; samesite=lax'; return 1")
    ff.entra(UI, token)
    ff.vai(f"{UI}/editor?flow={flow['id']}")
    ok("the editor shows the Output", ff.aspetta("document.querySelectorAll('.node-output').length === 1", 40))
    ff.aspetta("!!Array.from(document.querySelectorAll('button.primary')).find(b => b.innerText.includes('Run') && !b.disabled)", 20)
    ff.js("Array.from(document.querySelectorAll('button.primary')).find(b => b.innerText.includes('Run')).click(); return 1")
    ok("the run dialog opens", ff.aspetta("!!document.querySelector('.rd-card')", 10))
    ff.js("document.querySelector('.rd-actions button.primary').click(); return 1")
    ok("the run starts", ff.aspetta("!document.querySelector('.rd-card')", 20),
       ff.js("return (document.querySelector('.rd-err')||{innerText:''}).innerText"))
    ff.foto(os.path.join(SHOTS, "editor-mirror.png"))
finally:
    ferma(proc)

end, run = time.time() + 120, {}
while time.time() < end:
    runs = [r for r in api("GET", f"/flows/{flow['id']}/runs", token=token) if r.get("kind") == "flow"]
    run = api("GET", f"/runs/{runs[0]['id']}", token=token) if runs else {}
    if run.get("status") in ("SUCCESS", "FAILURE") and json.loads(run.get("mirror") or "{}").get("ok") is not None:
        break
    time.sleep(2)
mirror = json.loads(run.get("mirror") or "{}")
far = datetime.now(timezone.utc).astimezone(ZoneInfo(ZONE))
expected = f"editor/Copia_editor_{far:%Y-%m-%d}.parquet"
ok("the editor's run published the datasource", run.get("status") == "SUCCESS" and run.get("publish_name") == "copia dal editor", run)
ok("…and sent the S3 copy, its key resolved in the node's zone", mirror.get("ok") is True and mirror.get("key") == expected, mirror)
probe = ("import boto3,os;from botocore.config import Config;"
         "c=boto3.client('s3',endpoint_url='http://zzph-minio:9000',aws_access_key_id='zzphadmin',"
         "aws_secret_access_key=os.environ['S'],config=Config(s3={'addressing_style':'path'}));"
         f"print(c.head_object(Bucket='zzph-out',Key='{expected}')['ContentLength'])")
size = subprocess.run(["docker", "exec", "-e", f"S={os.environ['S3_SECRET']}", "dataprep-backend", "python", "-c", probe],
                      capture_output=True, text=True)
ok("the object is in the bucket", size.returncode == 0 and int(size.stdout.strip() or 0) > 0, size.stderr[-300:])
api("DELETE", f"/flows/{flow['id']}", token=token)
for d in api("GET", f"/projects/{PID}/datasources", token=token):
    if d["name"] == "copia dal editor":
        api("DELETE", f"/datasources/{d['id']}", token=token)

print(f"\n{sum(results)}/{len(results)} ok")
sys.exit(0 if all(results) else 1)
