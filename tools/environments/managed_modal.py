"""Managed Modal environment backed by tool-gateway.

Uses ``BaseEnvironment`` for command shaping (``_wrap_command()``) but keeps
its own ``execute()`` override because the HTTP gateway cannot return a
ProcessHandle — all execution is request/response.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import requests
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from tools.environments.base import BaseEnvironment
from tools.interrupt import is_interrupted
from tools.managed_tool_gateway import resolve_managed_tool_gateway

logger = logging.getLogger(__name__)


def _request_timeout_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class _ManagedModalExecHandle:
    exec_id: str


@dataclass(frozen=True)
class _ExecStartResult:
    """Discriminated return from _start_exec — either immediate or async."""
    handle: _ManagedModalExecHandle | None = None
    immediate: dict | None = None


class ManagedModalEnvironment(BaseEnvironment):
    """Gateway-owned Modal sandbox with Hermes-compatible execute/cleanup.

    Inherits from BaseEnvironment for _wrap_command() (CWD tracking,
    snapshot sourcing) but keeps its own execute() since the HTTP gateway
    cannot return a ProcessHandle.

    **Design note — no init_session():**
    Unlike direct backends (local, docker, ssh, modal, daytona, singularity),
    ManagedModal does not call init_session() because _run_bash() raises
    NotImplementedError (the gateway is HTTP request/response only).
    Commands run without login-shell snapshot sourcing — the gateway itself
    manages sandbox environment setup.  _wrap_command() is still used for
    CWD tracking via in-band stdout markers.
    """

    _CONNECT_TIMEOUT_SECONDS = _request_timeout_env("TERMINAL_MANAGED_MODAL_CONNECT_TIMEOUT_SECONDS", 1.0)
    _POLL_READ_TIMEOUT_SECONDS = _request_timeout_env("TERMINAL_MANAGED_MODAL_POLL_READ_TIMEOUT_SECONDS", 5.0)
    _CANCEL_READ_TIMEOUT_SECONDS = _request_timeout_env("TERMINAL_MANAGED_MODAL_CANCEL_READ_TIMEOUT_SECONDS", 5.0)
    _client_timeout_grace_seconds = 10.0

    def __init__(
        self,
        image: str,
        cwd: str = "/root",
        timeout: int = 60,
        modal_sandbox_kwargs: Optional[Dict[str, Any]] = None,
        persistent_filesystem: bool = True,
        task_id: str = "default",
    ):
        super().__init__(cwd=cwd, timeout=timeout)

        self._guard_unsupported_credential_passthrough()

        gateway = resolve_managed_tool_gateway("modal")
        if gateway is None:
            raise ValueError("Managed Modal requires a configured tool gateway and Nous user token")

        self._gateway_origin = gateway.gateway_origin.rstrip("/")
        self._nous_user_token = gateway.nous_user_token
        self._task_id = task_id
        self._persistent = persistent_filesystem
        self._image = image
        self._sandbox_kwargs = dict(modal_sandbox_kwargs or {})
        self._create_idempotency_key = str(uuid.uuid4())
        self._sandbox_id = self._create_sandbox()

    # ------------------------------------------------------------------
    # _run_bash stub — ManagedModal cannot return a ProcessHandle
    # ------------------------------------------------------------------

    def _run_bash(self, cmd_string: str, *, timeout: int | None = None,
                  stdin_data: str | None = None):
        """ManagedModal is HTTP-based and cannot return a ProcessHandle.

        This stub satisfies the BaseEnvironment interface.  init_session()
        is intentionally not called — the gateway manages the sandbox
        environment directly.  execute() is overridden with HTTP
        request/response logic.
        """
        raise NotImplementedError(
            "ManagedModalEnvironment is HTTP-based and cannot return a "
            "ProcessHandle. Use execute() directly."
        )

    # ------------------------------------------------------------------
    # execute() override — HTTP request/response model
    # ------------------------------------------------------------------

    def execute(
        self,
        command: str,
        cwd: str = "",
        *,
        timeout: int | None = None,
        stdin_data: str | None = None,
    ) -> dict:
        self._before_execute()
        effective_timeout = timeout or self.timeout

        # Handle stdin via heredoc embedding (gateway has payload support too)
        exec_stdin = stdin_data
        if stdin_data is not None:
            command = self._embed_stdin_heredoc(command, stdin_data)
            exec_stdin = None  # embedded in command now

        exec_command, sudo_stdin = self._prepare_command(command)
        if sudo_stdin is not None:
            exec_command = (
                f"printf '%s\\n' {shlex.quote(sudo_stdin.rstrip())} | {exec_command}"
            )

        # Use _wrap_command for consistent CWD tracking and snapshot sourcing.
        # The wrapped script handles cd internally, so no separate cwd needed.
        wrapped = self._wrap_command(exec_command, cwd)

        # Start the exec via the gateway
        start = self._start_exec(wrapped, effective_timeout, exec_stdin)

        if start.immediate is not None:
            self._update_cwd_from_gateway_output(start.immediate)
            return start.immediate

        if start.handle is None:
            return self._error_result(
                "Managed Modal exec start did not return an exec handle"
            )

        # Poll loop
        deadline = None
        if self._client_timeout_grace_seconds is not None:
            deadline = time.monotonic() + effective_timeout + self._client_timeout_grace_seconds

        while True:
            if is_interrupted():
                try:
                    self._cancel_exec(start.handle.exec_id)
                except Exception:
                    pass
                return self._result(
                    "[Command interrupted - Modal sandbox exec cancelled]", 130,
                )

            try:
                result = self._poll_exec(start.handle)
            except Exception as exc:
                return self._error_result(f"Managed Modal exec poll failed: {exc}")

            if result is not None:
                self._update_cwd_from_gateway_output(result)
                return result

            if deadline is not None and time.monotonic() >= deadline:
                try:
                    self._cancel_exec(start.handle.exec_id)
                except Exception:
                    pass
                return self._result(
                    f"Managed Modal exec timed out after {effective_timeout}s", 124,
                )

            time.sleep(0.25)

    def _update_cwd_from_gateway_output(self, result: dict) -> None:
        """Extract CWD from in-band marker in command output."""
        self._extract_cwd_from_output(result)

    # ------------------------------------------------------------------
    # Gateway transport
    # ------------------------------------------------------------------

    def _start_exec(self, command: str, timeout: int,
                    stdin_data: str | None) -> _ExecStartResult:
        exec_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "execId": exec_id,
            "command": command,
            "timeoutMs": int(timeout * 1000),
        }
        if stdin_data is not None:
            payload["stdinData"] = stdin_data

        try:
            response = self._request(
                "POST",
                f"/v1/sandboxes/{self._sandbox_id}/execs",
                json=payload,
                timeout=10,
            )
        except Exception as exc:
            return _ExecStartResult(immediate=self._error_result(f"Managed Modal exec failed: {exc}"))

        if response.status_code >= 400:
            return _ExecStartResult(immediate=self._error_result(
                self._format_error("Managed Modal exec failed", response)
            ))

        body = response.json()
        status = body.get("status")
        if status in {"completed", "failed", "cancelled", "timeout"}:
            return _ExecStartResult(immediate=self._result(body.get("output", ""), body.get("returncode", 1)))

        if body.get("execId") != exec_id:
            return _ExecStartResult(immediate=self._error_result(
                "Managed Modal exec start did not return the expected exec id"
            ))

        return _ExecStartResult(handle=_ManagedModalExecHandle(exec_id=exec_id))

    def _poll_exec(self, handle: _ManagedModalExecHandle) -> dict | None:
        try:
            status_response = self._request(
                "GET",
                f"/v1/sandboxes/{self._sandbox_id}/execs/{handle.exec_id}",
                timeout=(self._CONNECT_TIMEOUT_SECONDS, self._POLL_READ_TIMEOUT_SECONDS),
            )
        except Exception as exc:
            return self._error_result(f"Managed Modal exec poll failed: {exc}")

        if status_response.status_code == 404:
            return self._error_result("Managed Modal exec not found")

        if status_response.status_code >= 400:
            return self._error_result(
                self._format_error("Managed Modal exec poll failed", status_response)
            )

        status_body = status_response.json()
        status = status_body.get("status")
        if status in {"completed", "failed", "cancelled", "timeout"}:
            return self._result(
                status_body.get("output", ""),
                status_body.get("returncode", 1),
            )
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cleanup(self):
        if not getattr(self, "_sandbox_id", None):
            return

        try:
            self._request(
                "POST",
                f"/v1/sandboxes/{self._sandbox_id}/terminate",
                json={
                    "snapshotBeforeTerminate": self._persistent,
                },
                timeout=60,
            )
        except Exception as exc:
            logger.warning("Managed Modal cleanup failed: %s", exc)
        finally:
            self._sandbox_id = None

    # ------------------------------------------------------------------
    # Sandbox creation
    # ------------------------------------------------------------------

    def _create_sandbox(self) -> str:
        cpu = self._coerce_number(self._sandbox_kwargs.get("cpu"), 1)
        memory = self._coerce_number(
            self._sandbox_kwargs.get("memoryMiB", self._sandbox_kwargs.get("memory")),
            5120,
        )
        disk = self._coerce_number(
            self._sandbox_kwargs.get("ephemeral_disk", self._sandbox_kwargs.get("diskMiB")),
            None,
        )

        create_payload = {
            "image": self._image,
            "cwd": self.cwd,
            "cpu": cpu,
            "memoryMiB": memory,
            "timeoutMs": 3_600_000,
            "idleTimeoutMs": max(300_000, int(self.timeout * 1000)),
            "persistentFilesystem": self._persistent,
            "logicalKey": self._task_id,
        }
        if disk is not None:
            create_payload["diskMiB"] = disk

        response = self._request(
            "POST",
            "/v1/sandboxes",
            json=create_payload,
            timeout=60,
            extra_headers={
                "x-idempotency-key": self._create_idempotency_key,
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(self._format_error("Managed Modal create failed", response))

        body = response.json()
        sandbox_id = body.get("id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise RuntimeError("Managed Modal create did not return a sandbox id")
        return sandbox_id

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _guard_unsupported_credential_passthrough(self) -> None:
        """Managed Modal does not sync or mount host credential files."""
        try:
            from tools.credential_files import get_credential_file_mounts
        except Exception:
            return

        mounts = get_credential_file_mounts()
        if mounts:
            raise ValueError(
                "Managed Modal does not support host credential-file passthrough. "
                "Use TERMINAL_MODAL_MODE=direct when skills or config require "
                "credential files inside the sandbox."
            )

    def _result(self, output: str, returncode: int) -> dict:
        return {"output": output, "returncode": returncode}

    def _error_result(self, output: str) -> dict:
        return self._result(output, 1)

    def _request(self, method: str, path: str, *,
                 json: Dict[str, Any] | None = None,
                 timeout: int = 30,
                 extra_headers: Dict[str, str] | None = None) -> requests.Response:
        headers = {
            "Authorization": f"Bearer {self._nous_user_token}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        return requests.request(
            method,
            f"{self._gateway_origin}{path}",
            headers=headers,
            json=json,
            timeout=timeout,
        )

    def _cancel_exec(self, exec_id: str) -> None:
        try:
            self._request(
                "POST",
                f"/v1/sandboxes/{self._sandbox_id}/execs/{exec_id}/cancel",
                timeout=(self._CONNECT_TIMEOUT_SECONDS, self._CANCEL_READ_TIMEOUT_SECONDS),
            )
        except Exception as exc:
            logger.warning("Managed Modal exec cancel failed: %s", exc)

    @staticmethod
    def _coerce_number(value: Any, default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_error(prefix: str, response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                message = payload.get("error") or payload.get("message") or payload.get("code")
                if isinstance(message, str) and message:
                    return f"{prefix}: {message}"
                return f"{prefix}: {json.dumps(payload, ensure_ascii=False)}"
        except Exception:
            pass

        text = response.text.strip()
        if text:
            return f"{prefix}: {text}"
        return f"{prefix}: HTTP {response.status_code}"
