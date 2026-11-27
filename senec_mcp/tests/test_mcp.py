"""MCP server tests: offline registration/dry-run/validation + one stdio e2e (no live writes)."""

import asyncio
import json
import os
import shutil
import sys

import pytest

from senec_mcp import mcp_server
from senec_mcp.utils import senec_backend as be

EXPECTED = set(mcp_server.READ_TOOLS) | set(mcp_server.WRITE_TOOLS)
FORBIDDEN_WORDS = ("setup", "login", "test_charge", "test_discharge", "testpower",
                   "control_set", "raw_set", "storage_mode")


class FakeWeb:
    def __init__(self):
        self.writes = []

    def get_reserve(self, n):
        return 20

    def set_reserve(self, n, v):
        self.writes.append(("reserve", v))

    def get_peak_shaving(self, n):
        return {"peakShavingMode": "AUTO", "peakShavingCapacityLimitInPercent": 70,
                "peakShavingEndDate": 1_900_000_000_000}

    def set_peak_shaving(self, *a):
        self.writes.append(("peak", a))


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("SENEC_HOST", raising=False)
    cfg = mcp_server.Config(host="https://example.invalid",
                            session_file=str(tmp_path / "s.json"))
    web = FakeWeb()
    cfg.web = lambda: (web, {"id": "sys1", "controlUnitNumber": "X"}, 0, {})
    return mcp_server.build_server(cfg), cfg, web


def _tools(srv):
    return {t.name: t for t in asyncio.run(srv.list_tools())}


def _call(srv, name, args):
    res = asyncio.run(srv.call_tool(name, args))
    # FastMCP returns (content, structured) in recent versions
    if isinstance(res, tuple):
        res = res[1]
        return res.get("result", res)
    return json.loads(res[0].text)


class TestRegistration:
    def test_exact_tool_list(self, server):
        assert set(_tools(server[0])) == EXPECTED

    def test_no_forbidden_tools(self, server):
        for name in _tools(server[0]):
            assert not any(w in name for w in FORBIDDEN_WORDS), name

    def test_write_tools_have_confirm_default_false_and_warning(self, server):
        tools = _tools(server[0])
        for name in mcp_server.WRITE_TOOLS:
            t = tools[name]
            assert t.inputSchema["properties"]["confirm"]["default"] is False
            assert "confirm=true" in t.description and "user" in t.description

    def test_read_tools_have_no_confirm(self, server):
        tools = _tools(server[0])
        for name in mcp_server.READ_TOOLS:
            assert "confirm" not in tools[name].inputSchema.get("properties", {})


class TestDryRun:
    def test_safe_charge_dry_run_no_network(self, server, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("network write attempted")
        monkeypatch.setattr(be, "write_var", boom)
        monkeypatch.setattr(be, "query", boom)
        r = _call(server[0], "safe_charge", {"mode": "on"})
        assert r["applied"] is False and r["request"] == {"ENERGY": {"SAFE_CHARGE_FORCE": "u8_01"}}

    def test_reserve_dry_run(self, server):
        srv, _, web = server
        r = _call(srv, "set_reserve", {"percent": 30})
        assert r["applied"] is False and r["old"] == {"reserve_percent": 20}
        assert r["new"] == {"reserve_percent": 30} and web.writes == []

    def test_peak_dry_run_keeps_fields(self, server):
        srv, _, web = server
        r = _call(srv, "set_peak_shaving", {"capacity": 50})
        assert r["applied"] is False and r["new"]["mode"] == "AUTO"
        assert r["new"]["capacity_limit_percent"] == 50 and web.writes == []

    def test_reserve_confirm_writes_and_verifies(self, server):
        srv, _, web = server
        web.get_reserve = lambda n: 30 if web.writes else 20
        r = _call(srv, "set_reserve", {"percent": 30, "confirm": True})
        assert r["applied"] is True and r["verified"] is True and web.writes == [("reserve", 30)]


class TestValidation:
    @pytest.mark.parametrize("name,args", [
        ("safe_charge", {"mode": "maybe"}),
        ("set_reserve", {"percent": 60}),
        ("set_reserve", {"percent": -1}),
        ("set_peak_shaving", {"mode": "TURBO"}),
        ("set_peak_shaving", {"capacity": 101}),
        ("set_peak_shaving", {"end": "25:99"}),
        ("cloud_history", {"resolution": "SECOND"}),
    ])
    def test_rejects(self, server, name, args):
        with pytest.raises(Exception):
            _call(server[0], name, args)
        assert server[2].writes == []

    def test_unreachable_device_message(self, server):
        with pytest.raises(Exception, match="unreachable"):
            _call(server[0], "status", {})

    def test_host_from_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SENEC_HOST", "senec.local")
        cfg = mcp_server.Config(session_file=str(tmp_path / "s.json"))
        assert cfg.conn["host"] == "https://senec.local"

    def test_missing_env_file(self, tmp_path):
        cfg = mcp_server.Config(env_file=str(tmp_path / "nope.env"),
                                session_file=str(tmp_path / "s.json"))
        with pytest.raises(FileNotFoundError):
            cfg.creds()


# ── e2e: real stdio server, real device read, dry-run write only ──
def _server_cmd():
    path = shutil.which("senec-mcp")
    if path:
        return path, []
    if os.environ.get("SENEC_FORCE_INSTALLED", "").strip() == "1":
        raise RuntimeError("senec-mcp not found in PATH")
    return sys.executable, ["-m", "senec_mcp.mcp_server"]


def test_e2e_stdio_status_and_dry_run():
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd, args = _server_cmd()
    params = StdioServerParameters(command=cmd, args=args, env={**os.environ})

    async def run():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                assert names == EXPECTED
                st = await s.call_tool("status", {})
                assert not st.isError, st.content
                status = json.loads(st.content[0].text)
                assert 0 <= status["soc_percent"] <= 100
                dr = await s.call_tool("safe_charge", {"mode": "on"})
                assert not dr.isError
                plan = json.loads(dr.content[0].text)
                assert plan["applied"] is False and plan["dry_run"] is True
                print(f"\n  mcp status: {status}")
    asyncio.run(run())
