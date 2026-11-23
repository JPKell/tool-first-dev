"""
Purpose: OpenWeightGym HTTP client for knowledge-graph embeddings and module summaries.
Role: service
Patterns: structured-errors
Tests: tests/test_toolkit.py
Invariants: Never logs the API key; failures raise OWGError (HTTP) or httpx.TransportError.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)
SMALL_MODEL_HINTS = ("mini", "small", "tiny", "flash", "lite", "1.5b", "2b", "3b", "4b", "7b", "8b", "9b")


class OWGError(RuntimeError):
    """Structured error raised when an OpenWeightGym API request fails."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        code: str | None = None,
        request_id: str | None = None,
        loadcoach_request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        self.loadcoach_request_id = loadcoach_request_id


@dataclass(frozen=True)
class OWGSettings:
    protocol: str
    host: str
    port: str
    api_key: str
    embedding_model: str
    summary_model: str
    suite_application: str

    @property
    def base_url(self) -> str:
        host = self.host.strip().rstrip("/")
        if host.startswith(("http://", "https://")):
            return host if host.endswith("/v1") else f"{host}/v1"
        port_part = f":{self.port.strip()}" if self.port.strip() else ""
        return f"{self.protocol.strip() or 'http'}://{host}{port_part}/v1"

    @property
    def is_configured(self) -> bool:
        return bool(self.host.strip() and self.api_key.strip())


def parse_dotenv(path: Path) -> dict[str, str]:
    """Parse key=value pairs from a .env file without mutating os.environ."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in {"'", '"'}:
            val = val[1:-1]
        if key:
            values[key] = val
    return values


def load_owg_settings(project_root: Path, suite_application: str = "tool-first-dev") -> OWGSettings:
    """Resolve OpenWeightGym connection settings from environment and project .env."""
    file_env: dict[str, str] = {}
    for candidate in (project_root / ".env", project_root / ".agents" / ".env"):
        file_env.update(parse_dotenv(candidate))

    def _get(*names: str, default: str = "") -> str:
        for name in names:
            v = os.environ.get(name)
            if v is not None and v.strip():
                return v.strip()
        for name in names:
            v = file_env.get(name)
            if v is not None and v.strip():
                return v.strip()
        return default

    return OWGSettings(
        protocol=_get("OWG_PROTOCOL", "API_PROTOCOL", default="http"),
        host=_get("OWG_HOST", "API_IP", "API_HOST", default=""),
        port=_get("OWG_PORT", "API_PORT", default="8442"),
        api_key=_get("OWG_API_KEY", "API_KEY", default=""),
        embedding_model=_get("OWG_EMBEDDING_MODEL", default=""),
        summary_model=_get("OWG_SUMMARY_MODEL", default=""),
        suite_application=_get("OWG_SUITE_APPLICATION", default=suite_application),
    )


def _is_available(model_entry: dict[str, Any], unavailable_machines: set[str]) -> bool:
    locations = model_entry.get("locations") or []
    if not locations:
        machines = model_entry.get("machines") or []
        return not machines or any(m not in unavailable_machines for m in machines)
    enabled = [loc.get("machine") for loc in locations if loc.get("enabled", True)]
    if not enabled:
        return False
    return any(m not in unavailable_machines for m in enabled)


class OWGClient:
    """Thin OpenWeightGym HTTP client following app-builder.md contracts."""

    def __init__(self, settings: OWGSettings, timeout: float = 45.0) -> None:
        self.settings = settings
        self.timeout = timeout
        self._models_cache: dict[str, Any] | None = None

    def _headers(self, trace: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "X-Suite-Application": self.settings.suite_application,
        }
        if trace:
            headers["X-LoadCoach-Trace"] = trace
        return headers

    def _raise_for_error(self, resp: httpx.Response) -> None:
        if 200 <= resp.status_code < 300:
            return
        req_id = resp.headers.get("X-Request-ID")
        lc_id = resp.headers.get("X-LoadCoach-Request-ID")
        code = None
        msg = f"HTTP {resp.status_code}"
        try:
            body = resp.json()
            err = body.get("error") or {}
            msg = err.get("message") or msg
            code = err.get("code")
        except Exception:
            if resp.text:
                msg = resp.text[:240]
        raise OWGError(
            message=msg,
            status_code=resp.status_code,
            code=code,
            request_id=req_id,
            loadcoach_request_id=lc_id,
        )

    def list_models(self, refresh: bool = False) -> dict[str, Any]:
        """Call GET /v1/models and cache the response for this client instance."""
        if not self.settings.is_configured:
            raise OWGError("OpenWeightGym is not configured in .env (set API_IP/OWG_HOST and API_KEY/OWG_API_KEY).")
        if self._models_cache is not None and not refresh:
            return self._models_cache
        url = f"{self.settings.base_url}/models"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(url, headers=self._headers())
        self._raise_for_error(resp)
        self._models_cache = resp.json()
        return self._models_cache

    def resolve_embedding_model(self) -> tuple[str, int]:
        """Resolve (model_id, context_tokens) using OWG_EMBEDDING_MODEL or /v1/models discovery."""
        catalog = self.list_models()
        data = catalog.get("data") or []
        unavailable = set(catalog.get("unavailable_machines") or [])
        embed_models = [
            m for m in data if m.get("embedding") is True or (m.get("capabilities") or {}).get("embeddings") is True
        ]

        if self.settings.embedding_model:
            for m in embed_models:
                if m.get("id") == self.settings.embedding_model:
                    ctx = m.get("context_tokens") or (m.get("context") or {}).get("guaranteed") or 8192
                    return m["id"], int(ctx)
            # Allow role names returned in data even if embedding flag isn't mirrored
            for m in data:
                if m.get("id") == self.settings.embedding_model:
                    ctx = m.get("context_tokens") or (m.get("context") or {}).get("guaranteed") or 8192
                    return m["id"], int(ctx)
            raise OWGError(
                f"Configured OWG_EMBEDDING_MODEL '{self.settings.embedding_model}' not found in /v1/models."
            )

        if not embed_models:
            raise OWGError("No embedding model (embedding: true) available in /v1/models.")

        embed_models.sort(key=lambda m: (0 if _is_available(m, unavailable) else 1, m.get("id", "")))
        chosen = embed_models[0]
        ctx = chosen.get("context_tokens") or (chosen.get("context") or {}).get("guaranteed") or 8192
        return chosen["id"], int(ctx)

    def resolve_summary_model(self) -> str:
        """Resolve summary chat model ID using OWG_SUMMARY_MODEL or /v1/models discovery."""
        catalog = self.list_models()
        data = catalog.get("data") or []
        unavailable = set(catalog.get("unavailable_machines") or [])
        chat_models = [
            m
            for m in data
            if (m.get("capabilities") or {}).get("chat") is True and m.get("owned_by") != "adaptor"
        ]

        if self.settings.summary_model:
            for m in data:
                if m.get("id") == self.settings.summary_model:
                    return m["id"]
            raise OWGError(
                f"Configured OWG_SUMMARY_MODEL '{self.settings.summary_model}' not found in /v1/models."
            )

        if not chat_models:
            raise OWGError("No chat model (capabilities.chat: true) available in /v1/models.")

        def _rank(m: dict[str, Any]) -> tuple[int, int, str]:
            mid = str(m.get("id", "")).lower()
            avail = 0 if _is_available(m, unavailable) else 1
            small = 0 if any(h in mid for h in SMALL_MODEL_HINTS) else 1
            return (avail, small, mid)

        chat_models.sort(key=_rank)
        return str(chat_models[0]["id"])

    def embed_texts(self, texts: list[str], trace: str = "kg-embeddings") -> tuple[str, list[list[float]]]:
        """Generate dense float embeddings for one or more strings via POST /v1/embeddings."""
        if not texts:
            return "", []
        model_id, ctx_tokens = self.resolve_embedding_model()
        # Conservative char cap (~3 chars/token) to stay safely inside embedding context_tokens
        max_chars = max(512, min(ctx_tokens * 3, 24000))
        clipped = [t[:max_chars] for t in texts]

        url = f"{self.settings.base_url}/embeddings"
        payload = {"model": model_id, "input": clipped, "encoding_format": "float"}
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._headers(trace=trace), json=payload)
        self._raise_for_error(resp)
        body = resp.json()
        items = sorted(body.get("data") or [], key=lambda x: x.get("index", 0))
        vectors = [list(map(float, item["embedding"])) for item in items]
        return model_id, vectors

    def summarize_module(
        self,
        rel_path: str,
        role: str,
        patterns: list[str],
        source: str,
        max_source_chars: int = 8000,
        trace: str = "kg-summary",
    ) -> tuple[str, str]:
        """Generate a concise 2-3 sentence summary via POST /v1/chat/completions."""
        model_id = self.resolve_summary_model()
        excerpt = source[:max_source_chars]
        url = f"{self.settings.base_url}/chat/completions"
        payload = {
            "model": model_id,
            "temperature": 0.1,
            "max_tokens": 300,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write ultra-concise code index summaries for an AI coding agent knowledge graph. "
                        "Respond with 2-3 plain sentences covering: purpose, key exported symbols/interfaces, "
                        "important dependencies, and invariants. No markdown headers, no bullet lists, no preamble."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"File: {rel_path}\n"
                        f"Role: {role or 'unspecified'}\n"
                        f"Patterns: {', '.join(patterns) if patterns else 'none'}\n\n"
                        f"Code:\n{excerpt}"
                    ),
                },
            ],
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, headers=self._headers(trace=trace), json=payload)
            if resp.status_code == 400:
                # Fallback for engines that reject chat_template_kwargs
                fallback_payload = {k: v for k, v in payload.items() if k != "chat_template_kwargs"}
                fallback_payload["max_tokens"] = 1200
                resp = client.post(url, headers=self._headers(trace=trace), json=fallback_payload)
        self._raise_for_error(resp)
        body = resp.json()
        msg = ((body.get("choices") or [{}])[0]).get("message") or {}
        content = msg.get("content") or ""
        cleaned = THINK_BLOCK_RE.sub("", content).strip()
        cleaned = " ".join(cleaned.split())
        if not cleaned:
            raise OWGError(f"Model '{model_id}' returned empty summary content.")
        return model_id, cleaned[:900]
