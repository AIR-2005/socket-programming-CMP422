"""Central peer directory: register peers and broadcast the online userlist."""

import argparse
from dataclasses import dataclass, field
from datetime import datetime
import ipaddress
import logging
import queue
import socket
import sys
import threading

from protocol import receive_packet, send_packet


@dataclass
class Peer:
    username: str
    ip: str
    port: int
    connection_socket: socket.socket
    pending_userlists: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=1))
    stopped: threading.Event = field(default_factory=threading.Event)
    writer_thread: threading.Thread | None = None

    def public_information(self) -> dict:
        return {"username": self.username, "ip": self.ip, "port": self.port}


class AssignmentEventFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return getattr(record, "assignment_event", False)


class DirectoryServer:
    def __init__(self) -> None:
        self.peers: dict[str, Peer] = {}
        self.registry_lock = threading.Lock()
        # Serialize snapshots, but let each peer's writer perform its own network I/O.
        self.broadcast_lock = threading.Lock()

    def broadcast_userlist(self) -> None:
        with self.broadcast_lock:
            with self.registry_lock:
                online_peers = list(self.peers.values())

            packet = {
                "type": "userlist",
                "users": [peer.public_information() for peer in online_peers],
            }
            for peer in online_peers:
                if peer.stopped.is_set():
                    continue
                try:
                    peer.pending_userlists.put_nowait(packet)
                except queue.Full:
                    # A full snapshot supersedes an older snapshot, so retain only the latest.
                    try:
                        peer.pending_userlists.get_nowait()
                        peer.pending_userlists.task_done()
                    except queue.Empty:
                        pass
                    peer.pending_userlists.put_nowait(packet)
            logging.info("Userlist queued for %s peer(s)", len(online_peers))

    def send_userlists(self, peer: Peer) -> None:
        while not peer.stopped.is_set():
            try:
                packet = peer.pending_userlists.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                send_packet(peer.connection_socket, packet)
            except (OSError, ValueError) as error:
                if not peer.stopped.is_set():
                    logging.warning(
                        "Userlist delivery failed: %s", error,
                        extra={"username": peer.username},
                    )
                peer.stopped.set()
                try:
                    peer.connection_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return
            finally:
                peer.pending_userlists.task_done()

    def handle_connection(self, connection_socket: socket.socket) -> None:
        registered_peer = None

        try:
            registration = receive_packet(connection_socket)
            if registration is None:
                return
            if registration.get("type") != "register":
                raise ValueError("The first packet must be a registration.")

            username = registration.get("username")
            listening_ip = registration.get("ip")
            listening_port = registration.get("port")
            if not isinstance(username, str) or not username.strip():
                raise ValueError("Username must be a nonempty string.")
            if not isinstance(listening_ip, str):
                raise ValueError("The peer must provide its listening IPv4 address.")
            ipaddress.IPv4Address(listening_ip)
            if type(listening_port) is not int or not 1 <= listening_port <= 65535:
                raise ValueError("The listening port must be between 1 and 65535.")

            peer = Peer(username, listening_ip, listening_port, connection_socket)
            with self.registry_lock:
                if username in self.peers:
                    raise ValueError("Username is already online.")
                self.peers[username] = peer
                registered_peer = peer
                logging.info(
                    "%s %s connected", peer.ip, peer.port,
                    extra={"username": peer.username, "assignment_event": True},
                )

            peer.writer_thread = threading.Thread(target=self.send_userlists, args=(peer,))
            peer.writer_thread.start()
            self.broadcast_userlist()

            # Keep the directory connection open to detect the peer leaving.
            # Messages and files do not travel through this connection.
            while True:
                packet = receive_packet(connection_socket)
                if packet is None or packet.get("type") == "disconnect":
                    break
                raise ValueError("Only disconnect is accepted after registration.")

        except (OSError, ValueError) as error:
            logging.error(
                "Connection error: %s", error,
                extra={"username": registered_peer.username if registered_peer else "-"},
            )
        finally:
            if registered_peer is not None:
                with self.registry_lock:
                    del self.peers[registered_peer.username]
                    logging.info(
                        "%s %s disconnected", registered_peer.ip, registered_peer.port,
                        extra={"username": registered_peer.username, "assignment_event": True},
                    )
                registered_peer.stopped.set()
            try:
                connection_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection_socket.close()
            if registered_peer is not None:
                if registered_peer.writer_thread is not None:
                    registered_peer.writer_thread.join()
                self.broadcast_userlist()

    def run(self, port: int) -> None:
        connection_workers = []

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening_socket:
            listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listening_socket.bind(("0.0.0.0", port))
            listening_socket.listen()
            logging.info(
                "=== Server started | %s | port=%s ===",
                datetime.now().astimezone().isoformat(timespec="seconds"), port,
            )

            try:
                while True:
                    connection_socket, _ = listening_socket.accept()
                    # Discard finished workers instead of retaining every past session.
                    connection_workers = [
                        (thread, connection)
                        for thread, connection in connection_workers
                        if thread.is_alive()
                    ]
                    connection_thread = threading.Thread(
                        target=self.handle_connection,
                        args=(connection_socket,),
                    )
                    connection_workers.append((connection_thread, connection_socket))
                    connection_thread.start()
            except KeyboardInterrupt:
                pass
            finally:
                for _, connection in connection_workers:
                    try:
                        connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                for thread, _ in connection_workers:
                    thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", type=int, help="TCP port for the central server")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")

    # Keep the assignment's strict format while also recording who owns each endpoint.
    assignment_log = logging.FileHandler("server.log", encoding="utf-8")
    assignment_log.setFormatter(logging.Formatter("%(message)s"))
    assignment_log.addFilter(AssignmentEventFilter())

    detailed_format = logging.Formatter("%(username)s %(message)s", defaults={"username": "server"})
    detailed_log = logging.FileHandler("server-detailed.log", encoding="utf-8")
    detailed_log.setFormatter(detailed_format)
    console_log = logging.StreamHandler(sys.stdout)
    console_log.setFormatter(detailed_format)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[assignment_log, detailed_log, console_log],
    )
    DirectoryServer().run(arguments.port)


if __name__ == "__main__":
    main()
