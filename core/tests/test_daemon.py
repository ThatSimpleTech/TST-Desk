"""Tests for the daemon lifecycle and logging subsystem."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path

import pytest

from tstd.daemon import Daemon, DaemonState
from tstd.logging import (
    JSONFormatter,
    SecretsRedactionFilter,
    setup_logging,
    user_data_dir,
)


class TestDaemonState:
    def test_initial_state(self) -> None:
        s = DaemonState()
        assert s.active_sessions == 0
        assert s.uptime >= 0

    def test_uptime_increases(self) -> None:
        import time

        s = DaemonState()
        t1 = s.uptime
        time.sleep(0.01)
        t2 = s.uptime
        assert t2 > t1


class TestDaemonLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self) -> None:
        d = Daemon()

        async def stop() -> None:
            await asyncio.sleep(0.2)
            d._shutdown_event.set()

        await asyncio.gather(d.run(), stop())
        assert d._shutdown_event.is_set() is True
        assert d.state.uptime >= 0.2

    @pytest.mark.asyncio
    async def test_health(self) -> None:
        d = Daemon()

        async def stop() -> None:
            await asyncio.sleep(0.2)
            d._shutdown_event.set()

        await asyncio.gather(d.run(), stop())
        h = d.health()
        assert h["version"] == "0.1.0"
        assert h["uptime"] > 0
        assert h["active_sessions"] == 0
        assert h["data_dir"] is not None
        assert h["shutdown_requested"] is True

    @pytest.mark.asyncio
    async def test_multiple_shutdown_requests_idempotent(self) -> None:
        d = Daemon()

        async def stop() -> None:
            await asyncio.sleep(0.1)
            d._shutdown_event.set()
            d._shutdown_event.set()  # second call should be harmless

        await asyncio.gather(d.run(), stop())
        assert d._shutdown_event.is_set() is True


class TestLogging:
    def test_json_formatter_output(self) -> None:
        logger = logging.getLogger("test.json")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)

        logger.info("hello", extra={"extra_fields": {"user": "alice"}})
        record = json.loads(buf.getvalue())
        assert record["level"] == "INFO"
        assert record["message"] == "hello"
        assert record["logger"] == "test.json"
        assert record["user"] == "alice"
        assert "ts" in record

    def test_json_formatter_exception(self) -> None:
        logger = logging.getLogger("test.exception")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)

        try:
            raise ValueError("test error")
        except ValueError:
            logger.exception("something failed")

        record = json.loads(buf.getvalue())
        assert record["level"] == "ERROR"
        assert "exception" in record
        assert "ValueError" in record["exception"]
        assert "test error" in record["exception"]

    def test_secrets_redaction_filter_openai_key(self) -> None:
        logger = logging.getLogger("test.secrets")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretsRedactionFilter())
        logger.addHandler(handler)

        logger.info("api key is sk-abcdefghijklmnopqrstuvwxyz123456")  # tst-secret-ok
        msg = json.loads(buf.getvalue())["message"]
        assert "[REDACTED]" in msg
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in msg  # tst-secret-ok

    def test_secrets_redaction_filter_aws_key(self) -> None:
        logger = logging.getLogger("test.aws")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretsRedactionFilter())
        logger.addHandler(handler)

        logger.info("access key: AKIAIOSFODNN7EXAMPLE")  # tst-secret-ok
        msg = json.loads(buf.getvalue())["message"]
        assert "[REDACTED]" in msg
        assert "AKIAIOSFODNN7EXAMPLE" not in msg  # tst-secret-ok

    def test_clean_message_not_redacted(self) -> None:
        logger = logging.getLogger("test.clean")
        logger.handlers.clear()
        logger.setLevel(logging.DEBUG)

        import io

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(JSONFormatter())
        handler.addFilter(SecretsRedactionFilter())
        logger.addHandler(handler)

        logger.info("hello world, this is a normal message")
        msg = json.loads(buf.getvalue())["message"]
        assert msg == "hello world, this is a normal message"

    def test_setup_logging_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            setup_logging(level="DEBUG", log_dir=log_dir, log_to_stdout=False)
            try:
                logger = logging.getLogger("test.file")
                logger.info("written to file")
                # Check the log file was created
                log_files = list(log_dir.glob("*.log"))
                assert len(log_files) >= 1
                content = log_files[0].read_text()
                assert "written to file" in content
                assert json.loads(content.strip().split("\n")[0])["level"] == "INFO"
            finally:
                # Windows cannot unlink an open file: release the log file
                # before TemporaryDirectory cleanup runs. Only file handlers
                # are touched — pytest's own capture handler stays attached.
                root = logging.getLogger()
                for handler in root.handlers[:]:
                    if isinstance(handler, logging.FileHandler):
                        handler.close()
                        root.removeHandler(handler)

    def test_user_data_dir_returns_path(self) -> None:
        path = user_data_dir()
        assert isinstance(path, Path)
        assert str(path)  # non-empty


class TestSignalHandling:
    @pytest.mark.asyncio
    async def test_signal_requests_shutdown(self) -> None:
        """Signal handler sets shutdown event."""
        d = Daemon()
        assert d._shutdown_event.is_set() is False
        d._on_signal()
        assert d._shutdown_event.is_set() is True

    @pytest.mark.asyncio
    async def test_signal_handles_multiple_signals(self) -> None:
        """Multiple signals don't cause errors."""
        d = Daemon()
        d._on_signal()
        d._on_signal()  # second signal, already shutting down
        assert d._shutdown_event.is_set() is True
