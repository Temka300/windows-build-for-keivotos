"""Sanitized evidence for existing subprocess owners; never changes tool policy."""
from collections import deque
from functools import lru_cache
import importlib.metadata
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

from services.diagnostic_events import redact_log_text
from services.operation_diagnostics import emit
from services.process_launch import quiet_options


@lru_cache(maxsize=32)
def _version(executable, modified, size):
    if Path(executable).resolve() == Path(sys.executable).resolve():
        return sys.version.split()[0]
    if "ffmpeg" in Path(executable).name.lower():
        try:
            result = subprocess.run([executable, "-version"], capture_output=True, timeout=2, **quiet_options())
            return redact_log_text(result.stdout.decode("utf-8", "replace").splitlines()[0])[:256]
        except (OSError, subprocess.SubprocessError, IndexError, AttributeError):
            return "unavailable"
    return "unavailable"


def command_details(command, secrets=(), *, packages=()):
    arguments = []
    hide = False
    for raw in list(command)[1:]:
        value = str(raw)
        if hide:
            arguments.append("[redacted]")
            hide = False
        elif value in ("-c", "--exec", "--cookies", "--cookies-from-browser", "--password", "--username", "--add-header"):
            arguments.append(value)
            hide = True
        else:
            arguments.append(redact_log_text(value, secrets)[:1024])
    executable = str(command[0])
    try:
        facts = Path(executable).stat()
        version = _version(executable, facts.st_mtime_ns, facts.st_size)
    except OSError:
        version = "unavailable"
    versions = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    return dict(executable=redact_log_text(executable, secrets), tool_version=version,
                arguments=arguments[:64], package_versions=versions)


class ToolEvidence:
    """Bounded sanitized head/tail plus counts, independent of the worker protocol."""
    def __init__(self, owner, command, *, destination=None, secrets=(), packages=()):
        self.owner, self.secrets = owner, secrets
        self.started = time.monotonic()
        self.head, self.tail = [], deque(maxlen=8)
        self.lines = 0
        self.truncated_lines = 0
        self.bytes = 0
        self.closed = False
        try:
            details = command_details(command, secrets, packages=packages)
        except Exception:
            details = {"tool_version":"unavailable","arguments":[]}
        if destination is not None:
            details["destination_reference"] = str(destination)[:1024]
        emit(owner, "tool.started", "External tool started", details=details, secrets=secrets)

    def output(self, value, stream="stderr", *, binary=False, protocol=False):
        if not value:
            return
        if isinstance(value, bytes):
            self.bytes += len(value)
            if binary:
                return  # PNG/media stdout is never decoded or retained.
            value = value.decode("utf-8", "replace")
        else:
            self.bytes += len(value.encode("utf-8", "replace"))
        if protocol:
            # Workers can send entire metadata captures. Retain only safe control facts.
            try:
                event = json.loads(value) if isinstance(value, str) else value
                value = json.dumps({key: event[key] for key in ("event", "phase", "progress")
                                    if key in event and isinstance(event[key], (str, int, float))})
            except (ValueError, TypeError):
                value = "[invalid worker protocol]"
        for physical_line in str(value).splitlines():
            safe = redact_log_text(physical_line, self.secrets).rstrip()
            if not safe:
                continue
            if len(safe)>1024:
                self.truncated_lines += 1
                safe=safe[:1024]+"…[truncated]"
            row = {"stream": stream, "line": safe}
            self.lines += 1
            if len(self.head) < 8:
                self.head.append(row)
            else:
                self.tail.append(row)

    def finish(self, exit_code=None, *, outcome=None, reason=None, validation=None):
        if self.closed:
            return
        self.closed = True
        outcome = outcome or ("completed" if exit_code == 0 else "failed")
        details = dict(exit_code=exit_code, outcome=outcome,
                       duration_ms=round(max(0, time.monotonic()-self.started)*1000,3),
                       output_head=self.head, output_tail=list(self.tail),
                       output_lines=self.lines, output_bytes=self.bytes, truncated_lines=self.truncated_lines,
                       omitted_lines=max(0,self.lines-len(self.head)-len(self.tail)))
        if reason:
            details["reason"] = reason
        if validation is not None:
            details["output_validated"] = validation
        emit(self.owner, "tool.finished", "External tool: " + outcome,
             level=logging.ERROR if outcome=="failed" else logging.WARNING if outcome in ("interrupted","completed-with-warnings") else logging.INFO,
             details=details, secrets=self.secrets)


def run(owner, command, *, binary_stdout=False, packages=(), **kwargs):
    evidence = ToolEvidence(owner, command, destination=kwargs.get("cwd"), packages=packages)
    try:
        result = subprocess.run(command, **quiet_options(kwargs))
    except subprocess.CalledProcessError as error:
        evidence.output(error.stdout, "stdout", binary=binary_stdout)
        evidence.output(error.stderr, "stderr")
        evidence.finish(error.returncode)
        raise
    except BaseException as error:
        evidence.finish(outcome="cancelled" if isinstance(error, KeyboardInterrupt) else "failed",
                        reason="timeout" if isinstance(error, subprocess.TimeoutExpired) else type(error).__name__)
        raise
    evidence.output(result.stdout, "stdout", binary=binary_stdout)
    evidence.output(result.stderr, "stderr")
    evidence.finish(result.returncode)
    return result
