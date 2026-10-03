"""Reel Engine dashboard: static site builder, local read-only server and demo data.

Public API (used by the CLI):
    build_dashboard(cfg, conn, out_dir=None, demo=False) -> Path      (build.py)
    serve(cfg, site_dir, port=None, open_browser=False, media_root=None)  (server.py)
    seed_demo(db_path, cfg, thumbs_dir)                                 (demo.py)

Imports are lazy so `python -m reelengine.dashboard.server` does not pre-import the
server module (runpy warns about that) and the CLI only pays for what it uses.
"""
from __future__ import annotations

__all__ = ["build_dashboard", "serve", "make_server", "seed_demo"]


def __getattr__(name: str):
    if name == "build_dashboard":
        from .build import build_dashboard

        return build_dashboard
    if name in ("serve", "make_server"):
        from . import server

        return getattr(server, name)
    if name == "seed_demo":
        from .demo import seed_demo

        return seed_demo
    raise AttributeError(name)
