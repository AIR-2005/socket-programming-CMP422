"""Stream file bytes after the receiving user accepts a file offer."""

from dataclasses import dataclass, field
from pathlib import Path
import queue
import socket
import threading
from typing import BinaryIO, Callable

from protocol import send_packet


CHUNK_SIZE = 64 * 1024
TRANSFER_TIMEOUT = 30


@dataclass
class FileOffer:
    sender: dict
    filename: str
    size: int
    decision: queue.Queue = field(default_factory=queue.Queue)


def validate_file_path(path_text: str) -> Path:
    file_path = Path(path_text).expanduser()
    if not file_path.is_absolute():
        raise ValueError("sndf requires a full file path.")
    if not file_path.is_file():
        raise ValueError(f"File not found: {file_path}")
    return file_path


def parse_file_offer(packet: dict, sender: dict) -> FileOffer:
    filename = packet.get("filename")
    file_size = packet.get("size")
    if (
        not isinstance(filename, str) or not filename or filename in (".", "..")
        or "/" in filename or "\\" in filename or "\0" in filename
    ):
        raise ValueError("The offered filename must contain a filename only.")
    if type(file_size) is not int or file_size < 0:
        raise ValueError("File size must be a nonnegative integer.")
    return FileOffer(dict(sender), filename, file_size)


def send_file_bytes(
    connection_socket: socket.socket, source: BinaryIO, file_size: int,
    stopped: threading.Event,
) -> None:
    remaining = file_size
    while remaining:
        if stopped.is_set():
            raise ConnectionError("File transfer was cancelled.")
        chunk = source.read(min(CHUNK_SIZE, remaining))
        if not chunk:
            raise ConnectionError("The source file became shorter during transfer.")
        connection_socket.sendall(chunk)
        remaining -= len(chunk)


def create_download(download_directory: Path, filename: str) -> tuple[Path, BinaryIO]:
    """Reserve a new file without overwriting an existing download."""
    download_directory.mkdir(parents=True, exist_ok=True)
    original_name = Path(filename)
    candidate = download_directory / filename
    copy_number = 0
    while True:
        try:
            return candidate, candidate.open("xb")
        except FileExistsError:
            copy_number += 1
            candidate = download_directory / (
                f"{original_name.stem} ({copy_number}){original_name.suffix}"
            )


def receive_file_bytes(
    connection_socket: socket.socket, offer: FileOffer, download_directory: Path,
    stopped: threading.Event,
    on_decision: Callable[[bool], None] | None = None,
) -> Path | None:
    # The network worker waits here; the GUI decides through offer.decision.
    while not stopped.is_set():
        try:
            accepted = offer.decision.get(timeout=0.2)
            break
        except queue.Empty:
            continue
    else:
        raise ConnectionError("File transfer was cancelled before acceptance.")

    if on_decision is not None:
        on_decision(accepted)
    if not accepted:
        send_packet(connection_socket, {"type": "file_decision", "accepted": False})
        return None

    destination, output_file = create_download(download_directory, offer.filename)
    completed = False
    try:
        with output_file:
            connection_socket.settimeout(TRANSFER_TIMEOUT)
            send_packet(connection_socket, {"type": "file_decision", "accepted": True})
            remaining = offer.size
            while remaining:
                if stopped.is_set():
                    raise ConnectionError("File transfer was cancelled.")
                chunk = connection_socket.recv(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ConnectionError("Connection closed before the file was complete.")
                output_file.write(chunk)
                remaining -= len(chunk)
        completed = True
        return destination
    finally:
        if not completed:
            destination.unlink(missing_ok=True)
