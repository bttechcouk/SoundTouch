"""Shared pytest fixtures for the SoundTouch controller test suite.

The controller is a single module (`soundtouch_controller.py`) at the repo root;
importing it has no side effects (the server only starts under `__main__`), so we
can import it directly and exercise the pure/near-pure logic.
"""
import pathlib
import sys

import pytest

# Make the repo-root module importable regardless of where pytest is invoked.
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import soundtouch_controller as stc  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """A PresetStore backed by isolated temp directories."""
    return stc.PresetStore(presets_dir=tmp_path / "presets",
                           stations_dir=tmp_path / "stations")


@pytest.fixture
def dlna(store):
    """A DLNAServer wired to the temp-backed store (no sockets opened)."""
    return stc.DLNAServer(uuid="test-uuid", http_port=8888,
                          local_ip="192.168.1.10", store=store)
