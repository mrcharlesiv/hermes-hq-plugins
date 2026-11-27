"""Windows prompt bridge regressions. Stdlib only; no real Hermes or phone calls."""
import importlib.util
import os
from pathlib import Path
import json
import socketserver
import tempfile
import threading
import unittest
from multiprocessing import AuthenticationError
from multiprocessing.connection import Client


class EchoApp(socketserver.StreamRequestHandler):
    def handle(self):
        data = self.rfile.readline()
        if data:
            self.wfile.write(data)


class PipeClosureSignals(unittest.TestCase):
    def test_polling_a_broken_pipe_counts_as_disconnect(self):
        import windows_bridge

        class ClosedPipe:
            def poll(self, timeout):
                raise BrokenPipeError("The other end has closed")

        self.assertTrue(windows_bridge.PipeSocket(ClosedPipe()).peer_closed())

    def test_polling_eof_counts_as_disconnect(self):
        import windows_bridge

        class ClosedPipe:
            def poll(self, timeout):
                raise EOFError

        self.assertTrue(windows_bridge.PipeSocket(ClosedPipe()).peer_closed())

    def test_disconnect_probe_preserves_unread_bytes(self):
        import windows_bridge

        class PendingPipe:
            def poll(self, timeout):
                return True

            def recv_bytes(self, limit):
                return b"pending answer\n"

        stream = windows_bridge.PipeSocket(PendingPipe())
        self.assertFalse(stream.peer_closed())
        self.assertEqual(stream.recv(100), b"pending answer\n")


class PipeListener:
    def __init__(self, path, handler=EchoApp):
        import windows_bridge
        self.server = windows_bridge.Server(path, handler)

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self.server

    def __exit__(self, *args):
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        if self.thread.is_alive():
            raise AssertionError("Windows prompt listener did not stop")


@unittest.skipIf(os.name == "nt", "Unix socket compatibility")
class UnixGatewayBridge(unittest.TestCase):
    def test_gateway_code_request_still_receives_the_apps_answer(self):
        gateway = load_gateway()

        class App(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                if request["method"] == "vault.code":
                    self.wfile.write(b'{"value":"123456"}\n')

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        # macOS Unix socket paths must fit in 104 bytes.
        with tempfile.TemporaryDirectory(prefix="hq-", dir="/tmp") as folder:
            path = Path(folder) / "logins.sock"
            with Server(str(path), App) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    result = gateway.ask(path, "vault.code", {"site": "example.test", "hint": "code"},
                                         {"session_id": "test-session", "home": folder})
                    self.assertEqual(result, "123456")
                finally:
                    server.shutdown()
                    thread.join(3)
                    self.assertFalse(thread.is_alive())


def load_gateway():
    spec = importlib.util.spec_from_file_location(
        "hq_windows_gateway_test", Path(__file__).with_name("gateway_logins.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "nt", "Windows prompt bridge")
class WindowsGatewayBridge(unittest.TestCase):
    def test_gateway_prompt_module_loads_on_windows(self):
        gateway = load_gateway()
        self.assertTrue(callable(gateway.ask))
        self.assertTrue(callable(gateway.start_server))

    def test_gateway_code_request_receives_the_apps_answer(self):
        import windows_bridge
        gateway = load_gateway()

        class App(socketserver.StreamRequestHandler):
            def handle(self):
                request = json.loads(self.rfile.readline())
                if (request["method"] == "vault.code"
                        and request["params"] == {"site": "example.test", "hint": "login code"}):
                    self.wfile.write(b'{"value":"123456"}\n')

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logins.sock"
            server = windows_bridge.Server(path, App)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = gateway.ask(path, "vault.code", {"site": "example.test", "hint": "login code"},
                                     {"session_id": "test-session", "home": folder})
                self.assertEqual(result, "123456")
            finally:
                server.shutdown()
                thread.join(3)
                server.server_close()
                self.assertFalse(thread.is_alive())

    def test_unauthenticated_peer_is_rejected_without_stopping_the_listener(self):
        import windows_bridge
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logins.sock"
            with PipeListener(path) as server:
                with self.assertRaises(AuthenticationError):
                    Client(server.address, family="AF_PIPE", authkey=b"wrong-key")
                with windows_bridge.connect(path, 1) as connection:
                    connection.sendall(b"still available\n")
                    self.assertEqual(connection.recv(100), b"still available\n")

    def test_disconnect_can_withdraw_a_pending_prompt(self):
        import windows_bridge
        entered, disconnected = threading.Event(), threading.Event()

        class App(socketserver.StreamRequestHandler):
            def handle(self):
                self.rfile.readline()
                entered.set()
                for attempt in range(200):
                    if self.connection.peer_closed():
                        disconnected.set()
                        return
                    disconnected.wait(0.01)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logins.sock"
            with PipeListener(path, App):
                connection = windows_bridge.connect(path, 1)
                connection.sendall(b"pending prompt\n")
                self.assertTrue(entered.wait(2))
                connection.close()
                self.assertTrue(disconnected.wait(3))

    def test_a_peer_stalled_before_authentication_does_not_block_other_prompts(self):
        import windows_bridge
        completed = threading.Event()
        results = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logins.sock"
            with PipeListener(path) as server:
                stalled = Client(server.address, family="AF_PIPE", authkey=None)
                def prompt():
                    try:
                        with windows_bridge.connect(path, 1) as connection:
                            connection.sendall(b"another prompt\n")
                            results.append(connection.recv(100))
                    finally:
                        completed.set()
                thread = threading.Thread(target=prompt, daemon=True)
                thread.start()
                try:
                    self.assertTrue(completed.wait(2), "a stalled peer blocked a different prompt")
                    self.assertEqual(results, [b"another prompt\n"])
                finally:
                    stalled.close()
                    thread.join(3)

    def test_client_reconnects_after_the_listener_restarts(self):
        import windows_bridge
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "logins.sock"
            for restart in range(2):
                with PipeListener(path):
                    with windows_bridge.connect(path, 1) as connection:
                        connection.sendall(b"reconnected\n")
                        self.assertEqual(connection.recv(100), b"reconnected\n")


if __name__ == "__main__":
    unittest.main()
