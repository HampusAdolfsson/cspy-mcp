from __future__ import annotations

import pytest

from iar_cspy.testing import fake_client
from iar_cspy_mcp import server as server_module


@pytest.fixture()
def server():
    return server_module


@pytest.fixture()
def fake():
    """(client, rpc) installed as the server's client, with a started session.

    Most tools refuse to run without a session, and these tests exercise the
    tools, not the lifecycle; tests about the lifecycle reset the state.
    """
    client, rpc = fake_client()
    client.debugger.set_state(configured=True, started=True)
    server_module.set_client(client)
    yield client, rpc
    server_module.set_client(None)
    client.close()
