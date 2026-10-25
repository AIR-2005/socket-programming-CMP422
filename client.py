"""Discover online peers and exchange messages and files through a Tk GUI."""

import argparse
import errno
import logging
from pathlib import Path
import queue
import random
import socket
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from file_transfer import validate_file_path
from messaging import PeerMessenger, validate_peer_information
from protocol import receive_packet, send_packet


class ConversationHistory:
    """Keep each peer's conversation and unread count independently of Tk widgets."""

    def __init__(self) -> None:
        self.active_peer: str | None = None
        self.history: dict[str, list[str]] = {}
        self.unread: dict[str, int] = {}

    def select(self, username: str) -> None:
        self.active_peer = username
        self.unread[username] = 0

    def add(self, username: str, text: str, incoming: bool = False) -> None:
        self.history.setdefault(username, []).append(text)
        if incoming and username != self.active_peer:
            self.unread[username] = self.unread.get(username, 0) + 1

    def visible_lines(self) -> list[str]:
        return list(self.history.get(self.active_peer, []))


def parse_commands(command_text: str) -> list[tuple[str, str]]:
    """Read one command per line, preserving message text and spaces in file paths."""
    commands = []
    for line_number, line in enumerate(command_text.splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(maxsplit=1)
        command = parts[0]
        if command not in ("sndmsg", "sndf"):
            raise ValueError(f"Line {line_number}: unknown command '{parts[0]}'.")
        if len(parts) != 2 or not parts[1].strip():
            raise ValueError(f"Line {line_number}: {command} requires an argument.")
        argument = parts[1]
        if command == "sndf":
            argument = argument.strip()
            quote_pairs = {'"': '"', "'": "'", '“': '”'}
            if argument[0] in quote_pairs:
                if len(argument) < 2 or argument[-1] != quote_pairs[argument[0]]:
                    raise ValueError(f"Line {line_number}: file path has an unmatched quote.")
                argument = argument[1:-1]
            argument = str(validate_file_path(argument))
        commands.append((command, argument))
    if not commands:
        raise ValueError("Enter sndmsg <message> or sndf <full-file-path>.")
    return commands


def create_peer_listener(local_ip: str) -> socket.socket:
    """Try randomly selected high ports, binding before announcing the endpoint."""
    listening_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        for candidate_port in random.sample(range(49152, 65536), 100):
            try:
                listening_socket.bind((local_ip, candidate_port))
                break
            except OSError as error:
                if error.errno != errno.EADDRINUSE:
                    raise
        else:
            raise OSError("Could not find an available P2P listening port.")

        listening_socket.listen()
        return listening_socket
    except OSError:
        listening_socket.close()
        raise


class DirectoryConnection:
    """Network state is separate from the GUI; workers report events via a queue."""

    def __init__(
        self, username: str, events: queue.Queue, working_directory: Path | None = None
    ) -> None:
        self.username = username
        self.events = events
        self.stopped = threading.Event()
        self.socket_lock = threading.Lock()
        self.server_socket: socket.socket | None = None
        self.listening_socket: socket.socket | None = None
        self.messenger = PeerMessenger(username, events, self.stopped, working_directory)

    def connect_and_receive(self, server_ip: str, server_port: int) -> None:
        try:
            self.messenger.report_activity(f"Connecting to server {server_ip}:{server_port}", notify=False)
            with socket.create_connection(
                (server_ip, server_port), timeout=5
            ) as server_socket:
                # The outgoing connection tells us which local interface reaches this server.
                local_ip = server_socket.getsockname()[0]
                with create_peer_listener(local_ip) as listening_socket:
                    with self.socket_lock:
                        if self.stopped.is_set():
                            return
                        self.server_socket = server_socket
                        self.listening_socket = listening_socket
                        self.messenger.start(listening_socket)

                    send_packet(server_socket, {
                        "type": "register",
                        "username": self.username,
                        "ip": local_ip,
                        "port": listening_socket.getsockname()[1],
                    })
                    self.messenger.report_activity("Registration sent to server", notify=False)
                    # An idle directory connection is normal, not a five-second failure.
                    server_socket.settimeout(None)

                    while not self.stopped.is_set():
                        packet = receive_packet(server_socket)
                        if packet is None:
                            self.messenger.report_activity("Server closed the connection.", logging.WARNING)
                            break
                        if packet.get("type") != "userlist":
                            raise ValueError("Expected a userlist packet from the server.")
                        users = packet.get("users")
                        if not isinstance(users, list):
                            raise ValueError("The userlist must be a list.")
                        for user in users:
                            validate_peer_information(user)
                        self.messenger.report_activity(
                            f"Userlist received: {len(users)} peer(s)", notify=False
                        )
                        self.events.put(("userlist", users))
        except (OSError, ValueError) as error:
            if not self.stopped.is_set():
                self.messenger.report_activity(f"Connection error: {error}", logging.ERROR)
        finally:
            self.close()
            self.events.put(("disconnected", None))

    def close(self) -> None:
        """Shutdown wakes a worker blocked in recv; this method can be called twice."""
        self.stopped.set()
        with self.socket_lock:
            connections = (self.server_socket, self.listening_socket)
            self.server_socket = None
            self.listening_socket = None

        self.messenger.close()
        for connection in connections:
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()


class ClientWindow:
    def __init__(self, root: tk.Tk, server_ip: str, server_port: int, username: str) -> None:
        self.root = root
        self.username = username
        self.online_users: dict[str, dict] = {}
        self.displayed_users: list[str] = []
        self.conversations = ConversationHistory()
        self.events = queue.Queue()
        self.file_offers = queue.Queue()
        self.file_dialog_open = False
        self.connection = DirectoryConnection(username, self.events)

        root.title(f"Peer — {username}")
        root.geometry("800x550")
        root.minsize(600, 420)
        root.protocol("WM_DELETE_WINDOW", self.close_window)

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        self.status = tk.StringVar(value=f"Connecting to {server_ip}:{server_port}…")
        ttk.Label(frame, textvariable=self.status).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        self.conversation_title = tk.StringVar(value="Select a user to start a conversation")
        ttk.Label(frame, textvariable=self.conversation_title).grid(
            row=1, column=0, sticky="w", pady=(0, 4)
        )
        self.output = tk.Text(frame, width=48, height=12, state="disabled", wrap="word")
        self.output.grid(row=2, column=0, sticky="nsew", padx=(0, 12))

        users_frame = ttk.LabelFrame(frame, text="Online users", padding=6)
        users_frame.grid(row=1, column=1, rowspan=2, sticky="nsew")
        self.user_list = tk.Listbox(users_frame, width=20, exportselection=False)
        self.user_list.pack(fill="both", expand=True)
        self.user_list.bind("<<ListboxSelect>>", self.select_conversation)

        ttk.Label(frame, text="Commands").grid(
            row=3, column=0, sticky="w", pady=(10, 4)
        )
        self.command_area = tk.Text(frame, height=5, width=48, wrap="word")
        self.command_area.grid(row=4, column=0, sticky="nsew", padx=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=1, sticky="nsew")
        self.execute_button = ttk.Button(
            buttons, text="Execute", command=self.execute_commands, state="disabled"
        )
        self.execute_button.pack(fill="x", pady=(0, 8))
        self.disconnect_button = ttk.Button(
            buttons, text="Disconnect", command=self.connection.close
        )
        self.disconnect_button.pack(fill="x")
        self.activity_status = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.activity_status, wraplength=720).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )

        # Tk stays on the main thread; all blocking network reads use this worker.
        self.worker = threading.Thread(
            target=self.connection.connect_and_receive,
            args=(server_ip, server_port),
            daemon=True,
        )
        self.worker.start()
        root.after(100, self.process_events)

    def append_output(self, text: str) -> None:
        """Show system feedback outside the selected peer's conversation."""
        self.activity_status.set(text)

    def render_conversation(self) -> None:
        username = self.conversations.active_peer
        title = f"Conversation with {username}" if username else "Select a user to start a conversation"
        if username and username not in self.online_users:
            title += " (offline)"
        self.conversation_title.set(title)
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        lines = self.conversations.visible_lines()
        if lines:
            self.output.insert("end", "\n".join(lines) + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def refresh_user_list(self) -> None:
        self.displayed_users = sorted(self.online_users)
        self.user_list.delete(0, "end")
        for index, username in enumerate(self.displayed_users):
            unread = self.conversations.unread.get(username, 0)
            label = f"{username} ({unread} unread)" if unread else username
            self.user_list.insert("end", label)
            if username == self.conversations.active_peer:
                self.user_list.selection_set(index)

    def select_conversation(self, event=None) -> None:
        selected = self.user_list.curselection()
        if not selected:
            return
        username = self.displayed_users[selected[0]]
        if username == self.conversations.active_peer and not self.conversations.unread.get(username):
            return
        self.conversations.select(username)
        self.refresh_user_list()
        self.render_conversation()

    def add_conversation(self, username: str, text: str, incoming: bool = False) -> None:
        if self.conversations.active_peer is None:
            self.conversations.select(username)
        self.conversations.add(username, text, incoming)
        if incoming and username != self.conversations.active_peer:
            self.append_output(f"New activity from {username}")
        self.refresh_user_list()
        self.render_conversation()

    def execute_commands(self) -> None:
        selected = self.user_list.curselection()
        if not selected:
            self.append_output("Select a recipient from Online users first.")
            self.connection.messenger.report_activity("Command rejected: no recipient selected", notify=False)
            return
        recipient = self.online_users[self.displayed_users[selected[0]]]
        try:
            commands = parse_commands(self.command_area.get("1.0", "end-1c"))
            for command, argument in commands:
                if command == "sndmsg":
                    self.connection.messenger.queue_message(recipient, argument)
                else:
                    self.connection.messenger.queue_file(recipient, argument)
        except (OSError, ValueError) as error:
            self.append_output(str(error))
            self.connection.messenger.report_activity(f"Command error: {error}", logging.ERROR, notify=False)
            return
        self.command_area.delete("1.0", "end")

    def confirm_next_file(self) -> None:
        if self.file_dialog_open or self.connection.stopped.is_set():
            return
        try:
            offer = self.file_offers.get_nowait()
        except queue.Empty:
            return
        self.file_dialog_open = True
        accepted = False
        try:
            accepted = messagebox.askyesno(
                "Incoming file",
                f"{offer.sender['username']} wants to send:\n"
                f"{offer.filename}\n{offer.size:,} bytes\n\nAccept this file?",
                parent=self.root,
            )
        except tk.TclError:
            # Closing the application while the dialog is open rejects the offer.
            pass
        finally:
            if accepted and not self.connection.stopped.is_set():
                self.add_conversation(offer.sender["username"],
                    f"Accepted {offer.filename} from {offer.sender['username']}."
                )
            offer.decision.put(accepted)
            self.file_dialog_open = False

    def process_events(self) -> None:
        while True:
            try:
                event_type, data = self.events.get_nowait()
            except queue.Empty:
                break

            if event_type == "userlist":
                self.online_users = {
                    user["username"]: user for user in data
                    if user["username"] != self.username
                }
                self.refresh_user_list()
                self.render_conversation()
                self.status.set(f"Connected as {self.username}")
                self.execute_button.state(["!disabled"])
                self.append_output(
                    f"Userlist updated: {len(self.online_users)} other peer(s) online."
                )
            elif event_type == "info":
                self.append_output(data)
            elif event_type == "message":
                peer_username = data["peer"]["username"]
                if data["direction"] == "to":
                    label = f"{self.username} → {peer_username}"
                else:
                    label = f"{peer_username} → {self.username}"
                self.add_conversation(
                    peer_username, f"{label}: {data['content']}", data["direction"] == "from"
                )
            elif event_type == "file_offer":
                self.file_offers.put(data)
            elif event_type == "file":
                peer_username = data["peer"]["username"]
                if data["direction"] == "from":
                    self.add_conversation(
                        peer_username,
                        f"Received {data['filename']} from {peer_username}: {data['destination']}",
                        incoming=True,
                    )
                else:
                    self.add_conversation(peer_username, f"Sent {data['filename']} to {peer_username}.")
            elif event_type == "disconnected":
                self.status.set("Disconnected")
                self.online_users.clear()
                self.refresh_user_list()
                self.render_conversation()
                self.disconnect_button.state(["disabled"])
                self.execute_button.state(["disabled"])

        if not self.file_dialog_open and not self.file_offers.empty():
            self.root.after(0, self.confirm_next_file)
        self.root.after(100, self.process_events)

    def close_window(self) -> None:
        self.connection.close()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server_ip")
    parser.add_argument("server_port", type=int)
    parser.add_argument("username")
    arguments = parser.parse_args()
    if not 1 <= arguments.server_port <= 65535:
        parser.error("server_port must be between 1 and 65535")
    if not arguments.username.strip():
        parser.error("username must not be empty")

    root = tk.Tk()
    ClientWindow(root, arguments.server_ip, arguments.server_port, arguments.username)
    root.mainloop()


if __name__ == "__main__":
    main()
