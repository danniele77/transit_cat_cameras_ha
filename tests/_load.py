"""Carga aislada de módulos de custom_components/transit_cat_cameras para tests.

Mismo enfoque que tests/_load.py en jonathanathe/dgt_traffic_cameras_ha:
estos tests corren SIN Home Assistant instalado, registrando en
sys.modules stubs mínimos que solo cubren lo que api.py necesita para
importarse (HomeAssistant y aiohttp.ClientSession, usados únicamente como
anotaciones de tipo / firmas de función, nunca invocados de verdad aquí).

Esto prueba el comportamiento REAL del parseo de XML contra un fixture
real, no una simulación.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "transit_cat_cameras"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _module(name: str) -> types.ModuleType:
    parts = name.split(".")
    for i in range(1, len(parts) + 1):
        partial = ".".join(parts[:i])
        if partial not in sys.modules:
            mod = types.ModuleType(partial)
            sys.modules[partial] = mod
            if i > 1:
                parent = sys.modules[".".join(parts[: i - 1])]
                setattr(parent, parts[i - 1], mod)
    return sys.modules[name]


def _ensure_stubs() -> None:
    if "homeassistant.core" not in sys.modules:
        core = _module("homeassistant.core")

        class HomeAssistant:  # noqa: D401 - stub
            data: dict = {}

        core.HomeAssistant = HomeAssistant

    if "aiohttp" not in sys.modules:
        aiohttp = _module("aiohttp")

        class ClientSession:  # noqa: D401 - stub
            pass

        aiohttp.ClientSession = ClientSession


def load(module_name: str) -> types.ModuleType:
    """Carga custom_components/transit_cat_cameras/<module_name>.py aislado."""
    _ensure_stubs()

    full_name = f"custom_components.transit_cat_cameras.{module_name}"
    if full_name in sys.modules:
        return sys.modules[full_name]

    if "custom_components" not in sys.modules:
        pkg = types.ModuleType("custom_components")
        pkg.__path__ = [str(REPO_ROOT / "custom_components")]
        sys.modules["custom_components"] = pkg
    if "custom_components.transit_cat_cameras" not in sys.modules:
        pkg = types.ModuleType("custom_components.transit_cat_cameras")
        pkg.__path__ = [str(COMPONENT_DIR)]
        sys.modules["custom_components.transit_cat_cameras"] = pkg

    spec = importlib.util.spec_from_file_location(
        full_name, COMPONENT_DIR / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()
