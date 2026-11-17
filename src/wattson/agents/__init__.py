"""The three agents: orchestrator, triage, knowledge."""

import os

# a2a-sdk traces its own internals (event queues, handlers): about 180 spans per task, nothing a
# person reads. It reads this switch when it is imported, so it is set here, before any agent
# imports it. Set OTEL_INSTRUMENTATION_A2A_SDK_ENABLED=true to see them again.
os.environ.setdefault("OTEL_INSTRUMENTATION_A2A_SDK_ENABLED", "false")
