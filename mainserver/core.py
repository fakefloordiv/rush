"""
Mainserver-core makes only 1 thing: receives parsed request from wrapper
and distributes it to a most unloaded handler from handlers group to a
which one request belongs

class MainServerCore:
    __init__(response_callback, addr=('localhost', 1111), block_size=4096):
        - response_callback: callable object, that receives 1 argument
                             (see 'Response Callback Callable' part)
        - addr: local address on which handlers will be served
        - block_size: how much bytes per time will be received
                      from handler's socket (see part 'Handlers
                      Messages Delivering Protocol')
    send_request(request: dict):
        - request: request serialized to python's dict (for http
                   requests should be parsed to dict, too)

        This function just pushing request to worker-thread (see 'Requests
        Thread' part), so it's call is almost free
    start(threaded=False):
        - threaded: if set to True, function will return nothing,
                    but thread with this function will be started.

        Initialize and run mainserver-core. This function is blocking,
        it means, that mainserver-core will run until this function
        is running

part 'Response Callback Callable':
    Any callable object that receives 2 arguments: `response_to` and `response`.
    `response_to` may be any string, even integer or hash. It's problem of wrapper
    that will response to client this response belongs to. `response` is also any
    object, that should be specified with wrapper (it may be dict, that will be
    serialized to http again, or even directly http to avoid waste of resources).
    May be blocking, because will be called in a parallel thread (see 'Responses
    Thread' part)

part 'Handlers Messages Delivering Protocol':
    Server Side:
        Handlers can send to server only 2 types of packets:
            - heartbeat-packet with local machine's load (b'\x01')
            - response (b'\x00')

        Packets format:
            - 1 byte - type of request:
                - \x69 (heartbeat):
                    - 1 byte - integer that means machine's cpu load
                - \x42 (response):
                    - 4 bytes - length of the response
                    - 1-4294967295 bytes (4.2GB per response is current maximal response size,
                                          may be increased in future as a flag)

        When server receives RECEIVE event from epoll, it is reading
        blocks from socket by n bytes (see 'class MainServerCore.__init__:block_size'
        part)
    Client Side:
        Packets:
            - heartbeat: handler sends 2 bytes: b'\x01' (varconstant:HEARTBEAT) and
                         it's machine's load
                         in percent (uint integer)
            - response: client sends b'\x00' (varconstant:RESPONSE) and usual packet
                        in format lib.msgproto.fmt_packet

part 'Handler Initialization Handshake':
    After client's connecting, he should send bytes: b'\x69\x04\x02\x00',
    receive this bytes but reversed, send \x01, receive mainserver's name
    (using lib.msgproto), and send it's filter (to be associated with one
    of the virtual groups)

    Steps:
        (C)lient, (S)erver:
            C: b'\x69\x04\x02\x00'
            S: b'\x69\x04\x02\x00'[::-`]
            C: b'\x01'
            S: it's name (using lib.msgproto)
            C: it's filter (using lib.msgproto)

    Yes, this is kinda epollserver.handshake, but a bit modified

part 'Responses Thread' (currently unused, may be implemented):
    Worker-thread that checking shared list with responses. If not
    empty - getting response_to and sends client it's response

part 'Requests Thread' (currently unused, may be implemented):
    Worker-thread that checking shared list with requests. If not
    empty - thread is looking for group that belongs to request,
    and the most unloaded handler and sends request to it

part 'How Will Maximal Response Size Increased':
    If required, maximal response size may be fixed by changing
    protocol by this way:
        - 1 byte - bytes of size of packet (uint, 255 is maximal value)
        - n bytes - length of message (max packet size by this way is
                    idk how much, but a lot as fuck)
        - a lot as fuck (up to 2^2000TB) bytes - body
"""

from json import dumps
from socket import socket
from typing import Dict, List
from select import EPOLLIN, EPOLLOUT, EPOLLHUP

from lib import epollserver
from mainserver.entities import (Filter, Handler, HandlerInitializer,
                                 INITIAL_BYTES_SEQUENCE)

RESPONSE = 0
HEARTBEAT = 1


class CoreServer:
    def __init__(self, callback, name='mainserver', addr=('localhost', 10000),
                 receive_block_size=4096, response_block_size=4096):
        self.callback = callback
        self.name = name
        self.receive_block_size = receive_block_size
        self.response_block_size = response_block_size

        self.requests = {}   # conn: [msg_len_received, left_to_receive, received]
        self.responses = {}  # conn: [requests]
        self.responses_queue = {}
        self.waiting_for_init = {}  # conn: initializer
        self.handlers: Dict[socket, Handler] = {}  # conn: handler-entity

        self.virtual_groups: Dict[Filter, List[Handler]] = {}    # filter: [conns]

        self.epoll_server = epollserver.EpollServer(addr)
        self.epoll_server.add_handler(self.conn_handler, epollserver.CONNECT)
        self.epoll_server.add_handler(self.requests_handler, epollserver.RECEIVE)
        self.epoll_server.add_handler(self.disconn_handler, epollserver.DISCONNECT)

    def conn_handler(self, _, conn):
        ip, port = conn.getpeername()
        print(f'[MAINSERVER-CORE] New connection: {ip}:{port}')

        handler_entity = Handler(conn, None)
        self.handlers[conn] = handler_entity

        handler_initializer = HandlerInitializer(conn)
        self.waiting_for_init[conn] = handler_initializer

    def disconn_handler(self, _, conn):
        if conn in self.waiting_for_init:
            self.waiting_for_init.pop(conn)
        if conn in self.requests:
            self.requests.pop(conn)
        if conn in self.responses:
            self.responses.pop(conn)

        conn.close()

        ip, port = self.handlers.pop(conn).get_addr()
        print(f'[MAINSERVER-CORE] Disconnected: {ip}:{port}')

    def requests_handler(self, _, conn):
        """
        Initializing/receiving responses/heartbeat packets from handlers
        """

        if conn not in self.requests:
            future_msg_len = conn.recv(4)

            if len(future_msg_len) == 4:
                self.requests[conn] = [True, int.from_bytes(future_msg_len, 'little'), b'']
            else:
                self.requests[conn] = [False, 4 - len(future_msg_len), future_msg_len]
        else:
            request_cell = self.requests[conn]
            left_to_receive = request_cell[1]

            if not request_cell[0]:  # msg len haven't been received fully yet
                # you may ask me, why did I do such a work for simple 4 bytes receiving
                # If I won't do this, there is a possibility that some asshole
                # with slow internet will send me a byte per second, and this can raise
                # an UB
                left_to_receive = request_cell[1]
                received = conn.recv(left_to_receive)

                if left_to_receive - len(received) <= 0:
                    self.requests[conn] = [True, int.from_bytes(request_cell[2], 'little') +
                                           received, b'']
                else:
                    request_cell[2] += received
                    request_cell[1] -= len(received)
            else:
                request = conn.recv(left_to_receive if left_to_receive <= self.receive_block_size
                                    else self.receive_block_size)
                request_cell[2] += request
                request_cell[1] -= len(request)

                if request_cell[1] <= 0:
                    self.response(conn, request_cell[2])
                    self.requests.pop(conn)

    def response_handler(self, _, conn):
        """
        Sending requests to the handlers
        """

        block = self.requests[conn][0][:self.response_block_size]
        conn.send(block)

        if len(block) < self.response_block_size:
            self.requests[conn].pop(0)

            if not self.responses[conn]:
                self.responses.pop(conn)

    def response(self, conn, response_body):
        handler_entity = self.handlers[conn]

        if conn in self.waiting_for_init:
            initializer = self.waiting_for_init[conn]
            response = initializer.next_step(self, response_body)

            if not isinstance(response, bool):
                # received filter, initialization finished
                print('[MAINSERVER-CORE] Successfully initialized handler '
                      f'{handler_entity.ip}:{handler_entity.port}')
                self.waiting_for_init.pop(conn)

                filter_ = Filter(response)

                if filter_ not in self.virtual_groups:
                    self.virtual_groups[filter_] = [handler_entity]
                else:
                    self.virtual_groups[filter_].append(handler_entity)

                handler_entity.set_filter(filter_)
            elif not response:
                print('[MAINSERVER-CORE] Handshake failure with handler '
                      f'{handler_entity.ip}:{handler_entity.port}')
                conn.close()
        elif response_body[0] == HEARTBEAT:
            handler_entity.set_load(response_body[1])
        else:
            self.callback(handler_entity, response_body[1:])

    # USER API STARTS HERE

    def send_update(self, request: list):
        """
        Request: a list that contains 2 objects:
            response_to - any unique id for each client
            body - request body
        """

        for virtual_group_filter, handlers in self.virtual_groups.items():
            if virtual_group_filter(request):
                handler = min(handlers, key=lambda _handler: _handler.load)
                handler.send(dumps(request).encode())

    def start(self, threaded=True):
        ip, port = self.epoll_server.server_sock.getsockname()

        if not threaded:
            # if not threaded - server will shutdown before last print
            # but if threaded, we just call it and printing log entry
            # right below
            print(f'[MAINSERVER-CORE] Serving on {ip}:{port}')

        self.epoll_server.start(conn_signals=(EPOLLIN | EPOLLOUT | EPOLLHUP),
                                threaded=threaded)
        print(f'[MAINSERVER-CORE] Serving on {ip}:{port}')
