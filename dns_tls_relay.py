#!/usr/bin/env python3

import os, sys
import subprocess
import traceback
import time
import threading
import json
import random
import ssl

from collections import deque
from socket import socket, timeout, AF_INET, SOCK_DGRAM, SOCK_STREAM, SHUT_WR

from dns_packet_parser import PacketManipulation

LISTENING_ADDRESS = '192.168.2.250'

# must support DNS over TLS (not https/443, tcp/853)
PUBLIC_SERVER_1 = '1.1.1.1'
PUBLIC_SERVER_2 = '1.0.0.1'

# protocol constants
TCP = 6
UDP = 17
A_RECORD = 1
DNS_PORT = 53
DNS_TLS_PORT = 853


class DNSRelay:
    def __init__(self):
        self.tls_retry = 60

        self.unique_id_lock = threading.Lock()

        self.dns_connection_tracker = {}
        self.dns_tls_queue = deque()

        self.dns_servers = {}
        self.dns_servers[PUBLIC_SERVER_1] = {'reach': True, 'tls': True}
        self.dns_servers[PUBLIC_SERVER_2] = {'reach': True, 'tls': True}

    def Start(self):

        threading.Thread(target=self.TLSQueryQueue).start()
        self.Main()

    def Main(self):
        self.sock = socket(AF_INET, SOCK_DGRAM)
        self.sock.bind((LISTENING_ADDRESS, DNS_PORT))

        print(f'[+] Listening -> {LISTENING_ADDRESS}:{DNS_PORT}')
        while True:
            try:
                data_from_client, client_address = self.sock.recvfrom(1024)
#                print(f'Receved data from client: {client_address[0]}:{client_address[1]}.')
                if (not data_from_client):
                    break
                packet = PacketManipulation(data_from_client, protocol=UDP)
                packet.Parse()

                ## Matching IPV4 DNS queries only. All other will be dropped. Then creating a thread
                ## to handle the rest of the process and sending client data in for relay to dns server
                if (packet.qtype == 1):
                    self.TLSQueue(data_from_client, client_address)
                    time.sleep(.01)
            except Exception as E:
                print(f'MAIN: {E}')

        self.Main()

    def TLSQueue(self, data_from_client, client_address):
        packet = PacketManipulation(data_from_client, protocol=UDP)
        client_dns_id = packet.DNS()

        tcp_dns_id = self.GenerateIDandStore()
        dns_payload = packet.UDPtoTLS(tcp_dns_id)

        ## Adding client connection info to tracker to be used by response handler
        self.dns_connection_tracker.update({tcp_dns_id: {'client_id': client_dns_id, 'client_address': client_address}})

        self.dns_tls_queue.append(dns_payload)

    ## Queue Handler will make a TLS connection to remote dns server/ start a response handler thread and send all requests
    # in queue over the connection.
    def TLSQueryQueue(self):
        while True:
            try:
                secure_socket = None
                msg_queue = self.dns_tls_queue.copy()
                if (msg_queue):
                    for secure_server, server_info in self.dns_servers.items():
                        now = time.time()
                        retry = now - server_info.get('retry', now)
                        if (server_info['tls'] or retry >= self.tls_retry):
                            secure_socket = self.Connect(secure_server)
                        if (secure_socket):
                            break

                if (secure_socket):
                    threading.Thread(target=self.TLSResponseHandler, args=(secure_socket,)).start()
                    # prevents double free and ssllib crashes/ python segmentation faults
                    time.sleep(.001)
                    for message in msg_queue:
                        try:
                            secure_socket.send(message)

                        except Exception as E:
                            print(f'TLSQUEUE | SEND: {E}')

                        self.dns_tls_queue.popleft()

                    secure_socket.shutdown(SHUT_WR)
                # waiting 10ms before checking queue again, this will make idle performance lower.
                time.sleep(.01)
            except Exception as E:
                print(f'TLSQUEUE | GENERAL: {E}')

    # Response Handler will match all recieved request responses from the server, match it to the host connection
    # and relay it back to the correct host/port. this will happen as they are recieved. the socket will be closed
    # once the recieved count matches the expected/sent count or from socket timeout
    def TLSResponseHandler(self, secure_socket):
        while True:
            try:
                dns_query_response = None
                data_from_server = secure_socket.recv(4096)
                if (not data_from_server):
                    break

            except (timeout, BlockingIOError):
                break
            except Exception:
                traceback.print_exc()
                break

            try:
                # Checking the DNS ID in packet, Adjusted to ensure uniqueness
                packet = PacketManipulation(data_from_server, protocol=TCP)
                tcp_dns_id = packet.DNS()
#                print(f'Secure Request Received from Server. DNS ID: {tcp_dns_id}')

                # Checking client DNS ID and Address info to relay query back to host
                dns_query_info = self.dns_connection_tracker.get(tcp_dns_id, None)
                if (dns_query_info):
                    client_dns_id = dns_query_info.get('client_id')
                    client_address = dns_query_info.get('client_address')

                    ## Parsing packet and rewriting TTL to 5 minutes and changing DNS ID back to original.
                    packet.Rewrite(dns_id=client_dns_id)
                    dns_query_response = packet.send_data

                    ## Relaying packet from server back to host
                    self.sock.sendto(dns_query_response, client_address)
#                    print(f'Request Relayed to {client_address[0]}: {client_address[1]}')
            except ValueError:
                # to troubleshoot empty separator error
                print('empty separator error')
                print(data_from_server)
            except Exception:
                traceback.print_exc()

            self.dns_connection_tracker.pop(tcp_dns_id, None)

        secure_socket.close()

    # Aquire ID Lock, then generates a random number until a unique number is found. Once found
    # it will be stored and used for the external TLS query to ensure all requests are unique
    def GenerateIDandStore(self):
        with self.unique_id_lock:
            while True:
                dns_id = random.randint(1, 32000)
                if (dns_id not in self.dns_connection_tracker):

                    self.dns_connection_tracker.update({dns_id: ''})

                    return dns_id

    # Connect will retry 3 times if issues, then mark TLS server as inactive and timestamp
    # timestamp will be used to re attempt to connect after retry limit exceeded in message
    # queue handler method
    def Connect(self, secure_server):
        now = round(time.time())
        try:
            sock = socket(AF_INET, SOCK_STREAM)
            sock.bind((LISTENING_ADDRESS, 0))

            context = ssl.create_default_context()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_verify_locations('/etc/ssl/certs/ca-certificates.crt')

            # Wrap socket and Connect. If exception will return None which will have
            # the queue handler try the other server if available and will mark this
            # server as down

            secure_socket = context.wrap_socket(sock, server_hostname=secure_server)
            secure_socket.connect((secure_server, DNS_TLS_PORT))
        except Exception:
            secure_socket = None

        if (secure_socket):
            self.dns_servers[secure_server].update({'tls': True})
        else:
            self.dns_servers[secure_server].update({'tls': False, 'retry': now})

        return secure_socket
