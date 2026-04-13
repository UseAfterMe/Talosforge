#!/usr/bin/env python3
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass


def normalize_proxmox_api_url(raw: str) -> str:
    value = raw.strip()
    if "://" not in value:
        value = f"https://{value}:8006"
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Provide a hostname or a full https:// URL.")
    base = f"{parsed.scheme}://{parsed.netloc}"
    if not base.endswith("/api2/json"):
        base = f"{base}/api2/json"
    return base


@dataclass
class ProxmoxClient:
    api_url: str
    username: str
    password: str
    insecure: bool

    def __post_init__(self) -> None:
        self.ssl_context = ssl._create_unverified_context() if self.insecure else ssl.create_default_context()
        self.cookie: str | None = None
        self.csrf_token: str | None = None

    def request(self, method: str, path: str, payload: dict[str, str] | None = None) -> dict:
        url = f"{self.api_url}{path}"
        data = None
        headers: dict[str, str] = {}

        if payload is not None:
            data = urllib.parse.urlencode(payload).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        if self.cookie:
            headers["Cookie"] = f"PVEAuthCookie={self.cookie}"
        if self.csrf_token and method != "GET":
            headers["CSRFPreventionToken"] = self.csrf_token

        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, context=self.ssl_context, timeout=20) as response:
            return json.loads(response.read().decode())

    def login(self) -> None:
        response = self.request(
            "POST",
            "/access/ticket",
            payload={"username": self.username, "password": self.password},
        )
        data = response["data"]
        self.cookie = data["ticket"]
        self.csrf_token = data["CSRFPreventionToken"]

    def get(self, path: str) -> dict:
        return self.request("GET", path)
