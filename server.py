"""Central peer directory: register peers and broadcast the online userlist."""

import argparse
from dataclasses import dataclass
import ipaddress
import logging
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

    def public_information(self) -> dict:
        return {"username": self.username, "ip": self.ip, "port": self.port}


class DirectoryServer:
    def __init__(self) -> None:
        self.peers: dict[str, Peer] = {}
        self.registry_lock = threading.Lock()
        # One broadcast at a time prevents overlapping frames and stale snapshots.
        self.broadcast_lock = threading.Lock()

    def broadcast_userlist(self) -> None:
        with self.broadcast_lock:
            with self.registry_lock:
                online_peers = list(self.peers.values())

            packet = {
                "type": "userlist",
                "users": [peer.public_information() for peer in online_peers],
            }
            # Network I/O happens after releasing the registry lock.
            for peer in online_peers:
                try:
                    send_packet(peer.connection_socket, packet)
                except OSError:
                    # Wake the receiving thread so its finally block removes this peer.
                    try:
                        peer.connection_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

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
                    extra={"username": peer.username},
                )

            self.broadcast_userlist()

            # Keep the directory connection open to detect the peer leaving.
            # Messages and files do not travel through this connection.
            while True:
                packet = receive_packet(connection_socket)
                if packet is None or packet.get("type") == "disconnect":
                    break
                raise ValueError("Only disconnect is accepted after registration.")

        except (OSError, ValueError) as error:
            print(f"Connection error: {error}", file=sys.stderr)
        finally:
            if registered_peer is not None:
                with self.registry_lock:
                    del self.peers[registered_peer.username]
                    logging.info(
                        "%s %s disconnected", registered_peer.ip, registered_peer.port,
                        extra={"username": registered_peer.username},
                    )
            connection_socket.close()
            if registered_peer is not None:
                self.broadcast_userlist()

    def run(self, port: int) -> None:
        connection_workers = []

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listening_socket:
            listening_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listening_socket.bind(("0.0.0.0", port))
            listening_socket.listen()

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

    detailed_format = logging.Formatter("%(username)s %(message)s")
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
