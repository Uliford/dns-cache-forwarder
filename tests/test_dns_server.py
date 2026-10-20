"""
End-to-end tests: the real server runs as a subprocess and forwards to a fake
upstream resolver on localhost, so the tests need no Internet access.

Run with:  python -m unittest discover -s tests -v
"""

import os
import socket
import subprocess
import sys
import threading
import time
import unittest

import dns.message
import dns.rcode
import dns.rrset

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, 'dns_server.py')
TTL = 2  # short TTL so the expiry test is quick


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class FakeUpstream(threading.Thread):
    """Answers A queries with a fixed IP, NXDOMAIN for nope.*, and stays silent for slow.*."""

    def __init__(self, port):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', port))
        self.hits = 0

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                return
            self.hits += 1
            query = dns.message.from_wire(data)
            name = query.question[0].name.to_text()
            if name.startswith('slow'):
                continue
            response = dns.message.make_response(query)
            if name.startswith('nope'):
                response.set_rcode(dns.rcode.NXDOMAIN)
            else:
                response.answer.append(dns.rrset.from_text(name, TTL, 'IN', 'A', '192.0.2.10'))
            self.sock.sendto(response.to_wire(), addr)


class DNSServerTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.port = free_udp_port()
        upstream_port = free_udp_port()
        cls.upstream = FakeUpstream(upstream_port)
        cls.upstream.start()
        cls.proc = subprocess.Popen(
            [sys.executable, SERVER, str(cls.port), '--upstream', '127.0.0.1',
             '--upstream-port', str(upstream_port), '--timeout', '1'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.upstream.sock.close()

    def ask(self, name, timeout=3):
        query = dns.message.make_query(name, 'A')
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.sendto(query.to_wire(), ('127.0.0.1', self.port))
            response = dns.message.from_wire(s.recvfrom(4096)[0])
        self.assertEqual(response.id, query.id, 'response must carry the ID of the query')
        return response

    def test_1_miss_then_hit(self):
        before = self.upstream.hits
        first = self.ask('example.test')
        second = self.ask('EXAMPLE.test')  # names are case-insensitive
        self.assertEqual(first.answer[0][0].to_text(), '192.0.2.10')
        self.assertEqual(second.answer[0][0].to_text(), '192.0.2.10')
        self.assertEqual(self.upstream.hits - before, 1, 'second query must come from the cache')
        self.assertLessEqual(second.answer[0].ttl, TTL)

    def test_2_expiry(self):
        self.ask('expire.test')
        before = self.upstream.hits
        time.sleep(TTL + 0.5)
        self.ask('expire.test')
        self.assertEqual(self.upstream.hits - before, 1, 'expired entry must be fetched again')

    def test_3_nxdomain_passed_through(self):
        self.assertEqual(self.ask('nope.test').rcode(), dns.rcode.NXDOMAIN)

    def test_4_servfail_on_timeout(self):
        self.assertEqual(self.ask('slow.test').rcode(), dns.rcode.SERVFAIL)

    def test_5_survives_malformed_packets(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b'hello', ('127.0.0.1', self.port))
            s.sendto(os.urandom(40), ('127.0.0.1', self.port))
        time.sleep(0.2)
        self.assertIsNone(self.proc.poll(), 'server process must still be running')
        self.assertEqual(self.ask('alive.test').rcode(), dns.rcode.NOERROR)


if __name__ == '__main__':
    unittest.main()
