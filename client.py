import socket
import threading
import json
import tkinter as tk
from tkinter import messagebox
import sys
import os

class Client:
    def __init__(self, server_ip, server_port, username):
        # Get server ip, server port and user name from command line
        self.server_ip = server_ip
        self.server_port = int(server_port)
        self.username = username

        self.user_ip = None
        self.listen_port = None
        self.listener_socket = None
        self.server_socket = None
        self.user_dict = {}

        # Log file
        self.log_file_name = f"{self.username}.log"

        # Initialize root
        self.root = tk.Tk()
        self.root.title(self.username)
        self.root.geometry("1280x720")
        self.root.protocol("WM_DELETE_WINDOW", self.disconnect)
        self.init_gui()

    # Initialize GUI
    def init_gui(self):
        # Left side: Output Area (top) + Command Input Area (bottom)
        left_frame = tk.Frame(self.root)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.output_area = tk.Text(left_frame, height=15, state=tk.DISABLED)
        self.output_area.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.cmd_area = tk.Text(left_frame, height=6)
        self.cmd_area.pack(fill=tk.BOTH, expand=False)

        # Right side: Users Listbox (top) + Execute & Disconnect Buttons (bottom)
        right_frame = tk.Frame(self.root, width=150)
        right_frame.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 10), pady=10)
        right_frame.pack_propagate(False)

        users_group = tk.LabelFrame(right_frame, text="Users")
        users_group.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.user_listbox = tk.Listbox(users_group, exportselection=False)
        self.user_listbox.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.execute_btn = tk.Button(right_frame, text="Execute", height=2, command=self.execute_commands)
        self.execute_btn.pack(fill=tk.X, pady=(0, 5))

        self.disconnect_btn = tk.Button(right_frame, text="Disconnect", height=2, command=self.disconnect)
        self.disconnect_btn.pack(fill=tk.X)

    # Temporarly enable editing and add output to text box
    def write_output(self, text):
        self.output_area.config(state=tk.NORMAL)
        self.output_area.insert(tk.END, text + "\n")
        self.output_area.see(tk.END)
        self.output_area.config(state=tk.DISABLED)

    # Update user list when updated user list arrives
    def update_user_list(self):
        self.user_listbox.delete(0, tk.END)
        for user in self.user_dict:
            if user != self.username:
                self.user_listbox.insert(tk.END, user)

    # Execute function for execute button
    def execute_commands(self):
        # Select user from user list
        selected = self.user_listbox.curselection()
        if not selected:
            messagebox.showwarning("Warning", "Please select a user from the Users list first.")
            return
        target_username = self.user_listbox.get(selected[0])

        # Read all lines from the command box
        raw_text = self.cmd_area.get("1.0", tk.END).strip()
        if not raw_text:
            return
        # Delete readed text from command box
        self.cmd_area.delete("1.0", tk.END)
        # For every command, send it to sender function
        for cmd_line in raw_text.splitlines():
            cmd_line = cmd_line.strip()
            if cmd_line:
                threading.Thread(
                    target=self.sender, args=(target_username, cmd_line), daemon=True
                ).start()

    # Write log function
    def write_log(self, log_text):
        with open(self.log_file_name, "a", encoding="utf-8") as f:
            f.write(log_text + "\n")

    # Start
    def start(self):
        # Create Listener Socket for client
        self.listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener_socket.bind(("", 0))
        self.listener_socket.listen(10)
        self.listen_port = self.listener_socket.getsockname()[1]

        # Connect to server port
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.connect((self.server_ip, self.server_port))
        self.user_ip = self.server_socket.getsockname()[0]

        self.server_socket.sendall((json.dumps({
            "type": "connect",
            "username": self.username,
            "ip": self.user_ip,
            "port": self.listen_port
        }) + "\n").encode())

        threading.Thread(target=self.client_listener, daemon=True).start()
        threading.Thread(target=self.server_connect, daemon=True).start()
        # Start GUI event loop (blocks until window is closed)
        self.root.mainloop()

    # Server Connector
    def server_connect(self):
        self.reader = self.server_socket.makefile("r", encoding="utf-8")
        for line in self.reader:
            server_msg = json.loads(line)
            if server_msg.get("type") == "userlist":
                # Convert [{"username": u, "ip": ip, "port": p}, ...] -> {u: (ip, p)}
                self.user_dict = {
                    u["username"]: (u["ip"], u["port"]) for u in server_msg["users"]
                }
                print(self.user_dict)
                self.root.after(0, self.update_user_list)

    # Client Listener
    def client_listener(self):
        while True:
            # Accept Connection
            connection, addr = self.listener_socket.accept()
            data = connection.recv(4096).decode()
            if data:
                msg_info = json.loads(data)
                if msg_info["type"] == "msg":
                    sender = msg_info["username"]
                    content = msg_info["content"]
                    sender_ip, sender_port = self.user_dict[sender]

                    # Print to GUI output area and log file
                    self.root.after(0, self.write_output, f"{sender} > {content}")
                    self.write_log(f'Message from {sender}@{sender_ip}:{sender_port} "{content}"')
                elif msg_info["type"] == "file":
                    # Get file name and size, sender username
                    file_name = msg_info["fileName"]
                    file_size = msg_info["fileSize"]
                    sender = msg_info["username"]
                    sender_ip, sender_port = self.user_dict[sender]
                    # Create threading event to wait and get user response for download
                    confirm_event = threading.Event()
                    decision = {"accepted": False}
                    # Helper function to get user response
                    def ask_confirmation():
                        decision["accepted"] = messagebox.askyesno(
                            "File Transfer",
                            f"Do you want to accept '{file_name}' ({file_size} bytes) from {sender}?"
                        )
                        confirm_event.set()
                    # Run ask_conformation and wait for the response
                    self.root.after(0, ask_confirmation)
                    confirm_event.wait()
                    # Decision
                    if decision["accepted"]:
                        # Send accept to sender to start file transfer
                        connection.sendall("accept".encode())
                        # Create download path if it doesn't exist and create save path for downloaded file
                        os.makedirs("download", exist_ok=True)
                        save_path = os.path.join("download", file_name)
                        # Receive file as chunks
                        received = 0
                        with open(save_path, "wb") as file:
                            while received < file_size:
                                chunk = connection.recv(4096)
                                if not chunk:
                                    break
                                file.write(chunk)
                                received += len(chunk)
                        # Print file reiceved message and write log
                        self.root.after(0, self.write_output, f"{self.username} > file accepted")
                        self.write_log(f"{file_name} from {sender}@{sender_ip}:{sender_port}")
            connection.close()

    # Sender
    def sender(self, username, cmd):
        if " " not in cmd:
            return
        send_ip, send_port = self.user_dict[username]
        send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        send_socket.connect((send_ip, send_port))
        cmd_type, cmd_content = cmd.split(" ", 1)
        if cmd_type == "sndmsg":
            # Send message info and content
            send_socket.sendall(json.dumps({
                "type": "msg",
                "username": self.username,
                "content": cmd_content
            }).encode())
            # Print outgoing message to GUI and write to log
            self.root.after(0, self.write_output, f"{self.username} > {cmd_content}")
            self.write_log(f'Message to {username}@{send_ip}:{send_port} "{cmd_content}"')
        elif cmd_type == "sndf":
            # Clean the path file
            file_path = cmd_content.strip().strip('"')
            # If file is not in file path
            if not os.path.isfile(file_path):
                self.root.after(0, self.write_output, f"File not found: {file_path}")
                send_socket.close()
                return
            # Get file name and size
            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)

            # Send file info and size
            send_socket.sendall(json.dumps({
                "type": "file",
                "username": self.username,
                "fileName": file_name,
                "fileSize": file_size
            }).encode())
            self.root.after(0, self.write_output, f"{self.username} > file sent {cmd_content}")
            # Wait response from receiver
            response = send_socket.recv(1024).decode()
            # If file transfer is accepted
            if response == "accept":
                self.root.after(0, self.write_output, f"{username} > file accepted")
                # Read file as chunks and send it
                with open(file_path, "rb") as file:
                    while True:
                        chunk = file.read(4096)
                        if not chunk:
                            break
                        send_socket.sendall(chunk)
                # Write to log
                self.write_log(f"{file_name} to {username}@{send_ip}:{send_port}")
        else:
            pass #NOPE
        send_socket.close()

   # Disconnect
    def disconnect(self):
        # Close listener and server sockets
        self.server_socket.close()
        self.listener_socket.close()
        # Close window
        self.root.destroy()

# Run client
if __name__ == "__main__":
    if len(sys.argv) == 4:
        client = Client(sys.argv[1], sys.argv[2], sys.argv[3])
        client.start()
    else:
            print("Usage: python client.py <server-ip-address> <server-port-number> <user-name>")


