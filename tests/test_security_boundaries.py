from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _load_pdf_handler():
    path = (
        Path(__file__).parents[1]
        / "skills/resource-to-markdown/scripts/handlers/pdf_handler.py"
    )
    spec = importlib.util.spec_from_file_location("loom_pdf_handler", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_store_uses_owner_only_permissions(loom):
    home = loom["home"]
    store = loom["store"]

    assert os.stat(home).st_mode & 0o777 == 0o700
    assert os.stat(store.DB_PATH).st_mode & 0o777 == 0o600
    assert os.stat(store.CARDS_DIR).st_mode & 0o777 == 0o700
    assert os.stat(store.SOURCES_DIR).st_mode & 0o777 == 0o700


def test_remote_ocr_requires_explicit_host(monkeypatch):
    handler = _load_pdf_handler()
    monkeypatch.delenv("LOOM_OCR_REMOTE_HOST", raising=False)
    monkeypatch.setattr(handler.shutil, "which", lambda name: "/usr/bin/ssh" if name == "ssh" else None)

    calls = []
    monkeypatch.setattr(handler.subprocess, "run", lambda *args, **kwargs: calls.append(args))

    assert handler._rapidocr_device() in {"cpu", "gpu"}
    assert calls == []


def test_workbench_has_no_cross_origin_middleware():
    source = (Path(__file__).parents[1] / "workbench/backend/main.py").read_text(encoding="utf-8")
    assert "CORSMiddleware" not in source
    assert 'allow_origins=["*"]' not in source
