"""Connection UI contracts: secrets, atomic assignments and actual launch routing."""
import asyncio
import json
import os
import shutil
import subprocess

import pytest


def save(t, body):
    return t.connections_save(json.dumps(body).encode())


def test_add_proxy_from_fields_encodes_credentials_and_preserves_other_settings(t):
    result = save(t, {"proxy": {"name": "new", "scheme": "http", "host": "proxy.example",
                               "port": "8080", "username": "a@b", "password": "p:$ /"}})
    assert result["ok"]
    cfg = json.load(open(t.CONFIG_FILE))
    assert cfg["proxies"][-1] == {"name": "new", "url": "http://a%40b:p%3A%24%20%2F@proxy.example:8080"}
    assert cfg["agents"] == t.DEFAULTS["agents"]
    assert os.stat(t.CONFIG_FILE).st_mode & 0o777 == 0o600
    assert "p%3A%24" not in json.dumps(result)


@pytest.mark.parametrize("url", ["http://h:0", "http://h:65536", "ftp://h:80", "http://h:80/path",
                                 'http://u:pw"@h:80', "http://h:80\nurl=https://evil.example",
                                 "http://h:80?token=secret", "http://h:80#fragment"])
def test_invalid_proxy_never_changes_config(t, url):
    before = open(t.CONFIG_FILE).read()
    result = save(t, {"proxy": {"name": "new", "url": url}})
    assert result["ok"] is False
    assert open(t.CONFIG_FILE).read() == before
    assert "secret" not in result["error"]


def test_url_ipv6_and_duplicate_name(t):
    assert save(t, {"proxy": {"name": "ipv6", "url": "socks5h://[::1]:1080"}})["ok"]
    before = open(t.CONFIG_FILE).read()
    assert not save(t, {"proxy": {"name": "eu-1", "url": "http://other:80"}})["ok"]
    assert open(t.CONFIG_FILE).read() == before


def test_assignments_are_per_agent_and_atomic(t):
    assert save(t, {"defaults": {"claude": "eu-1", "codex": "direct"}})["ok"]
    assert t.CFG["agents"]["claude"]["proxy"] == "eu-1"
    assert t.CFG["agents"]["codex"]["proxy"] == "direct"
    before = open(t.CONFIG_FILE).read()
    assert not save(t, {"proxy": {"name": "new", "url": "http://h:80"},
                        "defaults": {"claude": "missing"}})["ok"]
    assert open(t.CONFIG_FILE).read() == before
    assert save(t, {"proxy": {"name": "new", "url": "http://h:80"},
                    "defaults": {"codex": "new"}})["ok"]
    assert t.CFG["agents"]["claude"]["proxy"] == "eu-1"
    assert t.CFG["agents"]["codex"]["proxy"] == "new"


@pytest.mark.parametrize("body", [[], {"defaults": {"pi": "direct"}}, {"defaults": {"codex": 1}},
                                  {"proxy": "http://h:80"}, {"defaults": "direct"}])
def test_malformed_connection_requests_are_rejected(t, body):
    before = open(t.CONFIG_FILE).read()
    assert save(t, body)["ok"] is False
    assert open(t.CONFIG_FILE).read() == before


def test_state_exposes_defaults_and_safe_addresses_without_prefix_secrets(t):
    t.CFG["agents"]["claude"]["prefix"] = "SECRET=hidden"
    t.CFG["agents"]["codex"]["proxy"] = "us-1"
    state = asyncio.run(t.connections_state())
    assert state["defaults"] == {"claude": "", "codex": "us-1"}
    assert state["routes"][0]["name"] == "eu-1"
    assert state["routes"][0]["hp"] == "203.0.113.10:3128"
    assert state["routes"][-1]["name"] == "direct"
    assert not any(secret in json.dumps(state) for secret in ("s3cret", "pa55", "hidden"))


def test_manual_check_reports_agents_independently_and_keeps_secrets_private(t, monkeypatch):
    async def probe(proxy, url):
        return {"code": 401 if "anthropic" in url else 403, "ms": 42}
    monkeypatch.setattr(t, "_curl", probe)
    result = asyncio.run(t.connections_check(json.dumps({"name": "eu-1"}).encode()))
    assert result["ok"]
    assert result["route"]["res"]["claude"]["code"] == 401
    assert result["route"]["res"]["codex"]["code"] == 403
    assert "s3cret" not in json.dumps(result)
    state = asyncio.run(t.connections_state())
    assert state["routes"][0]["res"]["claude"]["code"] == 401


def test_draft_check_does_not_save_but_result_survives_add(t, monkeypatch):
    async def probe(proxy, url):
        return {"code": 407, "ms": 10}
    monkeypatch.setattr(t, "_curl", probe)
    body = {"proxy": {"name": "new", "url": "http://u:pw@h:80"}}
    before = open(t.CONFIG_FILE).read()
    result = asyncio.run(t.connections_check(json.dumps(body).encode()))
    assert result["ok"] and result["route"]["res"]["claude"]["code"] == 407
    assert open(t.CONFIG_FILE).read() == before
    assert save(t, body)["ok"]
    state = asyncio.run(t.connections_state())
    assert next(r for r in state["routes"] if r["name"] == "new")["res"]["claude"]["code"] == 407


def test_forced_route_ignores_inherited_proxy_variables(t):
    t.CFG["proxies"] = [{"name": "test", "url": "http://selected.example:8080"}]
    env = dict(os.environ, http_proxy="http://wrong:1", https_proxy="http://wrong:1",
               ALL_PROXY="http://wrong:1", all_proxy="http://wrong:1", NO_PROXY="*", no_proxy="*")
    script = "printf '%s\\n' \"$HTTP_PROXY\" \"$HTTPS_PROXY\" \"${http_proxy-unset}\" \"${https_proxy-unset}\" \"${ALL_PROXY-unset}\" \"${all_proxy-unset}\" \"${NO_PROXY-unset}\" \"${no_proxy-unset}\""
    cmd = t.proxy_prefix("test") + " /bin/sh -c " + __import__("shlex").quote(script)
    lines = subprocess.check_output(["/bin/sh", "-c", cmd], env=env, text=True).splitlines()
    assert lines == ["http://selected.example:8080", "http://selected.example:8080"] + ["unset"] * 6


def test_curl_reports_proxy_auth_failure_from_connect(t):
    async def scenario():
        async def deny(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(deny, "127.0.0.1", 0)
        try:
            return await t._curl(f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
                                 "https://api.example/v1/models")
        finally:
            server.close()
            await server.wait_closed()
    assert asyncio.run(scenario())["code"] == 407


async def request(t, method, path, body=None, header=True):
    server = await asyncio.start_server(t.handle, "127.0.0.1", 0)
    data = json.dumps(body).encode() if body is not None else b""
    raw = (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n" +
           ("X-Toolbelt: 1\r\n" if header else "") + f"Content-Length: {len(data)}\r\n\r\n").encode() + data
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        writer.write(raw)
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()
    head, _, payload = response.partition(b"\r\n\r\n")
    return int(head.split()[1]), payload


@pytest.mark.parametrize("path", ["/connections/save", "/connections/check"])
def test_connection_mutations_require_post_and_header(t, path):
    assert asyncio.run(request(t, "POST", path, header=False))[0] == 403
    assert asyncio.run(request(t, "GET", path))[0] == 403


def test_connection_http_roundtrip_and_error_status(t, monkeypatch):
    async def probe(proxy, url):
        return {"code": 401, "ms": 1}
    monkeypatch.setattr(t, "_curl", probe)
    code, payload = asyncio.run(request(t, "POST", "/connections/save", {"defaults": {"codex": "direct"}}))
    assert code == 200 and json.loads(payload)["ok"]
    assert asyncio.run(request(t, "POST", "/connections/save", {"defaults": {"codex": "bad"}}))[0] == 400
    code, payload = asyncio.run(request(t, "POST", "/connections/check", {"name": "eu-1"}))
    assert code == 200 and json.loads(payload)["route"]["res"]["codex"]["code"] == 401
    assert asyncio.run(request(t, "GET", "/connections/state"))[0] == 200


def test_icons_are_local_and_asset_route_cannot_read_other_files(t):
    code, data = asyncio.run(request(t, "GET", "/assets/feather/settings.svg"))
    assert code == 200 and b"<svg" in data
    assert asyncio.run(request(t, "GET", "/assets/feather/../../agentbelt.py"))[0] == 404


def test_proxy_added_with_ipv6_can_be_saved_in_advanced_settings(t):
    assert save(t, {"proxy": {"name": "ipv6", "url": "http://u:pw@[::1]:8080"}})["ok"]
    masked = t.settings_state()["config"]
    assert t.settings_save(json.dumps({"proxies": masked["proxies"], "open_in": "tab"}).encode()) == "ok"
    assert next(p for p in t.CFG["proxies"] if p["name"] == "ipv6")["url"] == "http://u:pw@[::1]:8080"


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_existing_network_widget_never_marks_proxy_auth_or_rate_limit_as_green(t):
    import re
    from pathlib import Path
    html = (Path(t.HERE) / "pages" / "agent.html").read_text()
    expressions = "\n".join(re.findall(r"const (?:api|up)=.*", html))
    script = expressions + "\nconsole.log(JSON.stringify([401,407,429,403,200].map(code=>({code,up:!!up({code,ms:1}),html:api({code,ms:1})}))));"
    rows = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert [r["up"] for r in rows] == [True, False, False, False, True]
    for row in rows[1:4]:
        assert 'class="ok"' not in row["html"]
        assert str(row["code"]) in row["html"]
