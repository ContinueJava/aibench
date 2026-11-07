"""genai-bench WebUI: single-process, single-port management page.

Importing this package must NOT require Flask (the upstream CLI imports the
parent package unconditionally via gevent monkey-patching). ``create_app`` is
lazily exposed through ``__getattr__`` so the plain CLI keeps working even in
environments where the WebUI extras are absent.
"""

from __future__ import annotations


def __getattr__(name: str):
    if name == "create_app":
        from genai_bench.web.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["create_app"]
