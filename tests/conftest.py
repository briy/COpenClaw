"""Pytest session-wide fixtures for COpenClaw tests.

Prevents test runs from polluting the production log directory
(~/.copenclaw/.logs/copenclaw.log) by redirecting all logging to
a temporary directory.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _isolate_test_logging(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Redirect COpenClaw logging to a disposable temp directory.

    ``create_app()`` calls ``setup_logging(log_dir=settings.log_dir)``
    which — without this fixture — writes to the real production log at
    ``~/.copenclaw/.logs/copenclaw.log``.  Setting the env vars here
    ensures ``Settings.from_env()`` picks up the temp directory instead.
    """
    log_dir = str(tmp_path_factory.mktemp("copenclaw-test-logs"))
    data_dir = str(tmp_path_factory.mktemp("copenclaw-test-data"))

    os.environ["copenclaw_LOG_DIR"] = log_dir
    os.environ["copenclaw_DATA_DIR"] = data_dir


@pytest.fixture(autouse=True)
def _reset_log_handlers() -> None:
    """Remove file handlers added by setup_logging() after each test.

    Without this, a RotatingFileHandler from a previous test may keep the
    temp-dir log file open, causing issues on Windows (locked files) and
    leaking handlers across tests.
    """
    yield
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers
        if not isinstance(h, logging.FileHandler)
    ]
