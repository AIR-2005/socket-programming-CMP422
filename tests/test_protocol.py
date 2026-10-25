import json
import socket
import threading
import unittest

from protocol import HEADER, MAX_PACKET_SIZE, receive_packet, send_packet


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.sender, self.receiver = socket.socketpair()
        self.sender.settimeout(2)
        self.receiver.settimeout(2)
        self.addCleanup(self.sender.close)
        self.addCleanup(self.receiver.close)

    def test_fragmented_unicode_and_consecutive_packets(self):
        packet = {"type": "message", "content": "Merhaba Ayşe 👋"}
        payload = json.dumps(packet, ensure_ascii=False).encode("utf-8")
        frame = HEADER.pack(len(payload)) + payload
        errors = []

        def send_fragments():
            try:
                for byte in frame:
                    self.sender.sendall(bytes([byte]))
                send_packet(self.sender, {"type": "next"})
                self.sender.shutdown(socket.SHUT_WR)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=send_fragments)
        worker.start()
        self.assertEqual(receive_packet(self.receiver), packet)
        self.assertEqual(receive_packet(self.receiver), {"type": "next"})
        self.assertIsNone(receive_packet(self.receiver))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)

    def test_truncated_header(self):
        self.sender.sendall(b"\0\0")
        self.sender.shutdown(socket.SHUT_WR)
        with self.assertRaises(ConnectionError):
            receive_packet(self.receiver)

    def test_truncated_payload(self):
        self.sender.sendall(HEADER.pack(10) + b"abc")
        self.sender.shutdown(socket.SHUT_WR)
        with self.assertRaises(ConnectionError):
            receive_packet(self.receiver)

    def test_invalid_packet_lengths(self):
        for size in (0, MAX_PACKET_SIZE + 1):
            with self.subTest(size=size):
                self.sender.sendall(HEADER.pack(size))
                with self.assertRaises(ValueError):
                    receive_packet(self.receiver)

    def test_non_object_json_is_rejected(self):
        payload = b"[]"
        self.sender.sendall(HEADER.pack(len(payload)) + payload)
        with self.assertRaises(ValueError):
            receive_packet(self.receiver)
