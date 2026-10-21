"""A Kubernetes API for the scripting tests: Jobs, the ConfigMaps and Secrets they
mount, their Pods' logs and exit codes. A Job ends as soon as it is created, the way
`behave(job)` says: ("succeeded" | "failed" | None (still running), log, exit code)."""
import json
from typing import Callable, Optional
from urllib.parse import parse_qs, urlsplit

import httpx

Behaviour = Callable[[dict], tuple[Optional[str], str, Optional[int]]]


class Cluster:
    def __init__(self, token: str = "sa-token"):
        self.token = token
        self.objects: dict[tuple[str, str, str], dict] = {}  # (kind, namespace, name)
        self.results: dict[str, tuple[Optional[str], str, Optional[int]]] = {}  # job name → outcome
        self.behave: Behaviour = lambda job: ("succeeded", "", 0)
        self.created: list[tuple[str, str, dict]] = []  # (kind, namespace, body), in order
        self.fail_with = 0
        self.events: dict[str, list[dict]] = {}  # job name → what the cluster said about it

    def jobs(self) -> list[dict]:
        return [o for (k, _, _), o in self.objects.items() if k == "jobs"]

    def _set_status(self, job: dict) -> None:
        name = job["metadata"]["name"]
        outcome, log, code = self.results.setdefault(name, self.behave(job))
        if outcome == "succeeded":
            job["status"] = {"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]}
        elif outcome == "failed":
            reason = "DeadlineExceeded" if code is None else "BackoffLimitExceeded"
            job["status"] = {"failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": reason}]}
        else:
            job["status"] = {}

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == f"Bearer {self.token}"
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "unavailable"})
        parts = req.url.path.strip("/").split("/")
        query = parse_qs(urlsplit(str(req.url)).query)
        if parts[:2] == ["apis", "batch"]:
            kind, ns, name = "jobs", parts[4], parts[6] if len(parts) > 6 else None
        else:
            kind, ns, name = parts[4], parts[3], parts[5] if len(parts) > 5 else None
        if kind == "pods":
            return self._pods(ns, name, parts, query)
        if kind == "events":
            job_name = query["fieldSelector"][0].split("=", 1)[1]
            return httpx.Response(200, json={"items": self.events.get(job_name, [])})
        if req.method == "POST":
            body = json.loads(req.content)
            key = (kind, ns, body["metadata"]["name"])
            if key in self.objects:
                return httpx.Response(409, json={"message": "already exists"})
            body = {**body, "metadata": {**body["metadata"], "uid": f"uid-{len(self.created)}"}}
            self.objects[key] = body
            self.created.append((kind, ns, body))
            if kind == "jobs":
                self._set_status(body)
            return httpx.Response(201, json=body)
        if req.method == "GET" and name:
            obj = self.objects.get((kind, ns, name))
            if obj is None:
                return httpx.Response(404, json={"message": "not found"})
            if kind == "jobs":
                self._set_status(obj)
            return httpx.Response(200, json=obj)
        if req.method == "DELETE":
            if name:
                gone = self.objects.pop((kind, ns, name), None)
                if gone is None:
                    return httpx.Response(404, json={"message": "not found"})
                self._owned_go(gone)
                return httpx.Response(200, json={})
            label, value = query["labelSelector"][0].split("=")
            for key in [k for k, o in self.objects.items()
                        if k[0] == kind and k[1] == ns and o["metadata"].get("labels", {}).get(label) == value]:
                self._owned_go(self.objects.pop(key))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"not mocked: {req.method} {req.url.path}"})

    def _owned_go(self, owner: dict) -> None:
        uid = owner["metadata"].get("uid")
        for key in [k for k, o in self.objects.items()
                    if any(r.get("uid") == uid for r in o["metadata"].get("ownerReferences") or [])]:
            del self.objects[key]

    def _pods(self, ns, name, parts, query) -> httpx.Response:
        if name is None:  # list by job-name
            job_name = query["labelSelector"][0].split("=", 1)[1]
            if job_name not in self.results:
                return httpx.Response(200, json={"items": []})
            _, _, code = self.results[job_name]
            state = {"terminated": {"exitCode": code}} if code is not None else {"running": {}}
            return httpx.Response(200, json={"items": [{
                "metadata": {"name": f"{job_name}-pod", "creationTimestamp": "2026-10-21T00:00:00Z"},
                "status": {"containerStatuses": [{"state": state}]}}]})
        job_name = name.removesuffix("-pod")
        return httpx.Response(200, text=self.results.get(job_name, (None, "", None))[1])
