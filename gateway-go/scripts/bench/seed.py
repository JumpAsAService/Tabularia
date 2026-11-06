"""Seeds the bench database through the Python gateway's API: folders, flows, a group and
an editor who reaches them through the group. Prints the editor's and the admin's tokens
and a folder id, as shell assignments."""
import json
import sys
import urllib.error
import urllib.request

base, admin_password, editor_password = sys.argv[1], sys.argv[2], sys.argv[3]
FOLDERS, FLOWS = 40, 15


def call(method, path, body=None, token=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode())
    req.add_header("Content-Type", "application/json")
    if path != "/auth/login":
        req.add_header("X-Tabularia-CSRF", "1")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {path}: {e.code} {e.read()[:300]!r}")


admin = call("POST", "/auth/login", {"email": "admin@tabularia.local", "password": admin_password})["access_token"]
user = call("POST", "/users", {"email": "editor@bench.test", "password": editor_password, "full_name": "Editor"}, admin)
group = call("POST", "/groups", {"name": "analysts"}, admin)
call("PUT", f"/users/{user['id']}/groups/{group['id']}", None, admin)
folders = []
for i in range(FOLDERS):
    p = call("POST", "/projects", {"name": f"Folder {i:02d}", "description": "bench"}, admin)
    folders.append(p["id"])
    cap = "edit" if i % 2 == 0 else "view"
    call("POST", f"/projects/{p['id']}/permissions", {"capability": cap, "group_id": group["id"]}, admin)
    for j in range(FLOWS):
        call("POST", f"/projects/{p['id']}/flows", {"name": f"Flow {i:02d}-{j:02d}", "description": "x" * 40}, admin)
editor = call("POST", "/auth/login", {"email": "editor@bench.test", "password": editor_password})["access_token"]
print(f"EDITOR_TOKEN={editor}")
print(f"ADMIN_TOKEN={admin}")
print(f"FOLDER={folders[3]}")
