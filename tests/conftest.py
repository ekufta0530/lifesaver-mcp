import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def load_fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    """Minimal stand-in for requests.Response for offline tests."""

    def __init__(self, *, text="", content=b"", url="", status_code=200, history=None, headers=None):
        self.text = text
        self.content = content or text.encode()
        self.url = url
        self.status_code = status_code
        self.history = history or []
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """
    Records the last GET/POST and returns a queued FakeResponse.

    Set `.next_response` (or `.responses` for a sequence) before the call.
    """

    def __init__(self):
        self.calls = []
        self.next_response = None
        self.responses = []

    def _pop(self):
        if self.responses:
            return self.responses.pop(0)
        return self.next_response

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, {"params": params, **kw}))
        return self._pop()

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, {"data": data, **kw}))
        return self._pop()


@pytest.fixture
def fake_session():
    return FakeSession()
