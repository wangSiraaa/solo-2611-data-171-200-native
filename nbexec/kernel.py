"""Kernel lifecycle management with bounded shutdown and process reaping."""

from __future__ import annotations

import os
import queue
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from jupyter_client.manager import KernelManager


@dataclass
class KernelShutdownReport:
    requested: str = ""
    soft_wait_seconds: Optional[float] = None
    hard_wait_seconds: Optional[float] = None
    waited_seconds: Optional[float] = None
    reaped: bool = False
    pid: Optional[int] = None
    note: str = ""


class ManagedKernel:
    """Own one fresh python3 kernel and guarantee its process is reclaimed."""

    def __init__(self, cwd: str, startup_timeout: float = 30.0) -> None:
        self.cwd = cwd
        self.startup_timeout = startup_timeout
        self.manager = KernelManager(kernel_name="python3")
        self.client = None
        self.pid: Optional[int] = None
        self.shutdown_report: KernelShutdownReport = KernelShutdownReport()
        self._shutdown_done = False

    def start(self) -> Dict[str, Any]:
        try:
            self.manager.start_kernel(cwd=self.cwd)
        except Exception:
            # Make sure any partially started process/connection file is cleaned up.
            self._shutdown_done = True
            self.shutdown_report.requested = "startup-failure-cleanup"
            self._cleanup_connection()
            raise
        self.client = self.manager.client()
        self.client.start_channels()
        try:
            self.client.wait_for_ready(timeout=self.startup_timeout)
        except Exception:
            self.shutdown(soft_wait=5.0, hard_wait=5.0)
            raise
        self.pid = self.manager.provisioner.pid
        info = self._kernel_info_reply()
        lang = info.get("language_info", {})
        return {
            "kernel_name": "python3",
            "pid": self.pid,
            "language": lang.get("name"),
            "language_version": lang.get("version"),
            "implementation": lang.get("implementation", "ipython"),
            "banner": info.get("banner", ""),
        }

    def _kernel_info_reply(self) -> Dict[str, Any]:
        """Send kernel_info and block until the matching shell reply arrives."""
        assert self.client is not None
        msg_id = self.client.kernel_info()
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            try:
                reply = self.client.get_shell_msg(timeout=0.5)
            except queue.Empty:
                continue
            if reply["parent_header"].get("msg_id") == msg_id:
                return reply["content"]
        raise TimeoutError("timed out waiting for kernel_info_reply")

    def interrupt(self) -> None:
        self.manager.interrupt_kernel()

    def _is_process_gone(self) -> bool:
        if self.pid is None:
            return True
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Process exists but is not ours to signal; it is still alive.
            return False
        return False

    def shutdown(self, soft_wait: float = 10.0, hard_wait: float = 10.0) -> KernelShutdownReport:
        """Polite shutdown first, then SIGKILL; always wait for the process to exit.

        Safe to call more than once (the timeout path force-kills the kernel and
        the runner's ``finally`` block calls shutdown again).
        """
        if self._shutdown_done:
            return self.shutdown_report
        self._shutdown_done = True
        report = self.shutdown_report
        report.pid = self.pid
        started = time.monotonic()
        if self.client is not None:
            try:
                self.client.stop_channels()
            except Exception as exc:  # pragma: no cover - defensive
                report.note = f"stop_channels error: {exc!r}"

        if self._is_process_gone():
            report.requested = "already-exited"
            report.reaped = True
            report.waited_seconds = time.monotonic() - started
            self._cleanup_connection()
            return report

        try:
            report.requested = "shutdown_request"
            report.soft_wait_seconds = soft_wait
            self.manager.shutdown_kernel(now=False)
        except Exception as exc:
            report.note = (report.note + f" soft shutdown failed: {exc!r}").strip()

        deadline = started + soft_wait
        while time.monotonic() < deadline:
            if self._is_process_gone():
                report.reaped = True
                report.waited_seconds = time.monotonic() - started
                self._cleanup_connection()
                return report
            time.sleep(0.1)

        # Soft shutdown did not finish in time: terminate/kill the process directly.
        if not self._is_process_gone():
            report.requested = "forced-kill"
            report.hard_wait_seconds = hard_wait
            try:
                self.manager.shutdown_kernel(now=True)
            except Exception as exc:
                report.note = (report.note + f" hard shutdown failed: {exc!r}").strip()
            kill_deadline = time.monotonic() + hard_wait
            while time.monotonic() < kill_deadline:
                if self._is_process_gone():
                    break
                time.sleep(0.1)

        report.reaped = self._is_process_gone()
        report.waited_seconds = time.monotonic() - started
        self._cleanup_connection()
        return report

    def _cleanup_connection(self) -> None:
        try:
            self.manager.cleanup(connection_file=True)
        except Exception:
            pass
