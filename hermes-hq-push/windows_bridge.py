"""Authenticated, account-scoped Windows transport for the HQ prompt bridge.

Only JSON bytes cross the pipe; multiprocessing pickle deserialization is never used.
The shared authentication key is protected with current-user Windows DPAPI.
"""
import ctypes
from ctypes import wintypes
import hashlib
import io
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client, Listener, answer_challenge, deliver_challenge


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _protect(data, decrypt=False):
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    buf = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def _key(path, create=False):
    path = Path(str(path) + ".dpapi")
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".hq-pipe-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(_protect(secrets.token_bytes(32)))
            try:
                os.link(temporary, path)  # publish complete contents, without replacing another owner
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    key = _protect(path.read_bytes(), decrypt=True)
    if len(key) != 32:
        raise OSError("Invalid HQ pipe authentication key")
    return key


def _address(path):
    identity = os.path.normcase(str(Path(path).resolve())).encode("utf-8")
    return "\\\\.\\pipe\\hermes-hq-" + hashlib.sha256(identity).hexdigest()[:32]


class _Handshake:
    def __init__(self, connection, timeout):
        self.connection = connection
        self.deadline = time.monotonic() + timeout

    def send_bytes(self, data):
        self.connection.send_bytes(data)

    def recv_bytes(self, maxlength):
        if not self.connection.poll(max(0, self.deadline - time.monotonic())):
            raise socket.timeout("HQ pipe authentication timed out")
        return self.connection.recv_bytes(maxlength)


def _authenticate(connection, key, timeout, server=False):
    handshake = _Handshake(connection, timeout)
    if server:
        deliver_challenge(handshake, key)
        answer_challenge(handshake, key)
    else:
        answer_challenge(handshake, key)
        deliver_challenge(handshake, key)


class _Reader(io.RawIOBase):
    def __init__(self, stream):
        self.stream = stream

    def readable(self):
        return True

    def readinto(self, buffer):
        data = self.stream.recv(len(buffer))
        buffer[:len(data)] = data
        return len(data)


class PipeSocket:
    def __init__(self, connection):
        self.connection = connection
        self.timeout = None
        self.buffer = b""
        self.ended = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.connection.send_bytes(data)

    def recv(self, size):
        if not self.buffer and not self.ended:
            if not self.connection.poll(self.timeout):
                raise socket.timeout("HQ pipe timed out")
            try:
                self.buffer = self.connection.recv_bytes(1 << 20)
            except EOFError:
                self.ended = True
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def peer_closed(self):
        try:
            if not self.ended and not self.buffer and self.connection.poll(0):
                self.buffer = self.connection.recv_bytes(1 << 20)
        except (EOFError, OSError):
            # Windows may report closure during poll(), before recv_bytes().
            self.ended = True
        return self.ended

    def makefile(self, mode, buffering=-1):
        return io.BufferedReader(_Reader(self))

    def shutdown(self, how):
        self.close()

    def close(self):
        self.ended = True
        self.connection.close()


def connect(path, timeout):
    key = _key(path)
    connection = Client(_address(path), family="AF_PIPE", authkey=None)
    try:
        _authenticate(connection, key, timeout)
    except (AuthenticationError, EOFError, OSError) as error:
        connection.close()
        raise OSError("HQ pipe authentication failed") from error
    stream = PipeSocket(connection)
    stream.settimeout(timeout)
    return stream


class Server:
    def __init__(self, path, handler):
        self.path = Path(path)
        self.address = _address(path)
        self.handler = handler
        self.stopping = threading.Event()
        self.key = _key(path, create=True)
        self.listener = Listener(self.address, family="AF_PIPE", authkey=None)

    def _handle(self, connection):
        with PipeSocket(connection) as stream:
            stream.settimeout(5)
            try:
                _authenticate(connection, self.key, 5, server=True)
                self.handler(stream, None, self)
            except (AuthenticationError, EOFError, OSError):
                pass  # a disconnected gateway withdraws its own request

    def serve_forever(self):
        while True:
            try:
                connection = self.listener.accept()
            except (AuthenticationError, OSError):
                if self.stopping.is_set():
                    break
                continue
            if self.stopping.is_set():
                connection.close()
                break
            threading.Thread(target=self._handle, args=(connection,), daemon=True).start()

    def shutdown(self):
        if self.stopping.is_set():
            return
        self.stopping.set()
        try:
            connection = Client(self.address, family="AF_PIPE", authkey=None)
            connection.close()  # wake accept so the listener can exit
        except OSError:
            pass

    def server_close(self):
        self.listener.close()
