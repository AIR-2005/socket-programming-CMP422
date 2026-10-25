from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from client import ClientWindow, ConversationHistory, parse_commands
from file_transfer import FileOffer
from tests.helpers import PROJECT_ROOT


class ClientTests(unittest.TestCase):
    def test_conversations_and_unread_counts_are_separate(self):
        conversations = ConversationHistory()
        conversations.select("Ayse")
        conversations.add("Ayse", "Merhaba")
        conversations.add("Harun", "Gelen message", incoming=True)
        self.assertEqual(conversations.visible_lines(), ["Merhaba"])
        self.assertEqual(conversations.unread["Harun"], 1)
        conversations.select("Harun")
        self.assertEqual(conversations.visible_lines(), ["Gelen message"])
        self.assertEqual(conversations.unread["Harun"], 0)
        conversations.add("Harun", "Yeni message", incoming=True)
        self.assertEqual(conversations.unread["Harun"], 0)
        conversations.select("Ayse")
        self.assertEqual(conversations.visible_lines(), ["Merhaba"])

    def test_command_paths_quotes_and_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "örnek dosya.bin"
            path.touch()
            for opening, closing in (('"', '"'), ("'", "'"), ("“", "”"), ("", "")):
                with self.subTest(quotes=opening):
                    commands = parse_commands(
                        f"sndmsg Merhaba  Ayşe\nsndf {opening}{path}{closing}\nsndmsg Devam"
                    )
                    self.assertEqual(commands, [
                        ("sndmsg", "Merhaba  Ayşe"), ("sndf", str(path)), ("sndmsg", "Devam")
                    ])

    def test_background_peer_message_does_not_replace_selected_conversation(self):
        window = ClientWindow.__new__(ClientWindow)
        window.username = "Baris"
        window.conversations = ConversationHistory()
        window.conversations.select("Ayse")
        window.conversations.add("Ayse", "Existing conversation")
        window.events = queue.Queue()
        window.events.put(("message", {
            "peer": {"username": "Harun"}, "direction": "from", "content": "Hello",
        }))
        window.file_dialog_open = False
        window.file_offers = queue.Queue()
        window.root = SimpleNamespace(after=lambda *args: None)
        window.refresh_user_list = lambda: None
        rendered = []
        window.render_conversation = lambda: rendered.append(window.conversations.visible_lines())
        notices = []
        window.append_output = notices.append
        window.process_events()
        self.assertEqual(rendered[-1], ["Existing conversation"])
        self.assertEqual(window.conversations.history["Harun"], ["Harun → Baris: Hello"])
        self.assertEqual(window.conversations.unread["Harun"], 1)
        self.assertIn("Harun", notices[-1])

    def test_invalid_commands(self):
        for text in ("", "sndmsg", "sndf", "sndf relative.bin", 'sndf "unclosed',
                     "unknown command", "sndmsg valid\nunknown second"):
            with self.subTest(command=text), self.assertRaises(ValueError):
                parse_commands(text)

    def test_file_dialog_returns_the_users_decision(self):
        for answer in (True, False):
            window = ClientWindow.__new__(ClientWindow)
            window.connection = SimpleNamespace(stopped=threading.Event())
            window.file_dialog_open = False
            window.file_offers = queue.Queue()
            window.root = object()
            window.add_conversation = lambda *args, **kwargs: None
            offer = FileOffer({"username": "Baris"}, "test.bin", 4)
            window.file_offers.put(offer)
            with patch("client.messagebox.askyesno", return_value=answer) as dialog:
                window.confirm_next_file()
                dialog.assert_called_once()
            self.assertIs(offer.decision.get_nowait(), answer)
            self.assertFalse(window.file_dialog_open)

    def test_assignment_entrypoints_accept_help(self):
        for entrypoint in ("client", "server"):
            with self.subTest(entrypoint=entrypoint):
                result = subprocess.run(
                    [sys.executable, "-B", str(PROJECT_ROOT / entrypoint), "--help"],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("usage:", result.stdout)
