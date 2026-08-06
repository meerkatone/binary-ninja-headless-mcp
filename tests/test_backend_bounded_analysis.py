"""Analysis waits are bounded so they cannot outlive the MCP client request timeout."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
from binary_ninja_headless_mcp import backend as backend_mod
from binary_ninja_headless_mcp.backend import BinjaBackend
from binary_ninja_headless_mcp.fake_binja import FakeBinaryNinjaModule, FakeBinaryView
from binary_ninja_headless_mcp.server import SimpleMcpServer

# Budget used by these tests; small enough to keep them fast.
WAIT_BUDGET_S = 0.2

# Blocking analyses wait on an event instead of sleeping, so teardown releases them
# immediately. The value only needs to be far above WAIT_BUDGET_S.
SLOW_ANALYSIS_S = 30.0

# Upper bound for "returned without waiting for the analysis". Generous enough to
# survive a loaded CI machine while staying far below SLOW_ANALYSIS_S.
RETURN_DEADLINE_S = 5.0


class _AnalysisControl:
    """Knobs shared by every fake view: how long the analysis blocks, and if it fails."""

    def __init__(self) -> None:
        self.delay = 0.0
        self.fail = False
        self.release = threading.Event()


class _FakeAnalysisProgress:
    def __init__(self) -> None:
        self.state = "IdleState"
        self.count = 0
        self.total = 0


class _FakeAnalysisInfo:
    def __init__(self) -> None:
        self.state = "IdleState"
        self.analysis_time = 0.0
        self.active_info: list[Any] = []


class _AnalysisFakeView(FakeBinaryView):
    """Fake view whose analysis can be made slow or failing on demand."""

    def __init__(self, filename: str, control: _AnalysisControl):
        super().__init__(filename)
        self._control = control
        self.analysis_state = "IdleState"
        self.analysis_progress = _FakeAnalysisProgress()
        self.analysis_info = _FakeAnalysisInfo()

    def update_analysis_and_wait(self) -> None:
        if self._control.delay:
            self._control.release.wait(self._control.delay)
        if self._control.fail:
            raise RuntimeError("analysis exploded")


class _AnalysisFakeModule(FakeBinaryNinjaModule):
    def __init__(self, control: _AnalysisControl):
        self._control = control

    def load(
        self,
        path: str,
        update_analysis: bool = True,
        options: dict[str, Any] | None = None,
    ) -> _AnalysisFakeView:
        _ = (update_analysis, options)
        return _AnalysisFakeView(filename=path, control=self._control)


@pytest.fixture
def control() -> _AnalysisControl:
    return _AnalysisControl()


@pytest.fixture
def fake_backend(
    monkeypatch: pytest.MonkeyPatch,
    control: _AnalysisControl,
) -> Iterator[BinjaBackend]:
    monkeypatch.setattr(backend_mod, "ANALYSIS_WAIT_BUDGET_S", WAIT_BUDGET_S)
    backend = BinjaBackend(_AnalysisFakeModule(control))
    yield backend
    control.release.set()
    backend.shutdown()


def _open(backend: BinjaBackend, path: str) -> str:
    summary = backend.open_session(path, update_analysis=False, deterministic=False)
    return str(summary["session_id"])


def test_update_and_wait_returns_completed_status_within_budget(
    fake_backend: BinjaBackend,
) -> None:
    session_id = _open(fake_backend, "/fake/fast")

    result = fake_backend.analysis_update_and_wait(session_id)

    assert result["wait_completed"] is True
    assert result["task_id"]
    # The pre-existing analysis_status payload is still returned unchanged.
    assert {"session_id", "state", "is_aborted", "progress", "info"} <= set(result)


def test_update_and_wait_returns_task_handle_past_budget(
    fake_backend: BinjaBackend,
    control: _AnalysisControl,
) -> None:
    session_id = _open(fake_backend, "/fake/slow")
    control.delay = SLOW_ANALYSIS_S

    started = time.monotonic()
    result = fake_backend.analysis_update_and_wait(session_id)
    elapsed = time.monotonic() - started

    assert elapsed < RETURN_DEADLINE_S
    assert result["wait_completed"] is False
    assert result["task_id"]
    assert fake_backend.task_status(result["task_id"])["status"] in {"queued", "running"}


def test_update_and_wait_tool_is_bounded(
    fake_backend: BinjaBackend,
    control: _AnalysisControl,
) -> None:
    server = SimpleMcpServer(fake_backend)
    session_id = _open(fake_backend, "/fake/slow")
    control.delay = SLOW_ANALYSIS_S

    started = time.monotonic()
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "analysis.update_and_wait",
                "arguments": {"session_id": session_id},
            },
        }
    )
    elapsed = time.monotonic() - started

    assert response is not None
    assert "error" not in response
    assert elapsed < RETURN_DEADLINE_S

    structured = response["result"]["structuredContent"]
    assert structured["wait_completed"] is False
    assert structured["task_id"]
