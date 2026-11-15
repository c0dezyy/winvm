"""Tiny HTTP client (standard library only, so it also runs from a bare GitHub Actions runner)."""
import json
import urllib.error
import urllib.request


class ApiClient:
    def __init__(self, base_url, token=None, timeout=30, verify_ssl=True):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    def _ctx(self):
        if self.verify_ssl:
            return None
        import ssl
        return ssl._create_unverified_context()

    def _headers(self, extra=None):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        h.update(extra or {})
        return h

    def request(self, method, path, json_body=None, raw_body=None, headers=None, timeout=None):
        url = self.base_url + path
        data = raw_body if raw_body is not None else (json.dumps(json_body).encode() if json_body is not None else None)
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=self._ctx()) as r:
                body = r.read()
                return r.status, (json.loads(body) if body and r.headers.get("Content-Type", "").startswith("application/json") else body)
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body)
            except ValueError:
                return e.code, {"error": body.decode("utf-8", "replace")}

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, json_body=None, **kw):
        return self.request("POST", path, json_body=json_body, **kw)

    def put_file(self, path, file_path, timeout=120):
        import os
        with open(file_path, "rb") as f:
            data = f.read()
        return self.request("PUT", path, raw_body=data, headers={"Content-Type": "application/zip",
                            "Content-Length": str(len(data))}, timeout=timeout)

    def get_file(self, path, dest_path, timeout=120):
        req = urllib.request.Request(self.base_url + path, headers=self._headers())
        with urllib.request.urlopen(req, timeout=timeout, context=self._ctx()) as r:
            if r.status != 200:
                return r.status, None
            with open(dest_path, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            return r.status, dest_path
