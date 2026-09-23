from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Event, Thread
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from .protocol import AdmissionManager, Declaration, Grant, Observation, ProtocolError, VersionedWorkspace


def encode(value):
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    raise TypeError(type(value).__name__)


def declaration(value):
    return Declaration(frozenset(value["reads"]), frozenset(value["writes"]))


class WorkspaceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace, require_grant=True, validate_plan=True):
        self.workspace = workspace
        self.manager = workspace.admission
        self.require_grant = require_grant
        self.validate_plan = validate_plan
        self.cancel = Event()
        super().__init__(address, Handler)

    def dispatch(self, path, value):
        owner = value.get("owner", "")
        attempt = value.get("attempt_id", "")
        store = self.workspace
        manager = self.manager
        if path == "/txserve/begin":
            return {"attempt_id": store.begin(owner)}
        if path == "/txserve/plan/read":
            return asdict(store.plan_read(attempt, owner, value["resource"]))
        if path == "/txserve/plan/search":
            return [asdict(item) for item in store.search(attempt, owner, value["directory"], value["text"])]
        if path == "/txserve/plan/finalize":
            return asdict(store.finalize(attempt, owner, declaration(value["declaration"])))
        if path == "/txserve/plan/observations":
            return [asdict(item) for item in store.observations(attempt, owner)]
        if path == "/txserve/admit":
            graph_scope = (declaration(value["graph_scope"])
                           if not manager.resolve_hints and value.get("graph_scope") is not None else None)
            grant = manager.acquire(owner, declaration(value["declaration"]), value["lease_seconds"],
                                    graph_scope=graph_scope, timeout=value.get("timeout"),
                                    cancel=self.cancel, grant_id=value.get("grant_id"))
            return asdict(grant)
        if path == "/txserve/bind":
            snapshot = store.bind(attempt, owner, value["inputs"], value.get("grant_id"),
                                  self.validate_plan, self.require_grant)
            return {key: asdict(item) for key, item in snapshot.items()}
        if path == "/txserve/read":
            return asdict(store.read(attempt, owner, value["resource"]))
        if path == "/txserve/check-write":
            store.check_write(attempt, owner, value["resource"])
            return {}
        if path == "/txserve/publish":
            return store.publish(attempt, owner, value["patches"])
        if path == "/txserve/release":
            return {"released": manager.release(value["grant_id"], owner)}
        if path == "/txserve/finish":
            store.finish(attempt, owner)
            return {}
        if path == "/txserve/measure":
            return manager.measurements()
        raise ProtocolError("UNKNOWN_ENDPOINT")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 32 * 1024 * 1024:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(size))
            result = self.server.dispatch(self.path, payload)
            status = 200
        except ProtocolError as error:
            result = {"reason": error.reason, "resource": error.resource, "permission": error.permission}
            status = 409
        except (ValueError, KeyError, TypeError) as error:
            result = {"reason": "INVALID_REQUEST", "detail": str(error)}
            status = 400
        body = json.dumps(result, default=encode).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            if self.path == "/txserve/admit" and status == 200:
                self.server.manager.release(result["grant_id"], result["owner"])


class APIClient:
    def __init__(self, url: str, timeout: float = 660):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def post(self, path, **payload):
        request = Request(self.url + path, json.dumps(payload, default=encode).encode(),
                          {"Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            value = json.load(error)
            raise ProtocolError(value.get("reason", "HTTP_ERROR"), value.get("resource", ""),
                                value.get("permission", "")) from error


class RemoteAdmission:
    def __init__(self, client: APIClient):
        self.client = client

    def acquire(self, owner, declaration, lease_seconds=300, graph_scope=None, timeout=None,
                cancel=None, grant_id=None):
        if cancel is not None and cancel.is_set():
            raise ProtocolError("CANCELLED")
        value = self.client.post("/txserve/admit", owner=owner, declaration=asdict(declaration),
                                 graph_scope=asdict(graph_scope or declaration),
                                 lease_seconds=lease_seconds, timeout=timeout, grant_id=grant_id)
        value["declaration"] = Declaration(**value["declaration"])
        value["graph_scope"] = Declaration(**value["graph_scope"])
        return Grant(**value)

    def release(self, grant_id, owner):
        return self.client.post("/txserve/release", grant_id=grant_id, owner=owner)["released"]

    def measurements(self):
        return self.client.post("/txserve/measure")


class RemoteWorkspace:
    def __init__(self, client):
        self.client = client
        self.snapshots = {}

    def begin(self, owner):
        return self.client.post("/txserve/begin", owner=owner)["attempt_id"]

    def plan_read(self, attempt_id, owner, resource):
        return Observation(**self.client.post("/txserve/plan/read", attempt_id=attempt_id,
                                             owner=owner, resource=resource))

    def search(self, attempt_id, owner, directory, text):
        return [Observation(**item) for item in self.client.post(
            "/txserve/plan/search", attempt_id=attempt_id, owner=owner, directory=directory, text=text)]

    def observations(self, attempt_id, owner):
        return tuple(Observation(**item) for item in self.client.post(
            "/txserve/plan/observations", attempt_id=attempt_id, owner=owner))

    def finalize(self, attempt_id, owner, declaration):
        return Declaration(**self.client.post("/txserve/plan/finalize", attempt_id=attempt_id,
                                              owner=owner, declaration=asdict(declaration)))

    def bind(self, attempt_id, owner, inputs, grant_id, validate_plan=True, require_grant=True):
        value = self.client.post("/txserve/bind", attempt_id=attempt_id, owner=owner,
                                 inputs=list(inputs), grant_id=grant_id)
        snapshot = {key: Observation(**item) for key, item in value.items()}
        self.snapshots[attempt_id] = snapshot
        return snapshot

    def read(self, attempt_id, owner, resource):
        item = Observation(**self.client.post("/txserve/read", attempt_id=attempt_id,
                                              owner=owner, resource=resource))
        self.snapshots[attempt_id][resource] = item
        return item

    def check_write(self, attempt_id, owner, resource):
        self.client.post("/txserve/check-write", attempt_id=attempt_id, owner=owner, resource=resource)

    def publish(self, attempt_id, owner, patches, check=None):
        if check is not None:
            check(self.snapshots[attempt_id], patches)
        return self.client.post("/txserve/publish", attempt_id=attempt_id, owner=owner, patches=dict(patches))

    def finish(self, attempt_id, owner):
        try:
            self.client.post("/txserve/finish", attempt_id=attempt_id, owner=owner)
        finally:
            self.snapshots.pop(attempt_id, None)


@contextmanager
def serving(workspace, require_grant=True, validate_plan=True):
    server = WorkspaceServer(("127.0.0.1", 0), workspace, require_grant, validate_plan)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = APIClient(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield RemoteWorkspace(client), RemoteAdmission(client)
    finally:
        server.cancel.set()
        server.shutdown()
        server.server_close()
        thread.join()


def main():
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18081)
    parser.add_argument("--capacity", type=int, default=8)
    parser.add_argument("--reservation", type=int, default=8)
    parser.add_argument("--selection", choices=["minimum_degree", "fifo_compatible", "fifo_hol", "random"],
                        default="minimum_degree")
    args = parser.parse_args()
    blocks = json.loads(args.workspace.read_text(encoding="utf-8"))
    manager = AdmissionManager(args.capacity, args.selection, args.reservation)
    server = WorkspaceServer(("127.0.0.1", args.port), VersionedWorkspace(blocks, manager))
    try:
        server.serve_forever()
    finally:
        server.cancel.set()
        server.server_close()


if __name__ == "__main__":
    main()
