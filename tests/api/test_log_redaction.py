import logging

from lumi.api.main import RedactToken


def _line(msg, *args, name="uvicorn.error"):
    record = logging.LogRecord(name, logging.INFO, __file__, 0, msg, args, None)
    assert RedactToken().filter(record)
    return record.getMessage()


def test_the_ws_upgrade_line_does_not_carry_the_token():
    line = _line('%s - "WebSocket %s" [accepted]', "100.69.122.4:37054", "/ws?token=eyJ.abc.def")
    assert "eyJ" not in line
    assert line == '100.69.122.4:37054 - "WebSocket /ws?token=<redacted>" [accepted]'


def test_other_query_parameters_and_lines_are_left_alone():
    line = _line('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/x?a=1&token=t0k&b=2", "1.1", 200,
                 name="uvicorn.access")
    assert line == '1.2.3.4:5 - "GET /x?a=1&token=<redacted>&b=2 HTTP/1.1" 200'
    assert _line("plain ?tokens=keep") == "plain ?tokens=keep"


def test_the_filter_is_on_uvicorns_loggers():
    for name in ("uvicorn.access", "uvicorn.error"):
        assert any(isinstance(f, RedactToken) for f in logging.getLogger(name).filters)
