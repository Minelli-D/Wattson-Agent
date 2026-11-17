"""No OTEL_EXPORTER_OTLP_ENDPOINT: telemetry is fully off. Checked in a fresh process, because the
other tests of this folder turn it on for theirs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

SCRIPT = textwrap.dedent(
    """
    import json, os, threading
    from pathlib import Path
    from opentelemetry import trace
    from opentelemetry.instrumentation.httpx import HTTPX2ClientInstrumentor, HTTPXClientInstrumentor

    from wattson.agents.knowledge.app import create_app as knowledge_app
    from wattson.agents.knowledge.settings import Settings as KnowledgeSettings
    from wattson.agents.orchestrator.app import create_app as orchestrator_app
    from wattson.agents.orchestrator.settings import Settings as OrchestratorSettings
    from wattson.agents.triage.app import create_app as triage_app
    from wattson.agents.triage.settings import Settings as TriageSettings
    from wattson.shared import telemetry

    threads = threading.active_count()
    on = telemetry.setup_telemetry("wattson-orchestrator")
    token = "x" * 40
    data = Path(os.environ["T_DIR"])
    orchestrator = orchestrator_app(
        OrchestratorSettings(token=token, triage_url="http://t", triage_token=token,
                             knowledge_url="http://k", knowledge_token=token, data_dir=data / "o"),
        background=False,
    )
    triage = triage_app(TriageSettings(token=token))
    knowledge = knowledge_app(KnowledgeSettings(token=token, data_dir=data / "k"))
    with telemetry.span("wattson.step", **{"wattson.task_id": "t-1"}) as span:
        recording = span.is_recording()
    import a2a.utils.telemetry as a2a_telemetry

    print(json.dumps({
        "on": on,
        "enabled": telemetry.enabled(),
        "provider": type(trace.get_tracer_provider()).__name__,
        "new_threads": threading.active_count() - threads,
        "httpx_instrumented": HTTPXClientInstrumentor().is_instrumented_by_opentelemetry,
        "httpx2_instrumented": HTTPX2ClientInstrumentor().is_instrumented_by_opentelemetry,
        "opt_in": os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN"),
        "middleware": [m.cls.__name__ for m in orchestrator.user_middleware],
        "triage_app": type(triage).__name__,
        "knowledge_app": type(knowledge).__name__,
        "recording": recording,
        "a2a_tracing": a2a_telemetry.otel_enabled,
    }))
    """
)


def test_no_endpoint_no_telemetry(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env["T_DIR"] = str(tmp_path)
    done = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    facts = json.loads(done.stdout.strip().splitlines()[-1])
    assert facts == {
        "on": False,
        "enabled": False,
        "provider": "ProxyTracerProvider",  # the API's no-op: no SDK provider was set up
        "new_threads": 0,  # no exporter thread
        "httpx_instrumented": False,
        "httpx2_instrumented": False,
        "opt_in": None,
        "middleware": ["AccessLog", "BearerAuth"],  # no trace middleware
        "triage_app": "BearerAuth",  # not wrapped
        "knowledge_app": "BearerAuth",
        "recording": False,
        "a2a_tracing": False,  # a2a-sdk's own internal spans stay off by default
    }
