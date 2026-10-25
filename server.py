
import socket
import threading
import json
import sys

class Server:
    def __init__(self, port):
        self.user_dict = {}
        self.server_ip = socket.gethostbyname(socket.gethostname())
        self.server_port = int(port)
        self.listener_socket = None
        self.log_file_name = "server.log"

    def write_log(self, log_text):
        with open(self.log_file_name, "a", encoding="utf-8") as f:
                f.write(log_text + "\n")

    def send_userlist(self):
        users = [{"username": u, "ip": ip, "port": p} for u, (ip, p, _) in self.user_dict.items()]
        connections = [c for (_, _, c) in self.user_dict.values()]

        data = (json.dumps({"type": "userlist", "users": users}) + "\n").encode()
        
        for c in connections:    
            c.sendall(data)

    def handle_client(self, connection):
        username = None
        reader = connection.makefile("r", encoding="utf-8")
        
        for line in reader:                   
            user_info = json.loads(line)
            if user_info["type"] == "connect":
                username = user_info["username"]
                ip, port = user_info["ip"], user_info["port"]
                
                self.user_dict[username] = (ip, port, connection)
                self.write_log(f"{ip} {port} connected")
                self.send_userlist()

        if username is not None:
            ip, port, _ = self.user_dict.pop(username)
            self.write_log(f"{ip} {port} disconnected")
            self.send_userlist()
        
        connection.close()
        
        

    def listener(self):
        self.listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener_socket.bind((self.server_ip, self.server_port))
        self.listener_socket.listen()

        self.server_ip, self.server_port = self.listener_socket.getsockname()
        print(self.server_ip, self.server_port)

        while True:
            connection, accept = self.listener_socket.accept()
            threading.Thread(target=self.handle_client, args=(connection,), daemon=True).start()
        

if __name__ == "__main__":
    if len(sys.argv) == 2:
        Server(sys.argv[1]).listener()
    else:
        print("Usage: python server.py <server-port-number>")