"""The default suite refuses accidental external network access before I/O."""

import socket

import pytest


@pytest.mark.parametrize("host", ["example.invalid", "192.0.2.1", "2001:db8::1"])
def test_external_dns_is_blocked(host):
    with pytest.raises(RuntimeError, match="External network is disabled"):
        socket.getaddrinfo(host, 443)


@pytest.mark.parametrize("operation", ["connect", "connect_ex"])
def test_direct_external_connections_are_blocked(operation):
    with socket.socket() as sock:
        with pytest.raises(RuntimeError, match="External network is disabled"):
            getattr(sock, operation)(("192.0.2.1", 443))


def test_loopback_fixture_connections_remain_available():
    with socket.socket() as listener, socket.socket() as client:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        client.settimeout(1)
        client.connect(listener.getsockname())
        connection, _ = listener.accept()
        connection.close()
