import datetime
import select
import socket
import logging
import enum
import json
import re
import threading
import errno
import time
import os


PORT = 12345

HELLO_MESSAGE = {
    "type": "hello",
    "myname": None
}

AS_MESSAGE = {
    "type": "aleykumselam",
    "myname": None
}

MESSAGE = {
    "type": "message",
    "content": None
}

FILE_MESSAGE = {
    "type": 4,
    "name": None,
    "seq": None,
    "body":None
}

ACK_MESSAGE = {
    "type": 5,
    "name": None,
    "seq": None,
    "rwnd":None
}


IP_PATTERN = r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'
BROADCAST_PERIOD = 60
PRUNING_PERIOD = 120
BATCH_SIZE = 1500 # bytes
RWND = 10
PACKET_TIMEOUT = 1
# TODO :Find a way of closing the self-listener thread.


class MessageType(enum.Enum):
    hello = 1
    aleykumselam = 2
    message = 3
    file = 4
    ack = 5




class SendCtx: 
    """
        Context manager for sending a file over UDP.
    """
    def __init__(self, filepath, ip, batch_size = BATCH_SIZE, rwnd = 1):
        self.filepath = filepath
        self.ip = ip
        self.filesize = os.stat(filepath).st_size
        self.batch_size = batch_size

        self.packet_count = self.filesize // self.batch_size if self.filesize % self.batch_size == 0 else self.filesize // self.batch_size + 1
        self.seq = 1
        self.rwnd = rwnd
        self.packets = []
        self.acked = []
        self.on_fly = []

        with open(self.filepath, "rb") as file:
            # divide into batch size packets and store in self.packets
            while ( batch := file.read(self.batch_size) ):
                self.packets.append(batch)

    def build_message(self, seq):
        if seq == 0:
            message = FILE_MESSAGE.copy()
            message["name"] = self.filepath
            message["seq"] = seq
            message["body"] = self.packet_count
            return message
        
        message = FILE_MESSAGE.copy()
        message["name"] = self.filepath
        message["seq"] = seq
        message["body"] = self.packets[seq - 1]
        return message
    
    def get_next_message(self):
        if self.seq == self.packet_count - 1:
            return None

        self.on_fly.append((time.time(), self.seq))
        self.seq += 1
        return self.build_message(self.seq - 1)

    def is_complete(self):
        return self.seq == self.packet_count - 1
    
    def ack(self, seq):
        is_acked_before = False
        for s in self.acked:
            if s == seq:
                is_acked_before = True
                break
        if not is_acked_before:
            self.acked.append(seq)
        self.on_fly = [x for x in self.on_fly if x[1] != seq]
    
    def is_open_to_send(self):
        return len(self.on_fly) < self.rwnd and not self.is_complete()
    
class RecvCtx:
    """
        Context manager for receiving a file over UDP.
        This context has no on-fly packets, since ACKs are sent via TCP.
    """
    def __init__(self, filepath, ip, packet_count):
        self.filepath = filepath
        self.ip = ip

        self.packet_count = packet_count
        self.seq = 1

        self.packets = []
        self.acked = []

    def build_message(self, seq):
        message = ACK_MESSAGE.copy()
        message["name"] = self.filepath
        message["seq"] = seq
        message["rwnd"] = self.packet_count - len(self.acked)
        return message
    
    def get_next_message(self):
        if self.seq == self.packet_count - 1:
            return None

        self.seq += 1
        return self.build_message(self.seq - 1)

    def add_packet(self, packet):
        is_duplicate = False
        for p in self.packets:
            if p["seq"] == packet["seq"]:
                is_duplicate = True
                break
        if not is_duplicate:
            self.packets.append(packet)

    def is_complete(self):
        return self.seq == self.packet_count - 1

    def build_file(self):
        self.packets.sort(key=lambda x: x["seq"])
        self.write_to_file()
        return self.filepath
    
    def write_to_file(self):
        with open(self.filepath, "wb") as file:
            for packet in self.packets:
                file.write(packet["body"])

    def save(self):
        self.build_file()
        self.write_to_file()

    def write_to_file(self):
        with open(self.filepath, "wb") as file:
            for packet in self.packets:
                file.write(packet)

class Netchat:
    def __init__(self, name: str = None):
        logging.info("Finding out whoami.")
        self.terminate = False
        hostname: str = socket.gethostname()
        ip_addresses = socket.gethostbyname_ex(hostname)[-1]
        if len(ip_addresses) > 1:
            # TODO: lookup why this happens ?
            ipaddress: str = socket.gethostbyname_ex(hostname)[-1][1]
        else:
            ipaddress: str = socket.gethostbyname_ex(hostname)[-1][0]
        logging.info(f"Resolved whoami. IP:{ipaddress} \t Hostname:{hostname}")
        self.whoami: dict = dict()
        self.whoami["myname"] = name if name is not None else hostname
        self.whoami["ip"] = ipaddress

        self.peers: dict = {}
        self.listener_threads: dict = {}
        self.prune_list: list[str] = []

        self.send_ctxs : dict = {}
        self.recv_ctxs : dict  = {}

        self.listener_threads["BROADCAST"] = threading.Thread(
            target=self.listen_broadcast, daemon=True).start()
        self.listener_threads[self.whoami["ip"]] = threading.Thread(
            target=lambda: self.listen_peer(self.whoami["ip"]), daemon=True
        ).start()
        self.last_timestamp = time.time() - BROADCAST_PERIOD
        self.broadcast_thread = threading.Thread(
            target=self.broadcast, daemon=True).start()

        print(f"Discovery completed, ready to chat.")
        self.user_input_thread = threading.Thread(target=self.listen_user)
        self.user_input_thread.start()
        self.user_input_thread.join()

    def show_peers(self):
        print("IP:\t\tName:")
        for peer in self.peers:
            print(f"{peer}\t{self.peers[peer][1]}")

    def get_ip_by_name(self, name: str):
        for peer in self.peers.keys():
            if self.peers[peer][1] == name:
                return peer
        return None

    def shutdown(self):
        logging.info("Terminating...")
        self.terminate = True
        for ip in self.listener_threads.keys():
            if ip not in [self.whoami["ip"], "BROADCAST"]:
                self.listener_threads[ip].join()
                logging.info(f"{ip} listener closed.")

    def listen_user(self):
        while True and not self.terminate:
            line = input()
            if line == ":whoami":
                print(f'IP:{self.whoami["ip"]}\tName:{self.whoami["myname"]}')

            if line == ":quit":
                self.shutdown()

            if line == ":peers":
                self.show_peers()

            if line.startswith(":hello"):
                try:
                    name = line.split()[1]
                    ip = name.strip()
                    match = re.match(IP_PATTERN, ip)
                    if match and self.whoami["ip"] != ip:
                        hello_message = HELLO_MESSAGE.copy()
                        hello_message["myname"] = self.whoami["myname"]
                        self.send_message(ip, MessageType.hello)
                    else:
                        logging.warn("Incorrect IP string.")
                except BaseException:
                    print("Invalid command. Usage: :hello ip")
            if line.startswith(":send_file"):
                try:
                    # strip command from the second empty spaace and keep the
                    # rest as content
                    name, filename = line.split(
                        " ", 2)[1], line.split(
                        " ", 2)[2]
                    name = name.strip()
                    filename = filename.strip()
                    ip = self.get_ip_by_name(name)
                    if ip is None:
                        print(f"Peer with name \"{name}\" not found.")
                    else:
                        if ip not in self.send_ctxs:
                            self.send_ctxs[ip] = {}
                        self.send_ctxs[ip][filename] = SendCtx(filename, ip)
                        message = self.send_ctxs[ip][filename].build_message(0)
                        self.send_message(
                            ip, 
                            MessageType.file, 
                            content=message, 
                            override=True,
                            protocol=socket.SOCK_DGRAM
                            )
                except BaseException as e:
                    # TODO: Add handling
                    print(e)

            if line.startswith(":send"):
                try:
                    # strip command from the second empty spaace and keep the
                    # rest as content
                    name, content = line.split(
                        " ", 2)[1], line.split(
                        " ", 2)[2]
                    name = name.strip()
                    content = content.strip()
                    ip = self.get_ip_by_name(name)
                    if ip is None:
                        print(f"Peer with name \"{name}\" not found.")
                    else:
                        self.send_message(
                            ip, MessageType.message, content=content)
                except BaseException as e:
                    print("Invalid command. Usage: :send name message")

    def broadcast(self, port: int = PORT) -> dict:
        while True:
            if time.time() - self.last_timestamp > BROADCAST_PERIOD:
                logging.info("Broadcasting...")
                self.last_timestamp = time.time()
                hello_message = HELLO_MESSAGE.copy()
                hello_message['myname'] = self.whoami["myname"]
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.bind(('', 0))
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(json.dumps(hello_message).encode(
                        'utf-8'), ('<broadcast>', port))
                logging.info("Done.")
                
    def file_transfer_daemon(self):
        while True and not self.terminate:
            for ip in self.send_ctxs:
                for filename in self.send_ctxs[ip]:
                    ctx = self.send_ctxs[ip][filename]
                    if not ctx.is_complete():
                        for packet in ctx.on_fly:
                            if packet[0] + PACKET_TIMEOUT < time.time():
                                self.send_message(ip, MessageType.file_transfer, content=packet[1], override=True, protocol=socket.SOCK_DGRAM)
                        if ctx.is_open_to_send():
                            message = ctx.get_next_message()
                            self.send_message(ip, MessageType.file_transfer, content=message, override=True, protocol=socket.SOCK_DGRAM)
                    else:
                        del self.send_ctxs[ip][filename]
                        print(f"File {filename} sent to {ip}.")

    def send_message(self, 
                     ip: str, 
                     type: MessageType,
                     content = None, 
                     override: bool = False,
                     port: int = PORT,
                    protocol = socket.SOCK_STREAM) -> None:
        # TODO: Refactor this function
        if protocol == socket.SOCK_STREAM:
            try:
                logging.info("Creating a socket")
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    logging.info(f"Connecting to the {ip} on {port}")
                    s.connect((ip, port))

                    logging.info("Preparing the message.")
                    if type == MessageType.hello:
                        message = HELLO_MESSAGE.copy()
                        message["myname"] = self.whoami["myname"]
                    if type == MessageType.aleykumselam:
                        message = AS_MESSAGE.copy()
                        message["myname"] = self.whoami["myname"]
                    if type == MessageType.message:
                        message = MESSAGE.copy()
                        message["content"] = content

                    encode = json.dumps(message).encode('utf-8')
                    s.sendall(encode)
                    logging.info("Sent the message")
                    s.close()
                    logging.info(f"Closed the connection on {ip}")
            except Exception as e:
                logging.error(f"Error while sending the message. Reason: {e}")
        elif protocol == socket.SOCK_DGRAM:
            try:
                logging.info("Creating a socket")
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    logging.info(f"Connecting to the {ip} on {port}")

                    logging.info("Preparing the message.")
                    if message["type"] == FILE_MESSAGE["type"] and override:
                        # only message that should go through UDP
                        message = content
                    encode = json.dumps(message).encode('utf-8')
                    s.sendto(encode, (ip, port))
                    logging.info("Sent the message")
                    s.close()
                    logging.info(f"Closed the connection on {ip}")
            except Exception as e:
                logging.error(f"Error while sending the message. Reason: {e}")

    def process_message(self, data: str, ip: str):
        data = json.loads(data)
        try:
            if data["type"] == HELLO_MESSAGE["type"] and ip != self.whoami["ip"]:
                logging.info(f"{ip} reached to say 'hello'")
                self.peers[ip] = (time.time(), data["myname"])
                self.listener_threads[ip] = [
                    threading.Thread(target=lambda: self.listen_peer(ip)),
                    threading.Thread(target=lambda: self.listen_peer(ip, protocol=socket.SOCK_DGRAM))
                ]
                self.listener_threads[ip][0].start()
                self.listener_threads[ip][1].start()
                logging.info(f"Sending 'aleykumselam' to {ip}")
                self.send_message(ip, MessageType.aleykumselam)

            if data["type"] == AS_MESSAGE["type"]:
                logging.info(f"{ip} said 'aleykumselam'")
                self.peers[ip] = (time.time(), data["myname"])

            if data["type"] == MESSAGE["type"]:
                logging.info(
                    f"Processing message from {self.peers[ip]}({ip})")
                _content = data['content']
                _from = 'UNKNOWN_HOST' if ip not in self.peers.keys(
                ) else self.peers[ip][1]
                print(
                    f"[{datetime.datetime.now()}] FROM: {_from}({ip}): {_content}")
                
            if data["type"] == ACK_MESSAGE["type"]:
                logging.info(
                    f"Processing ACK from {self.peers[ip]}({ip})")
                _sender = 'UNKNOWN_HOST' if ip not in self.peers.keys(
                ) else self.peers[ip][1]
                send_ctx = self.send_ctxs[ip][data["name"]]
                send_ctx.rwnd = int(data["rwnd"])
                if send_ctx.is_complete():
                    self.send_ctxs[ip][data["name"]] = None
                    logging.info("File sent.")
                else:
                    send_ctx.ack(int(data["seq"]))
                    logging.info(f"FROM:{_sender}(ACK[{data['seq']}]): <{data['name']} received.>")
                    

            if data["type"] == FILE_MESSAGE["type"]:
                logging.info(
                    f"Processing file from {self.peers[ip]}({ip})")
                _sender = 'UNKNOWN_HOST' if ip not in self.peers.keys(
                ) else self.peers[ip][1]

                if data["name"] not in self.recv_ctxs[ip].keys():
                    self.recv_ctxs[ip][data["name"]] = RecvCtx(data["name"], ip)
                if data["seq"] == 0:
                    self.recv_ctxs[ip][data["name"]].packet_count = int(data["content"])
                else:
                    recv_ctx = self.recv_ctxs[ip][data["name"]]
                    recv_ctx.add_packet(data)
                    if recv_ctx.is_complete():
                        logging.info(f"FROM:{_sender}(file): <{data['name']} received.>")
                        recv_ctx.save()
                        self.recv_ctxs[ip][data["name"]] = None

        except KeyError as e:
            logging.error(
                f"Incoming message with unexpected structure. Message: {data}")
        except Exception as e:
            logging.error(f"Unexpected error. Check the exception: {e}")

    def listen_peer(self, ip: str, protocol = socket.SOCKET_STREAM, port: int = PORT):
        if protocol == socket.SOCK_STREAM:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind((ip, port))
                    s.listen()
                    while True and not self.terminate:
                        conn, addr = s.accept()
                        addr = addr[0]
                        with conn:
                            while True:
                                data = conn.recv(1024)
                                if not data:
                                    break
                                data = data.decode('utf-8')
                                self.process_message(data, addr)
                    if self.terminate:
                        logging.info(f"Closed the connection on {ip}")
                        s.close()
                except socket.error as e:
                    if e.errno == errno.EADDRNOTAVAIL:
                        logging.info(f"Host not available")
                    if e.errno == errno.ECONNREFUSED or 'Connection refused' in str(
                            e):
                        logging.info(f"Host refused to connect")
        elif protocol == socket.SOCK_DGRAM:
            while True and not self.terminate:
                buffer_size = 1024
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.bind((ip, port))
                    s.setblocking(0)
                    result = select.select([s], [], [])
                    msg, sender = result[0][0].recvfrom(buffer_size)
                    sender = sender[0]
                    self.process_message(msg, sender)

    def listen_broadcast(self, port=PORT):
        while True and not self.terminate:
            buffer_size = 1024
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.bind(('', port))
                s.setblocking(0)
                result = select.select([s], [], [])
                msg, sender = result[0][0].recvfrom(buffer_size)
                sender = sender[0]
                self.process_message(msg, sender)

                for peer in list(self.peers.keys()):
                    if time.time() - self.peers[peer][0] > PRUNING_PERIOD:
                        logging.info(
                            f"Pruning peer due to inactivity: {self.peers[peer][1]}({peer})")
                        self.peers.pop(peer)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    netchat = Netchat("Deniz")
