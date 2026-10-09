"""The HTTP server the suite posts events to (``conftest.make_httpserver``)."""

from __future__ import annotations

import socket

import httpx
from pytest_httpserver import HTTPServer


def test_an_idle_connection_does_not_stall_the_event_server(httpserver: HTTPServer) -> None:
    httpserver.expect_request("/v0/events", method="POST").respond_with_data("", status=201)
    with socket.create_connection((httpserver.host, httpserver.port)):
        response = httpx.post(httpserver.url_for("/v0/events"), json={}, timeout=2.0)
    assert response.status_code == 201
