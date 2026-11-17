"""The laptop connector: the controlled local execution of wattson (FR-CLI-05). Git context and
evidence (`git`), read-only kubectl evidence and the reader kubeconfig (`kube`), the allow-list and
collection of evidence (`evidence`), and Claude Code sessions (`claude`). No model runs here.
Every command goes through `run.run`: an argument list, never a shell, with a timeout."""
