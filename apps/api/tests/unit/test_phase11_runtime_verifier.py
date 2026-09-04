import socket

import pytest
from scripts.verify_phase11_runtime import _reserve_loopback_port


def test_phase11_runtime_verifier_reserves_an_ephemeral_loopback_port() -> None:
    reservation, port = _reserve_loopback_port()
    competitor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        assert reservation.getsockname() == ("127.0.0.1", port)
        with pytest.raises(OSError):
            competitor.bind(("127.0.0.1", port))
    finally:
        competitor.close()
        reservation.close()
