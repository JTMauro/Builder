"""A small client for Builder's HTTP API, for other apps and scripts.

Standard library only, so you can also copy this one file into another project.

    from media_backend.client import Builder

    b = Builder("http://127.0.0.1:8000", token="mbp_...")      # token: Connect Claude Code, or `builder token`
    clip = b.upload("intro.mp4")
    short = b.run("trim", file_id=clip["id"], start=2, duration=5)["outputs"][0]
    b.download(short["id"], "short.mp4")

    run = b.build("A landing page for my bakery", target="https://example.com")
    for event in b.follow(run["id"]):            # live events until the build ends
        if event["type"] == "ask":                 # a question or an approval for the person
            print(event["request"]["title"])
    print(b.chat("What did that build make?")["reply"])
"""

from __future__ import annotations

import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterator


class BuilderError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class Builder:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 120):
        self.url = (url or os.environ.get("BUILDER_URL") or "http://127.0.0.1:8000").rstrip("/")
        self.token = token or os.environ.get("BUILDER_TOKEN")
        self.timeout = timeout

    # ------------------------------------------------------------ plumbing

    def _request(self, method: str, path: str, body: Any = None, query: dict | None = None,
                 raw: bytes | None = None, content_type: str | None = None, timeout: float | None = None) -> Any:
        url = self.url + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                payload = resp.read()
                if resp.headers.get_content_type() == "application/json":
                    return json.loads(payload) if payload else None
                return payload
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except ValueError:
                detail = e.reason
            raise BuilderError(e.code, detail) from None

    # ------------------------------------------------------------ files and media tools

    def health(self) -> dict:
        return self._request("GET", "/health")

    def operations(self) -> list[dict]:
        return self._request("GET", "/operations")

    def files(self) -> list[dict]:
        return self._request("GET", "/files")

    def upload(self, path: str | Path) -> dict:
        path = Path(path)
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode()
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        return self._request("POST", "/files", raw=body, content_type=f"multipart/form-data; boundary={boundary}", timeout=600)

    def run(self, operation: str, **params: Any) -> dict:
        """Run a media operation and wait for it. Raises BuilderError if it fails."""
        job = self._request("POST", f"/ops/{operation}", body=params, timeout=3600)
        if job.get("status") != "succeeded":
            raise BuilderError(422, job.get("error") or job.get("status"))
        return job

    def download(self, file_id: str, dest: str | Path) -> Path:
        dest = Path(dest)
        dest.write_bytes(self._request("GET", f"/files/{file_id}/content", timeout=600))
        return dest

    # ------------------------------------------------------------ Craft builds

    def build(self, request: str, target: str = "", file_id: str | None = None, rounds: int = 2) -> dict:
        return self._request("POST", "/craft/runs", body={"request": request, "target": target, "file_id": file_id, "rounds": rounds})

    def build_status(self, run_id: str) -> dict:
        return self._request("GET", f"/craft/runs/{run_id}")["run"]

    def follow(self, run_id: str, poll: float = 25) -> Iterator[dict]:
        """Yield the build's events as they happen, until it ends."""
        cursor = 0
        while True:
            r = self._request("GET", f"/craft/runs/{run_id}", query={"after": cursor, "wait": poll}, timeout=poll + 30)
            yield from r["events"]
            cursor = r["cursor"]
            if r["run"]["finished_at"]:
                return
            if not r["events"]:
                time.sleep(0.2)

    def answer(self, run_id: str, *, choice: str | None = None, text: str | None = None,
               allow: bool | None = None, always: bool = False) -> dict:
        body = {k: v for k, v in {"choice": choice, "text": text, "allow": allow, "always": always or None}.items() if v is not None}
        return self._request("POST", f"/craft/runs/{run_id}/answer", body=body)

    def stop(self, run_id: str) -> dict:
        return self._request("POST", f"/craft/runs/{run_id}/stop")

    def change(self, run_id: str, request: str) -> dict:
        return self._request("POST", f"/craft/runs/{run_id}/followup", body={"request": request})

    def projects(self) -> list[dict]:
        return self._request("GET", "/craft/projects")

    # ------------------------------------------------------------ Rivet

    def chat(self, message: str, session_id: str | None = None, file_ids: list[str] | None = None) -> dict:
        return self._request("POST", "/assistant/chat", body={"message": message, "session_id": session_id, "file_ids": file_ids}, timeout=1800)
