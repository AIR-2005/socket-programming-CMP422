from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import messaging
from file_transfer import CHUNK_SIZE
from protocol import receive_packet, send_packet
from tests.helpers import PeerSession, ServerProcess


class PeerTransferTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="peer-transfer-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.server = ServerProcess()
        self.addCleanup(self.server.close)
        self.baris = PeerSession("Baris", self.server.port, self.root / "Baris")
        self.addCleanup(self.baris.close)
        self.baris.wait_for("userlist")
        self.ayse = PeerSession("Ayse", self.server.port, self.root / "Ayse")
        self.addCleanup(self.ayse.close)
        for peer in (self.baris, self.ayse):
            userlist = peer.wait_for("userlist", lambda users: len(users) == 2)
        self.peers = {peer["username"]: peer for peer in userlist}
        self.downloads = self.root / "Ayse" / "download"

    def source_file(self, filename="örnek dosya.bin", content=b"example"):
        source = self.root / filename
        source.write_bytes(content)
        return source

    def offer_file(self, source):
        self.baris.connection.messenger.queue_file(self.peers["Ayse"], str(source))
        return self.ayse.wait_for("file_offer")

    def completed_file(self):
        incoming = self.ayse.wait_for("file")
        outgoing = self.baris.wait_for("file")
        self.assertEqual(incoming["direction"], "from")
        self.assertEqual(outgoing["direction"], "to")
        return Path(incoming["destination"])

    def test_bidirectional_messages_keep_command_order(self):
        for index in range(10):
            self.baris.connection.messenger.queue_message(self.peers["Ayse"], f"Merhaba Ayşe {index}")
            self.ayse.connection.messenger.queue_message(self.peers["Baris"], f"Cevap {index}")
        for peer, prefix in ((self.ayse, "Merhaba Ayşe"), (self.baris, "Cevap")):
            for index in range(10):
                event = peer.wait_for("message", lambda data: data["direction"] == "from")
                self.assertEqual(event["content"], f"{prefix} {index}")
            for _ in range(10):
                peer.wait_for("message", lambda data: data["direction"] == "to")

    def test_acceptance_delay_and_messaging_during_binary_transfer(self):
        content = bytes(range(256)) * 6000
        source = self.source_file(content=content)
        started = threading.Event()
        resume = threading.Event()
        original_send = messaging.send_file_bytes

        def paused_send(connection, source_file, size, stopped):
            chunk = source_file.read(min(CHUNK_SIZE, size))
            connection.sendall(chunk)
            started.set()
            if not resume.wait(timeout=8):
                raise ConnectionError("The test did not resume its paused transfer.")
            original_send(connection, source_file, size - len(chunk), stopped)

        with patch("messaging.send_file_bytes", side_effect=paused_send):
            try:
                offer = self.offer_file(source)
                time.sleep(5.2)
                self.assertFalse(started.is_set())
                self.assertFalse(self.downloads.exists())
                offer.decision.put(True)
                self.assertTrue(started.wait(timeout=3))
                self.assertLess((self.downloads / source.name).stat().st_size, len(content))

                for sender, receiver, recipient in (
                    (self.baris, self.ayse, "Ayse"), (self.ayse, self.baris, "Baris")
                ):
                    sender.connection.messenger.queue_message(self.peers[recipient], "During transfer 👋")
                    self.assertEqual(receiver.wait_for("message")["content"], "During transfer 👋")
                    sender.wait_for("message")
                resume.set()
                self.assertEqual(self.completed_file().read_bytes(), content)
            finally:
                resume.set()

    def test_rejection_is_logged_without_creating_a_download(self):
        source = self.source_file("rejected.bin")
        self.offer_file(source).decision.put(False)
        self.baris.wait_for("info", lambda text: "rejected rejected.bin" in text)
        self.ayse.wait_for("info", lambda text: "Rejected rejected.bin" in text)
        self.assertFalse(self.downloads.exists())
        receiver_detail = (self.root / "Ayse" / "client-Ayse-detailed.log").read_text()
        sender_detail = (self.root / "Baris" / "client-Baris-detailed.log").read_text()
        self.assertIn("Rejected rejected.bin", receiver_detail)
        self.assertIn("rejected rejected.bin", sender_detail)
        strict_log = self.root / "Ayse" / "client-Ayse.log"
        self.assertFalse(strict_log.exists())

    def test_empty_files_and_duplicate_names(self):
        source = self.source_file("empty.txt", b"")
        self.offer_file(source).decision.put(True)
        first = self.completed_file()
        self.assertEqual(first.read_bytes(), b"")
        self.offer_file(source).decision.put(True)
        second = self.completed_file()
        self.assertEqual(second.name, "empty (1).txt")
        self.assertTrue(first.exists())
        receiver_log = (self.root / "Ayse" / "client-Ayse.log").read_text().splitlines()
        expected = f"empty.txt from Baris@127.0.0.1:{self.peers['Baris']['port']}"
        self.assertEqual(receiver_log, [expected, expected])

    def test_incomplete_download_is_removed(self):
        with socket.create_connection(("127.0.0.1", self.peers["Ayse"]["port"]), timeout=3) as peer:
            send_packet(peer, {
                "type": "file_offer", "sender": self.peers["Baris"],
                "filename": "partial.bin", "size": 100,
            })
            self.ayse.wait_for("file_offer").decision.put(True)
            self.assertTrue(receive_packet(peer)["accepted"])
            peer.sendall(b"abc")
            peer.shutdown(socket.SHUT_WR)
            self.assertIsNone(receive_packet(peer))
        self.ayse.wait_for("info", lambda text: "before the file was complete" in text)
        self.assertFalse((self.downloads / "partial.bin").exists())
        self.assertIn("before the file was complete", (
            self.root / "Ayse" / "client-Ayse-detailed.log"
        ).read_text())

    def test_invalid_file_metadata_never_reaches_confirmation(self):
        for filename, size in (("../escape.bin", 1), ("invalid.bin", -1), ("invalid.bin", True)):
            with self.subTest(filename=filename, size=size):
                with socket.create_connection(("127.0.0.1", self.peers["Ayse"]["port"]), timeout=3) as peer:
                    send_packet(peer, {
                        "type": "file_offer", "sender": self.peers["Baris"],
                        "filename": filename, "size": size,
                    })
                    self.assertIsNone(receive_packet(peer))
                self.ayse.wait_for("info", lambda text: "Incoming transfer error" in text)
        self.assertFalse(self.downloads.exists())

    def test_unreachable_peer_is_only_in_detailed_log(self):
        self.ayse.close()
        self.baris.wait_for("userlist", lambda users: len(users) == 1)
        self.baris.connection.messenger.queue_message(self.peers["Ayse"], "Cannot deliver")
        self.baris.wait_for("info", lambda text: "could not be confirmed" in text)
        self.assertFalse((self.root / "Baris" / "client-Baris.log").exists())
        self.assertIn("could not be confirmed", (
            self.root / "Baris" / "client-Baris-detailed.log"
        ).read_text())

    def test_disconnect_cancels_a_pending_file_offer(self):
        self.offer_file(self.source_file())
        self.ayse.close()
        self.baris.wait_for("info", lambda text: "could not be confirmed" in text)
        self.assertFalse(self.downloads.exists())
