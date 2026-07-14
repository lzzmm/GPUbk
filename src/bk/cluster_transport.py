from __future__ import annotations

import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from threading import Event
from typing import Optional, Sequence

from .models import BookingError


MAX_NODE_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_NODE_STDERR_BYTES = 64 * 1024
NODE_IO_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True)
class ClusterNode:
    name: str
    node_id: str
    transport: str
    target: Optional[str]
    executable: str
    priority: int
    timeout_seconds: float


@dataclass(frozen=True)
class NodeReply:
    node: ClusterNode
    payload: Optional[dict]
    error: Optional[str]
    timed_out: bool = False
    cancelled: bool = False


class _NodeOutputTooLarge(ValueError):
    pass


class _NodeRequestCancelled(RuntimeError):
    pass


def invoke_node(
    node: ClusterNode,
    argv: Sequence[str],
    *,
    cancel_event: Optional[Event] = None,
) -> NodeReply:
    command, environment = node_command(node, argv)
    try:
        returncode, stdout, stderr = _run_node_process(
            command,
            environment,
            node.timeout_seconds,
            cancel_event=cancel_event,
        )
    except subprocess.TimeoutExpired:
        return NodeReply(
            node,
            None,
            f"timed out after {node.timeout_seconds:g}s",
            timed_out=True,
        )
    except _NodeRequestCancelled:
        return NodeReply(node, None, "request cancelled", cancelled=True)
    except _NodeOutputTooLarge:
        return NodeReply(node, None, "response exceeds 8 MiB")
    except OSError as exc:
        return NodeReply(node, None, f"transport failed: {exc}")

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        if returncode not in {0, 3} and detail:
            return NodeReply(node, None, detail[-1])
        return NodeReply(node, None, "returned invalid JSON")
    if not isinstance(payload, dict):
        return NodeReply(node, None, "returned a non-object JSON response")
    identity = payload.get("node")
    if not isinstance(identity, dict) or identity.get("id") != node.node_id:
        return NodeReply(
            node,
            None,
            "stable node identity does not match the catalog",
        )
    if payload.get("kind") == "error":
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return NodeReply(node, None, str(message or "remote command failed"))
    if returncode not in {0, 3}:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        return NodeReply(
            node,
            None,
            detail[-1] if detail else f"exit {returncode}",
        )
    return NodeReply(node, payload, None)


def node_command(
    node: ClusterNode,
    argv: Sequence[str],
) -> tuple[list[str], Optional[dict]]:
    if node.transport == "local":
        environment = dict(os.environ)
        environment["BK_CLUSTER_DISABLE"] = "1"
        return [sys.executable, "-m", "bk", *argv], environment
    ssh = shutil.which("ssh")
    if ssh is None:
        raise BookingError("OpenSSH client is unavailable")
    remote = shlex.join([node.executable, *argv])
    command = [
        ssh,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "NumberOfPasswordPrompts=0",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "RequestTTY=no",
        "-o",
        f"ConnectTimeout={max(1, int(node.timeout_seconds))}",
        "-o",
        "ConnectionAttempts=1",
        "--",
        str(node.target),
        remote,
    ]
    return command, None


def _run_node_process(
    argv: Sequence[str],
    environment: Optional[dict],
    timeout_seconds: float,
    *,
    cancel_event: Optional[Event] = None,
) -> tuple[int, bytes, bytes]:
    process = subprocess.Popen(
        list(argv),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        close_fds=True,
    )
    if process.stdout is None or process.stderr is None:
        _kill_process_group(process)
        raise OSError("cluster subprocess pipes are unavailable")

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    deadline = time.monotonic() + timeout_seconds
    streams = (process.stdout, process.stderr)
    try:
        for stream, label in zip(streams, ("stdout", "stderr")):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, label)

        while selector.get_map():
            if cancel_event is not None and cancel_event.is_set():
                raise _NodeRequestCancelled
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
            for key, _events in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), NODE_IO_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    _close_selector_stream(selector, stream)
                elif key.data == "stdout":
                    keep = MAX_NODE_OUTPUT_BYTES + 1 - len(stdout)
                    if keep > 0:
                        stdout.extend(chunk[:keep])
                    if len(stdout) > MAX_NODE_OUTPUT_BYTES:
                        raise _NodeOutputTooLarge
                else:
                    _append_bounded_tail(stderr, chunk, MAX_NODE_STDERR_BYTES)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        returncode = process.wait(timeout=remaining)
        _kill_process_group(process)
        return returncode, bytes(stdout), bytes(stderr)
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        for stream in streams:
            _close_selector_stream(selector, stream)
        selector.close()


def _append_bounded_tail(buffer: bytearray, chunk: bytes, limit: int) -> None:
    if len(chunk) >= limit:
        buffer[:] = chunk[-limit:]
        return
    overflow = len(buffer) + len(chunk) - limit
    if overflow > 0:
        del buffer[:overflow]
    buffer.extend(chunk)


def _close_selector_stream(selector: selectors.BaseSelector, stream) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()
    except OSError:
        pass


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
