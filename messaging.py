"""Direct peer-to-peer messages and files, using a separate connection per operation."""

import ipaddress
import logging
import os
from pathlib import Path
import queue
import re
import socket
import threading

from file_transfer import (
    TRANSFER_TIMEOUT, parse_file_offer, receive_file_bytes,
    send_file_bytes, validate_file_path,
)
from protocol import receive_packet, send_packet


def validate_peer_information(peer: dict) -> None:
    if not isinstance(peer, dict):
        raise ValueError("Peer information must be an object.")
    if not isinstance(peer.get("username"), str) or not peer["username"].strip():
        raise ValueError("Peer username is missing.")
    if not isinstance(peer.get("ip"), str):
        raise ValueError("Peer IPv4 address is missing.")
    ipaddress.IPv4Address(peer["ip"])
    if type(peer.get("port")) is not int or not 1 <= peer["port"] <= 65535:
        raise ValueError("Peer listening port is invalid.")


class PeerMessenger:
    def __init__(self, username: str, events: queue.Queue, stopped: threading.Event) -> None:
        self.username = username
        self.events = events
        self.stopped = stopped
        self.identity: dict | None = None
        self.outgoing_messages = queue.Queue()
        self.connection_lock = threading.Lock()
        self.active_connections: set[socket.socket] = set()
        self.download_directory = Path.cwd() / "download"

        # Each process has its own log. FileHandler serializes writes from threads.
        safe_username = re.sub(r"[^\w.-]", "_", username)
        self.log_handler = logging.FileHandler(
            f"client-{safe_username}.log", encoding="utf-8", delay=True
        )
        self.log_handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger = logging.Logger(f"peer.{username}", level=logging.INFO)
        self.logger.addHandler(self.log_handler)

    def start(self, listening_socket: socket.socket) -> None:
        local_ip, local_port = listening_socket.getsockname()
        self.identity = {"username": self.username, "ip": local_ip, "port": local_port}
        listening_socket.settimeout(0.2)
        threading.Thread(
            target=self.accept_connections, args=(listening_socket,), daemon=True
        ).start()
        threading.Thread(target=self.send_queued_messages, daemon=True).start()

    def track_connection(self, connection_socket: socket.socket) -> bool:
        with self.connection_lock:
            if self.stopped.is_set():
                connection_socket.close()
                return False
            self.active_connections.add(connection_socket)
            return True

    def release_connection(self, connection_socket: socket.socket) -> None:
        with self.connection_lock:
            self.active_connections.discard(connection_socket)
        connection_socket.close()

    def accept_connections(self, listening_socket: socket.socket) -> None:
        while not self.stopped.is_set():
            try:
                connection_socket, _ = listening_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            connection_socket.settimeout(5)
            if not self.track_connection(connection_socket):
                break
            threading.Thread(
                target=self.receive_operation, args=(connection_socket,), daemon=True
            ).start()

    def receive_operation(self, connection_socket: socket.socket) -> None:
        try:
            packet = receive_packet(connection_socket)
            if packet is None:
                return
            sender = packet.get("sender")
            validate_peer_information(sender)
            if packet.get("type") == "message":
                content = packet.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Message content must be nonempty text.")
                if self.record_message("from", sender, content):
                    send_packet(connection_socket, {"type": "message_received"})
            elif packet.get("type") == "file_offer":
                offer = parse_file_offer(packet, sender)
                self.events.put(("file_offer", offer))
                destination = receive_file_bytes(
                    connection_socket, offer, self.download_directory, self.stopped
                )
                if destination is not None:
                    if self.record_file("from", sender, offer.filename, destination):
                        send_packet(connection_socket, {"type": "file_received"})
                elif not self.stopped.is_set():
                    self.events.put(("info", f"Rejected {offer.filename} from {sender['username']}."))
            else:
                raise ValueError("Expected a message or file_offer packet.")
        except (OSError, ValueError) as error:
            if not self.stopped.is_set():
                self.events.put(("info", f"Incoming transfer error: {error}"))
        finally:
            self.release_connection(connection_socket)

    def queue_message(self, recipient: dict, content: str) -> None:
        validate_peer_information(recipient)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Message content must be nonempty text.")
        if self.stopped.is_set() or self.identity is None:
            raise ConnectionError("Client is disconnected.")
        # Capture the target now, even if the GUI selection or userlist later changes.
        self.outgoing_messages.put((dict(recipient), content))

    def queue_file(self, recipient: dict, path_text: str) -> None:
        validate_peer_information(recipient)
        file_path = validate_file_path(path_text)
        if self.stopped.is_set() or self.identity is None:
            raise ConnectionError("Client is disconnected.")
        # A file upload never occupies the ordered message-sending worker.
        threading.Thread(
            target=self.send_file, args=(dict(recipient), file_path), daemon=True
        ).start()

    def send_file(self, recipient: dict, file_path: Path) -> None:
        try:
            with file_path.open("rb") as source:
                file_size = os.fstat(source.fileno()).st_size
                with socket.create_connection(
                    (recipient["ip"], recipient["port"]), timeout=5
                ) as connection_socket:
                    if not self.track_connection(connection_socket):
                        return
                    try:
                        send_packet(connection_socket, {
                            "type": "file_offer", "sender": self.identity,
                            "filename": file_path.name, "size": file_size,
                        })
                        # Human confirmation has no five-second network deadline.
                        # Disconnect still interrupts this recv through shutdown().
                        connection_socket.settimeout(None)
                        decision = receive_packet(connection_socket)
                        if (
                            decision is None or decision.get("type") != "file_decision"
                            or type(decision.get("accepted")) is not bool
                        ):
                            raise ConnectionError("No valid file acceptance response.")
                        if not decision["accepted"]:
                            if not self.stopped.is_set():
                                self.events.put(("info", (
                                    f"{recipient['username']} rejected {file_path.name}."
                                )))
                            return

                        connection_socket.settimeout(TRANSFER_TIMEOUT)
                        self.events.put(("info", (
                            f"Sending {file_path.name} to {recipient['username']}…"
                        )))
                        send_file_bytes(connection_socket, source, file_size, self.stopped)
                        confirmation = receive_packet(connection_socket)
                        if confirmation != {"type": "file_received"}:
                            raise ConnectionError("No completed-file confirmation from the peer.")
                        self.record_file("to", recipient, file_path.name)
                    finally:
                        self.release_connection(connection_socket)
        except (OSError, ValueError) as error:
            if not self.stopped.is_set():
                self.events.put(("info", (
                    f"File transfer to {recipient['username']} could not be confirmed: {error}"
                )))

    def send_queued_messages(self) -> None:
        # One worker preserves command order while the GUI and receiver stay active.
        while not self.stopped.is_set():
            try:
                recipient, content = self.outgoing_messages.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                with socket.create_connection(
                    (recipient["ip"], recipient["port"]), timeout=5
                ) as connection_socket:
                    if not self.track_connection(connection_socket):
                        return
                    try:
                        send_packet(connection_socket, {
                            "type": "message",
                            "sender": self.identity,
                            "content": content,
                        })
                        confirmation = receive_packet(connection_socket)
                        if confirmation != {"type": "message_received"}:
                            raise ConnectionError("No receipt confirmation from the peer.")
                        self.record_message("to", recipient, content)
                    finally:
                        self.release_connection(connection_socket)
            except (OSError, ValueError) as error:
                if not self.stopped.is_set():
                    self.events.put(("info", (
                        f"Delivery to {recipient['username']} could not be confirmed: {error}"
                    )))
            finally:
                self.outgoing_messages.task_done()

    def record_message(self, direction: str, peer: dict, content: str) -> bool:
        with self.connection_lock:
            if self.stopped.is_set():
                return False
            self.logger.info(
                "Message %s %s@%s:%s “%s”",
                direction, peer["username"], peer["ip"], peer["port"], content,
            )
            self.events.put(("message", {
                "direction": direction, "peer": dict(peer), "content": content,
            }))
            return True

    def record_file(
        self, direction: str, peer: dict, filename: str, destination: Path | None = None
    ) -> bool:
        with self.connection_lock:
            if self.stopped.is_set():
                return False
            self.logger.info(
                "%s %s %s@%s:%s", filename, direction,
                peer["username"], peer["ip"], peer["port"],
            )
            self.events.put(("file", {
                "direction": direction, "peer": dict(peer), "filename": filename,
                "destination": str(destination) if destination is not None else None,
            }))
            return True

    def close(self) -> None:
        self.stopped.set()
        with self.connection_lock:
            connections = list(self.active_connections)
            self.active_connections.clear()
            self.log_handler.close()
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
