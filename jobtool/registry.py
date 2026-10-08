"""Auto-discovery of extensions in the top-level `extensions` package."""
from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil

from .extension import Extension

log = logging.getLogger(__name__)


def discover(package: str = "extensions") -> dict[str, type[Extension]]:
    found: dict[str, type[Extension]] = {}
    pkg = importlib.import_module(package)
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package}.{mod_info.name}")
        except Exception:
            # A broken extension must not take the others down.
            log.exception("Failed to import extension module %s", mod_info.name)
            continue
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Extension) and obj is not Extension and obj.__module__ == module.__name__:
                if obj.site_id in found:
                    log.error("Duplicate site_id %s in %s, ignoring", obj.site_id, mod_info.name)
                    continue
                found[obj.site_id] = obj
    return found


def runnable(extensions: dict[str, type[Extension]], demo: bool) -> dict[str, type[Extension]]:
    return {
        sid: ext
        for sid, ext in extensions.items()
        if ext.enabled and (ext.public_demo_enabled or not demo)
    }
