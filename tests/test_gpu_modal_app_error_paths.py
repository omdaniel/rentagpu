from __future__ import annotations

import importlib
import io
import json
import pathlib
import sys
import tarfile
import tempfile
import types
import unittest
from typing import Any, Callable
from unittest import mock


def import_gpu_modal_app_with_fake_modal() -> Any:
    fake_modal = types.ModuleType("modal")

    class FakeImage:
        @staticmethod
        def from_registry(*args: object, **kwargs: object) -> "FakeImage":
            return FakeImage()

        def apt_install(self, *args: object, **kwargs: object) -> "FakeImage":
            return self

        def pip_install(self, *args: object, **kwargs: object) -> "FakeImage":
            return self

    class FakeVolume:
        @staticmethod
        def from_name(name: str, create_if_missing: bool = False) -> object:
            return object()

    class FakeApp:
        def __init__(self, name: str) -> None:
            self.name = name

        def function(self, **kwargs: object) -> Callable[[Any], Any]:
            def deco(fn: Any) -> Any:
                fn.remote = fn
                return fn

            return deco

        def local_entrypoint(self) -> Callable[[Any], Any]:
            def deco(fn: Any) -> Any:
                return fn

            return deco

    fake_modal.Image = FakeImage  # type: ignore[attr-defined]
    fake_modal.Volume = FakeVolume  # type: ignore[attr-defined]
    fake_modal.App = FakeApp  # type: ignore[attr-defined]

    with mock.patch.dict(sys.modules, {"modal": fake_modal}):
        sys.modules.pop("scripts.gpu_modal_app", None)
        return importlib.import_module("scripts.gpu_modal_app")


class GpuModalAppErrorPathTests(unittest.TestCase):
    gpu_modal_app: Any

    @classmethod
    def setUpClass(cls) -> None:
        cls.gpu_modal_app = import_gpu_modal_app_with_fake_modal()

    def test_safe_extract_tar_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="modal_extract_") as tmp:
            archive_path = pathlib.Path(tmp) / "workspace.tar.gz"
            destination = pathlib.Path(tmp) / "out"

            with tarfile.open(archive_path, mode="w:gz") as tf:
                data = b"malicious"
                info = tarfile.TarInfo(name="../evil.txt")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))

            with self.assertRaises(ValueError):
                self.gpu_modal_app._safe_extract_tar(archive_path, destination)

    def test_materialize_workspace_archive_raises_when_payload_missing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="modal_materialize_") as tmp:
            staging_dir = pathlib.Path(tmp)
            with self.assertRaises(ValueError):
                self.gpu_modal_app._materialize_workspace_archive({}, staging_dir, {})

    def test_submit_rejects_invalid_execution_mode(self) -> None:
        with tempfile.TemporaryDirectory(prefix="modal_submit_") as tmp:
            payload_path = pathlib.Path(tmp) / "payload.json"
            payload_path.write_text(json.dumps({"run_id": "abc"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.gpu_modal_app.submit(str(payload_path), execution_mode="invalid")


if __name__ == "__main__":
    unittest.main()
