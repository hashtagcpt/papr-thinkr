"""Durable model-call checkpoints, keyed by actual inputs, never credentials."""
from __future__ import annotations

import copy
import json
import threading
import time

from ..llm import check_cancelled
from ..project import _atomic_write_json
from .provenance import digest, file_hash, scope_signature


class ReviewCache:
    VERSION = 1

    def __init__(self, project, llm, manifest, force=False):
        self.project, self.llm, self.manifest, self.force = project, llm, manifest, force
        self.hits = self.misses = 0
        self.lock = threading.Lock()

    def chat_json(self, messages, *, roles=None, cache_if=None, **kwargs):
        check_cancelled()
        identity = {k: getattr(self.llm, k, None) for k in
                    ("backend", "model", "host", "base_url", "num_ctx", "context_chars", "supports_images")}
        identity["resolved_model"] = getattr(self.llm, "_resolved_model", None)
        images = []
        for message in messages:
            for path in message.get("images") or []:
                try:
                    images.append([path, file_hash(path)])
                except OSError:
                    images.append([path, None])
        key = digest({"version": self.VERSION, "model": identity, "messages": messages, "images": images,
                      "options": kwargs, "sources": scope_signature(self.manifest, roles)})
        path = self.project.dir / "review-cache" / f"{key}.json"
        if not self.force and path.is_file():
            try:
                result = json.loads(path.read_text(encoding="utf-8"))["result"]
                if cache_if is None or cache_if(result):
                    with self.lock:
                        self.hits += 1
                    return copy.deepcopy(result)
            except (ValueError, KeyError, OSError):
                pass
        with self.lock:
            self.misses += 1
        result = self.llm.chat_json(messages, **kwargs)
        # Failed calls and incomplete batches never become checkpoints.
        schema = kwargs.get("json_schema") or {}
        valid_shape = ((schema.get("type") == "array" and isinstance(result, list)) or
                       (schema.get("type") == "object" and isinstance(result, dict) and bool(result)))
        if valid_shape and (cache_if is None or cache_if(result)):
            path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_json(path, {"created_at": time.time(), "result": result})
        return result

    def summary(self):
        return {"reused_calls": self.hits, "new_calls": self.misses}
