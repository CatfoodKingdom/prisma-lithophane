from __future__ import annotations

from io import BytesIO, TextIOWrapper

import pytest

from Prisma.lib.console_output import configure_console_streams


@pytest.mark.parametrize("encoding", ["ascii", "cp1252"])
def test_console_streams_escape_paths_their_encoding_cannot_represent(encoding: str) -> None:
    raw = BytesIO()
    stream = TextIOWrapper(raw, encoding=encoding, errors="strict")

    configure_console_streams((stream,))
    stream.write("Images: C:\\portable Ω\\图.png")
    stream.flush()

    rendered = raw.getvalue().decode(encoding)
    assert stream.encoding == encoding
    assert stream.errors == "backslashreplace"
    assert r"\u03a9" in rendered
    assert r"\u56fe" in rendered


def test_console_stream_configuration_tolerates_host_streams_without_reconfigure() -> None:
    configure_console_streams((None, object()))


def test_console_stream_configuration_tolerates_host_reconfigure_failure() -> None:
    class HostStream:
        def reconfigure(self, **_kwargs: object) -> None:
            raise RuntimeError("host stream refuses reconfiguration")

    configure_console_streams((HostStream(),))
