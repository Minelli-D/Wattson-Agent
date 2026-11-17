"""Commands about this laptop and the connection: `sessions`, `status`, `k8s add`, `k8s list`."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from wattson.cli import localstate
from wattson.cli.a2a_client import AgentError
from wattson.cli.commands.common import ERROR, OK, STOPPED, UsageError, agent
from wattson.cli.config import (
    ConfigError,
    agent_url_override,
    config_path,
    file_mode,
    load_config,
    token_mode_problem,
    token_path,
)
from wattson.cli.review import ago
from wattson.cli.ui import UI, short


def add_parsers(sub: Any) -> None:
    p = sub.add_parser("sessions", help="list the Claude Code conversations of a repository")
    p.add_argument("--repo", metavar="PATH", help="the repository (default: the current folder)")
    p.set_defaults(func=run_sessions)

    p = sub.add_parser("status", help="check the settings, the token file and the agent")
    p.set_defaults(func=run_status)

    k8s = sub.add_parser("k8s", help="read-only access to Kubernetes clusters")
    ksub = k8s.add_subparsers(dest="k8s_command", metavar="COMMAND", required=True)
    p = ksub.add_parser("add", help="give Wattson Agent read-only access to a cluster (asks first)")
    p.add_argument("context", help="the kube context name, for example my-cluster")
    p.add_argument(
        "--namespace",
        help="an existing namespace for the read-only account (default: the namespace of Wattson Agent "
        "on the cluster that hosts it; needed for any other cluster)",
    )
    p.set_defaults(func=run_k8s_add)
    p = ksub.add_parser("list", help="list your kube contexts and which have read-only access")
    p.set_defaults(func=run_k8s_list)


def _folder(repo: str | None) -> Path:
    from wattson.connector import git as cgit

    cwd = Path(repo).expanduser().resolve() if repo else Path.cwd()
    if not cwd.is_dir():
        raise UsageError(f"There is no folder {cwd}.")
    found = cgit.detect_repo(cwd)
    if repo and found is None:
        raise UsageError(f"{cwd} is not inside a git repository.")
    return found[1] if found else cwd


def run_sessions(ui: UI, args: argparse.Namespace) -> int:
    from wattson.connector import claude as cc

    folder = _folder(args.repo)
    sessions = cc.list_sessions(folder)
    if not sessions:
        ui.info(f"No Claude Code conversation for {folder}.")
        return OK
    ui.table(
        ["Conversation", "Last used", "Messages", "First prompt", "Note"],
        [
            (
                s.id,
                ago(s.last_activity),
                s.messages,
                short(s.first_prompt, 60),
                "maybe open now" if s.maybe_open else "",
            )
            for s in sessions
        ],
        title=f"Claude Code conversations for {folder}",
    )
    ui.dim('Use one with: wattson ask "..." --session ID')
    return OK


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def run_status(ui: UI, args: argparse.Namespace) -> int:
    code = OK
    ui.heading("This laptop")
    url = agent_url_override()
    try:
        cfg = load_config()
    except ConfigError as exc:
        cfg = None
        ui.error(str(exc))
        code = ERROR
    tpath = token_path()
    mode = file_mode(tpath)
    problem = token_mode_problem(tpath)
    ui.kv(
        [
            ("Settings file", f"{config_path()}" + ("" if cfg else " (missing)")),
            ("Cluster context", cfg.k8s_context if cfg else None),
            ("Namespace", cfg.namespace if cfg else None),
            ("Deployment", cfg.deployment if cfg else None),
            ("Agent port", cfg.remote_port if cfg else None),
            ("WATTSON_URL", url or "not set (the CLI opens a port-forward)"),
            ("Token file", f"{tpath} (mode {mode:o})" if mode is not None else f"{tpath} (missing)"),
        ]
    )
    if problem:
        ui.warn(problem)
        code = ERROR
    unfinished = localstate.list_unfinished()
    if unfinished:
        ui.warn(f"{len(unfinished)} task(s) on this laptop still need a report. See: wattson tasks")
    if problem or (cfg is None and not url):
        if cfg is None and not url:
            ui.info("Run k8s/deploy.sh first: it writes the settings file and the token file.")
        return ERROR

    ui.heading("Agent")
    try:
        with agent(ui) as client:
            health = client.health()
    except (AgentError, ConfigError) as exc:
        ui.error(str(exc))
        return ERROR
    grim = health.grimoire
    ui.kv(
        [
            ("Agent", f"{'ok' if health.ok else 'NOT OK'} (version {health.version})"),
            (
                "Triage (small model)",
                f"{'ok' if health.triage.ok else 'NOT OK'}"
                + (f": {health.triage.detail}" if health.triage.detail else ""),
            ),
            (
                "Knowledge",
                f"{'ok' if health.knowledge.ok else 'NOT OK'}"
                + (f": {health.knowledge.detail}" if health.knowledge.detail else ""),
            ),
            (
                "Grimoire",
                "unknown"
                if grim is None
                else (
                    f"connected to {grim.mcp_url}"
                    if grim.connected
                    else "not connected: run wattson grimoire connect"
                ),
            ),
            ("History", ", ".join(f"{k} {v}" for k, v in health.history.items())),
        ]
        + (
            [
                (
                    "Injection model",
                    f"{'ok' if health.shield.ok else 'NOT OK'}"
                    + (f": {health.shield.detail}" if health.shield.detail else ""),
                )
            ]
            if health.shield is not None
            else []
        )
    )
    if health.shield is not None and not health.shield.ok:
        ui.warn("The injection model does not answer: the Guard checks the evidence with its rules only.")
    if grim is not None and grim.last_error:
        ui.warn(f"Grimoire: {grim.last_error}")
    return code if health.ok else ERROR


def run_k8s_list(ui: UI, args: argparse.Namespace) -> int:
    from wattson.connector import kube

    contexts, current = kube.list_contexts()
    readers = set(kube.reader_contexts())
    if not contexts:
        ui.info("No kube contexts found (is kubectl installed and a kubeconfig set?).")
        return OK
    ui.table(
        ["Context", "Current", "Read-only access", "Reader kubeconfig"],
        [
            (
                c,
                _yes(c == current),
                "ready" if c in readers else "missing: wattson k8s add " + c,
                str(kube.reader_kubeconfig_path(c)) if c in readers else "",
            )
            for c in contexts
        ],
        title="Kube contexts",
    )
    return OK


def reader_namespace(context: str, given: str | None) -> str:
    """Where the read-only account goes: --namespace, else the namespace of Wattson Agent when
    `context` is the cluster that hosts it (config.json). Wattson Agent never makes a namespace."""
    if given:
        return given
    try:
        cfg = load_config()
    except ConfigError:
        cfg = None
    if cfg is not None and cfg.k8s_context == context:
        return cfg.namespace
    raise UsageError(
        f"Which namespace should hold the read-only account on '{context}'? "
        f"Pass an existing one: wattson k8s add {context} --namespace <name>"
    )


def run_k8s_add(ui: UI, args: argparse.Namespace) -> int:
    from wattson.connector import kube

    namespace = reader_namespace(args.context, args.namespace)
    answered = {"yes": False}

    def confirm(question: str) -> bool:
        answered["yes"] = ui.confirm(question, False)
        return answered["yes"]

    try:
        kube.add_reader(args.context, confirm=confirm, log=ui.info, namespace=namespace)
    except kube.KubeError as exc:
        ui.error(str(exc))
        return ERROR
    if not answered["yes"]:
        return STOPPED
    ui.ok(f"Done. Wattson Agent can now read (only read) the cluster of {args.context}.")
    return OK
