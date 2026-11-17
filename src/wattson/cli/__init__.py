"""The wattson command line: an A2A client of the orchestrator that answers its questions
(clarify, collect evidence, approve, run Claude Code) on the laptop."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wattson")
except PackageNotFoundError:  # running from a source tree without the package installed
    __version__ = "0.0.0"
