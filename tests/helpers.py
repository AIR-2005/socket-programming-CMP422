"""Isolated local servers and event queues for network integration tests."""

from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

from client import DirectoryConnection


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ServerProcess:
    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="socket-server-test-")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(PROJECT_ROOT / "server"), str(self.port)],
            cwd=self.directory.name, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while True:
            try:
                with self.connect(timeout=0.2):
                    break
            except OSError:
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self.close()
                    raise RuntimeError("The test server did not start.")
                time.sleep(0.02)

    def connect(self, timeout: float = 3) -> socket.socket:
        return socket.create_connection(("127.0.0.1", self.port), timeout=timeout)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        try:
            _, diagnostics = self.process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            raise AssertionError("The test server did not stop.")
        finally:
            self.directory.cleanup()
        if self.process.returncode != 0 or diagnostics:
            raise AssertionError(diagnostics.decode() or "The server exited unsuccessfully.")


class PeerSession:
    def __init__(self, username: str, port: int, working_directory: Path) -> None:
        self.connection = DirectoryConnection(username, queue.Queue(), working_directory)
        self.pending = []
        self.worker = threading.Thread(
            target=self.connection.connect_and_receive, args=("127.0.0.1", port)
        )
        self.worker.start()

    def wait_for(self, event_type: str, predicate=lambda data: True, timeout: float = 4):
        for index, (kind, data) in enumerate(self.pending):
            if kind == event_type and predicate(data):
                self.pending.pop(index)
                return data
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            kind, data = self.connection.events.get(
                timeout=max(0.01, deadline - time.monotonic())
            )
            if kind == event_type and predicate(data):
                return data
            self.pending.append((kind, data))
        raise AssertionError(f"Missing event: {event_type}")

    def close(self) -> None:
        self.connection.close()
        self.worker.join(timeout=5)
        if self.worker.is_alive():
            raise AssertionError("A client directory worker did not stop.")
