import json
import logging

from shared.logging_config import JSONFormatter, configure_logging


def _make_record(**extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="something happened: %s",
        args=("detail",),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_json_formatter_produces_valid_json_with_expected_keys():
    formatter = JSONFormatter()
    record = _make_record()

    line = formatter.format(record)
    payload = json.loads(line)

    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "something happened: detail"
    assert "timestamp" in payload


def test_json_formatter_includes_extra_fields():
    formatter = JSONFormatter()
    record = _make_record(dataset="mFRR_capacity", saved=42)

    payload = json.loads(formatter.format(record))

    assert payload["dataset"] == "mFRR_capacity"
    assert payload["saved"] == 42


def test_json_formatter_includes_exception_info():
    formatter = JSONFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record()
        record.exc_info = sys.exc_info()

    payload = json.loads(formatter.format(record))

    assert "exception" in payload
    assert "boom" in payload["exception"]


def test_configure_logging_installs_single_json_handler():
    configure_logging()
    configure_logging()  # idempotent -- must not accumulate handlers

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JSONFormatter)
