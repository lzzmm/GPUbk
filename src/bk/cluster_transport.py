from __future__ import annotations

import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from threading import Event
from typing import Optional, Sequence

from .models import BookingError


MAX_NODE_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_NODE_STDERR_BYTES = 64 * 1024
NODE_IO_CHUNK_BYTES = 64 * 1024
MAX_NODE_ERROR_CHARS = 1000
_STABLE_NODE_ID = re.compile(r"^[0-9a-f]{20}$")


@dataclass(frozen=True)
class ClusterNode:
    name: str
    node_id: str
    transport: str
    target: Optional[str]
    executable: str
    priority: int
    timeout_seconds: float
    enabled: bool = True


@dataclass(frozen=True)
class NodeReply:
    node: ClusterNode
    payload: Optional[dict]
    error: Optional[str]
    timed_out: bool = False
    cancelled: bool = False
    error_code: Optional[str] = None


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
    return _invoke_node(
        node,
        argv,
        cancel_event=cancel_event,
        expected_node_id=node.node_id,
    )


def probe_ssh_node(
    node: ClusterNode,
    argv: Sequence[str],
    *,
    cancel_event: Optional[Event] = None,
) -> NodeReply:
    """Query one SSH endpoint before its stable node ID is cataloged."""

    if node.transport != "ssh":
        raise BookingError("node discovery is available only for SSH endpoints")
    return _invoke_node(
        node,
        argv,
        cancel_event=cancel_event,
        expected_node_id=None,
    )


def _invoke_node(
    node: ClusterNode,
    argv: Sequence[str],
    *,
    cancel_event: Optional[Event],
    expected_node_id: Optional[str],
) -> NodeReply:
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_reply(node)
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
            error_code="timeout",
        )
    except _NodeRequestCancelled:
        return _cancelled_reply(node)
    except _NodeOutputTooLarge:
        return NodeReply(
            node,
            None,
            "response exceeds 8 MiB",
            error_code="protocol",
        )
    except OSError as exc:
        return NodeReply(
            node,
            None,
            _node_error_text(f"transport failed: {exc}"),
            error_code="transport",
        )

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        if returncode not in {0, 3} and detail:
            return NodeReply(
                node,
                None,
                _node_error_text(detail[-1]),
                error_code="transport",
            )
        return NodeReply(
            node,
            None,
            "returned invalid JSON",
            error_code="protocol",
        )
    if not isinstance(payload, dict):
        return NodeReply(
            node,
            None,
            "returned a non-object JSON response",
            error_code="protocol",
        )
    identity = payload.get("node")
    remote_node_id = identity.get("id") if isinstance(identity, dict) else None
    if not isinstance(remote_node_id, str) or not _STABLE_NODE_ID.fullmatch(
        remote_node_id
    ):
        return NodeReply(
            node,
            None,
            "returned an invalid stable node identity",
            error_code="identity",
        )
    if expected_node_id is not None and remote_node_id != expected_node_id:
        return NodeReply(
            node,
            None,
            "stable node identity does not match the catalog",
            error_code="identity",
        )
    reply_node = (
        node
        if expected_node_id is not None
        else replace(node, node_id=remote_node_id)
    )
    if payload.get("kind") == "error":
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        return NodeReply(
            reply_node,
            None,
            _node_error_text(message or "remote command failed"),
            error_code="remote",
        )
    if returncode not in {0, 3}:
        detail = stderr.decode("utf-8", "replace").strip().splitlines()
        return NodeReply(
            reply_node,
            None,
            _node_error_text(detail[-1] if detail else f"exit {returncode}"),
            error_code="transport",
        )
    return NodeReply(reply_node, payload, None)


def _cancelled_reply(node: ClusterNode) -> NodeReply:
    return NodeReply(
        node,
        None,
        "request cancelled",
        cancelled=True,
        error_code="cancelled",
    )


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

        returncode = _wait_for_node_process(
            process,
            argv,
            deadline,
            timeout_seconds,
            cancel_event=cancel_event,
        )
        _kill_process_group(process)
        return returncode, bytes(stdout), bytes(stderr)
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        for stream in streams:
            _close_selector_stream(selector, stream)
        selector.close()


def _wait_for_node_process(
    process: subprocess.Popen,
    argv: Sequence[str],
    deadline: float,
    timeout_seconds: float,
    *,
    cancel_event: Optional[Event],
) -> int:
    """Wait without losing cancellation after a child closes its pipes early."""

    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise _NodeRequestCancelled
        returncode = process.poll()
        if returncode is not None:
            return returncode
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        time.sleep(min(remaining, 0.05))


def _append_bounded_tail(buffer: bytearray, chunk: bytes, limit: int) -> None:
    if len(chunk) >= limit:
        buffer[:] = chunk[-limit:]
        return
    overflow = len(buffer) + len(chunk) - limit
    if overflow > 0:
        del buffer[:overflow]
    buffer.extend(chunk)


def _node_error_text(value: object) -> str:
    printable = "".join(
        character if character.isprintable() else " " for character in str(value)
    )
    collapsed = " ".join(printable.split()) or "remote command failed"
    if len(collapsed) <= MAX_NODE_ERROR_CHARS:
        return collapsed
    return collapsed[: MAX_NODE_ERROR_CHARS - 1] + "~"


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
