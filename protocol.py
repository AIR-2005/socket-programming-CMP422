"""Send JSON packets over TCP with a four-byte payload length prefix."""

import json
import socket
import struct


HEADER = struct.Struct("!I")  # Four-byte unsigned integer, network byte order.
MAX_PACKET_SIZE = 1024 * 1024  # Control packets only; files will use a separate flow.


def receive_exact(connection_socket: socket.socket, byte_count: int) -> bytes | None:
    """Collect exactly byte_count bytes; None means EOF before any bytes arrived."""
    received_data = bytearray()

    while len(received_data) < byte_count:
        chunk = connection_socket.recv(byte_count - len(received_data))
        if not chunk:
            if received_data:
                raise ConnectionError("Connection closed during a packet.")
            return None
        received_data.extend(chunk)

    return bytes(received_data)


def send_packet(connection_socket: socket.socket, packet: dict) -> None:
    """Encode a packet, prefix its byte length, then send the complete frame."""
    payload = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    if not 0 < len(payload) <= MAX_PACKET_SIZE:
        raise ValueError("Packet size is outside the allowed range.")

    connection_socket.sendall(HEADER.pack(len(payload)) + payload)


def receive_packet(connection_socket: socket.socket) -> dict | None:
    """Read one JSON packet, even when TCP splits or combines transmissions."""
    header = receive_exact(connection_socket, HEADER.size)
    if header is None:
        return None

    payload_size = HEADER.unpack(header)[0]
    if not 0 < payload_size <= MAX_PACKET_SIZE:
        raise ValueError("Packet size is outside the allowed range.")

    payload = receive_exact(connection_socket, payload_size)
    if payload is None:
        raise ConnectionError("Connection closed before the packet payload.")

    packet = json.loads(payload.decode("utf-8"))
    if not isinstance(packet, dict):
        raise ValueError("A packet must be a JSON object.")
    return packet
