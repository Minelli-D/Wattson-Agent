"""The port-forward to the orchestrator, with a fake kubectl."""

from __future__ import annotations

import json

import pytest
from cli_fakes import FIXTURES

from wattson.cli.config import Config
from wattson.cli.portforward import PortForward, PortForwardError, parse_forward_line

CFG = Config(k8s_context="my-cluster", namespace="grimoire")


def test_parse_forward_line():
    assert parse_forward_line("Forwarding from 127.0.0.1:41235 -> 9000\n") == 41235
    assert parse_forward_line("Forwarding from [::1]:41235 -> 9000") is None
    assert parse_forward_line("Handling connection for 41235") is None


@pytest.fixture
def kubectl(monkeypatch, tmp_path):
    log = tmp_path / "kubectl.log"
    monkeypatch.setenv("WATTSON_KUBECTL_BIN", str(FIXTURES / "fake_kubectl"))
    monkeypatch.setenv("FAKE_KUBECTL_LOG", str(log))
    monkeypatch.setenv("FAKE_KUBECTL_PORT", "45678")
    return log


def test_start_and_stop(kubectl):
    pf = PortForward(CFG)
    assert pf.start() == "http://127.0.0.1:45678"
    assert pf.alive()
    proc = pf.proc
    pf.stop()
    assert not pf.alive() and proc.poll() is not None
    args = json.loads(kubectl.read_text().splitlines()[0])
    assert args == [
        "--context",
        "my-cluster",
        "-n",
        "grimoire",
        "port-forward",
        "deploy/wattson-orchestrator",
        "0:9000",
        "--address",
        "127.0.0.1",
    ]


def test_reopens_when_it_drops(kubectl):
    messages = []
    pf = PortForward(CFG, log=messages.append)
    pf.start()
    first = pf.proc
    first.kill()
    first.wait()
    assert pf.ensure() == "http://127.0.0.1:45678"
    assert pf.proc is not first and pf.alive()
    assert "dropped" in messages[0]
    pf.stop()


def test_kubectl_error_is_shown(kubectl, monkeypatch):
    monkeypatch.setenv("FAKE_KUBECTL_PF", "fail")
    with pytest.raises(PortForwardError, match="not found"):
        PortForward(CFG).start()


def test_timeout(kubectl, monkeypatch):
    monkeypatch.setenv("FAKE_KUBECTL_PF", "hang")
    pf = PortForward(CFG, timeout_s=1)
    with pytest.raises(PortForwardError, match="did not start within 1 seconds"):
        pf.start()
    assert pf.proc is None


def test_no_kubectl(monkeypatch, tmp_path):
    monkeypatch.setenv("WATTSON_KUBECTL_BIN", str(tmp_path / "nope"))
    with pytest.raises(PortForwardError, match="kubectl was not found"):
        PortForward(CFG).start()


def test_status_through_the_port_forward(env, cli, monkeypatch):
    """No WATTSON_URL: the CLI reads config.json, starts (fake) kubectl port-forward, and talks to
    the agent on the port kubectl printed."""
    monkeypatch.delenv("WATTSON_URL")
    monkeypatch.setenv("FAKE_KUBECTL_PORT", str(env.orchestrator.port))
    (env.config / "config.json").write_text(
        json.dumps({"k8s_context": "my-cluster", "namespace": "grimoire"})
    )
    r = cli(["status"])
    assert r.code == 0, r.out
    assert "version 0.1.0" in r.out
    calls = [json.loads(line) for line in env.kubectl_log.read_text().splitlines()]
    assert any("port-forward" in c for c in calls)
