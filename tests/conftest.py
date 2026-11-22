import pytest

from bookly.agent.state import Session
from bookly.config import Settings
from bookly.data.repo import Repo
from bookly.tools import run_tool
from bookly.tools.common import ToolDeps
from bookly.tools.knowledge import PolicyIndex


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "test.db")
    r.reset()
    return r


@pytest.fixture
def deps(repo, tmp_path):
    return ToolDeps(repo=repo, settings=Settings(db_path=tmp_path / "test.db"), kb=PolicyIndex())


@pytest.fixture
def session():
    s = Session()
    s.turn = 1
    return s


@pytest.fixture
def call(deps):
    def _call(s, name, **args):
        return run_tool(s, name, args, deps)
    return _call


def verify(call, s, order="4801", email="ana.ruiz@example.com"):
    out = call(s, "verify_customer", order_number=order, email=email)
    assert out.kind == "ok", out.result
    return out


def unlock(s, route):
    from bookly.agent import gate
    s.ctx.route = route
    s.ctx.write_tools = gate.ROUTE_WRITE_TOOLS.get(route, frozenset())
