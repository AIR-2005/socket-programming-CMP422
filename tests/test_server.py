from pathlib import Path
import socket
import threading
import unittest

from protocol import receive_packet, send_packet
from server import DirectoryServer, Peer
from tests.helpers import ServerProcess


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.server = ServerProcess()
        self.addCleanup(self.server.close)

    def register(self, username):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        connection = self.server.connect()
        self.addCleanup(connection.close)
        port = listener.getsockname()[1]
        send_packet(connection, {
            "type": "register", "username": username, "ip": "127.0.0.1", "port": port,
        })
        return connection, port

    def wait_for_users(self, connection, expected):
        for _ in range(10):
            packet = receive_packet(connection)
            self.assertEqual(packet["type"], "userlist")
            users = {peer["username"]: peer for peer in packet["users"]}
            if set(users) == expected:
                return users
        self.fail("The expected userlist did not arrive.")

    def test_join_disconnect_and_new_endpoint(self):
        baris, baris_port = self.register("Baris")
        self.wait_for_users(baris, {"Baris"})
        ayse, old_port = self.register("Ayse")
        for connection in (baris, ayse):
            users = self.wait_for_users(connection, {"Baris", "Ayse"})
            self.assertEqual(users["Ayse"]["port"], old_port)
        self.assertNotEqual(baris_port, baris.getsockname()[1])
        send_packet(ayse, {"type": "disconnect"})
        self.assertIsNone(receive_packet(ayse))
        self.wait_for_users(baris, {"Baris"})
        ayse, new_port = self.register("Ayse")
        self.assertNotEqual(old_port, new_port)
        for connection in (baris, ayse):
            users = self.wait_for_users(connection, {"Baris", "Ayse"})
            self.assertEqual(users["Ayse"]["port"], new_port)

        lines = Path(self.server.directory.name, "server.log").read_text().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(len(line.split()) == 3 for line in lines))
        self.assertTrue(all(line.split()[2] in ("connected", "disconnected") for line in lines))
        detail = Path(self.server.directory.name, "server-detailed.log").read_text()
        self.assertIn(f"Ayse 127.0.0.1 {new_port} connected", detail)
        self.assertIn("Userlist queued", detail)

    def test_registration_error_only_enters_detailed_log(self):
        with self.server.connect() as connection:
            send_packet(connection, {"type": "message", "content": "wrong endpoint"})
            self.assertIsNone(receive_packet(connection))
        log_directory = Path(self.server.directory.name)
        self.assertEqual((log_directory / "server.log").read_text(), "")
        self.assertIn("first packet must be a registration", (
            log_directory / "server-detailed.log"
        ).read_text())


class BroadcastTests(unittest.TestCase):
    def test_nonreading_peer_does_not_block_other_userlists(self):
        directory = DirectoryServer()
        slow_socket, slow_client = socket.socketpair()
        fast_socket, fast_client = socket.socketpair()
        slow_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        fast_client.settimeout(3)
        slow = Peer("Slow", "127.0.0.1", 51000, slow_socket)
        fast = Peer("Fast", "127.0.0.1", 52000, fast_socket)
        directory.peers = {peer.username: peer for peer in (slow, fast)}
        for peer in (slow, fast):
            peer.writer_thread = threading.Thread(target=directory.send_userlists, args=(peer,))
            peer.writer_thread.start()

        next_broadcast = threading.Event()
        next_broadcast.set()
        producer_stopped = threading.Event()
        errors = []

        def publish_updates():
            try:
                for _ in range(80):
                    if not next_broadcast.wait(timeout=3) or producer_stopped.is_set():
                        return
                    next_broadcast.clear()
                    directory.broadcast_userlist()
            except Exception as error:
                errors.append(error)

        producer = threading.Thread(target=publish_updates)
        producer.start()
        try:
            # Slow never reads: its small send buffer fills, while Fast must keep receiving.
            for _ in range(80):
                self.assertEqual(receive_packet(fast_client)["type"], "userlist")
                next_broadcast.set()
            self.assertLessEqual(slow.pending_userlists.qsize(), 1)
            self.assertFalse(errors)
        finally:
            producer_stopped.set()
            next_broadcast.set()
            for peer in (slow, fast):
                peer.stopped.set()
            for connection in (slow_socket, slow_client, fast_socket, fast_client):
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
            producer.join(timeout=3)
            for peer in (slow, fast):
                peer.writer_thread.join(timeout=3)
                self.assertFalse(peer.writer_thread.is_alive())
            self.assertFalse(producer.is_alive())
