#!/usr/bin/env python3
"""
DNS Forwarder with a local TTL-based cache, over UDP.

For every incoming datagram the server:
  1. parses it with dnspython (malformed packets are logged and ignored),
  2. looks the (name, type) pair up in an in-memory cache,
       hit  -> answers from the cache with the new query ID and the remaining TTL,
       miss -> forwards the original packet to a public resolver (1.1.1.1 by default),
  3. caches the upstream answer for min(TTL) seconds and returns it to the client,
  4. answers SERVFAIL if the upstream resolver does not reply in time.

Sockets: only the built-in `socket` module with SOCK_DGRAM.
Parsing: dnspython.
"""

import argparse
import time
from socket import AF_INET, SOCK_DGRAM, socket

import dns.message
import dns.rcode
import dns.rdatatype

BUFSIZE = 4096  # large enough for EDNS(0) answers above 512 bytes


def log(tag, msg):
    print(time.strftime('%H:%M:%S'), f'[{tag}]', msg, flush=True)


def ask_upstream(data, upstream, timeout):
    """Act as a client: forward the raw query to the upstream resolver and wait for the reply."""
    with socket(AF_INET, SOCK_DGRAM) as up_socket:  # closed automatically, even on timeout
        up_socket.settimeout(timeout)
        up_socket.sendto(data, upstream)
        reply, _ = up_socket.recvfrom(BUFSIZE)
    return reply


def serve(host, port, upstream, timeout):
    cache = {}  # (name, type) -> (answer bytes, expiry timestamp)

    server_socket = socket(AF_INET, SOCK_DGRAM)
    server_socket.bind((host, port))
    log('START', f'DNS server on {host}:{port}/UDP, upstream {upstream[0]}:{upstream[1]}')

    while True:
        data, client = server_socket.recvfrom(BUFSIZE)

        # 1. parse with dnspython; a malformed packet must never crash the server
        try:
            query = dns.message.from_wire(data)
            q = query.question[0]
        except Exception as e:
            log('MALFORMED', f'{len(data)} bytes from {client}: {type(e).__name__}')
            continue

        key = (q.name.to_text().lower(), q.rdtype)
        name = f'{q.name} {dns.rdatatype.to_text(q.rdtype)}'
        log('QUERY', f'{name} from {client} (ID {query.id})')

        # 2. cache lookup
        if key in cache:
            wire, expires = cache[key]
            remaining = int(expires - time.time())
            if remaining > 0:
                response = dns.message.from_wire(wire)
                response.id = query.id         # the client expects the ID of *its* query
                for rrset in response.answer:
                    rrset.ttl = remaining      # hand out the TTL that is left, not the original
                server_socket.sendto(response.to_wire(), client)
                log('CACHE HIT', f'{name} ({remaining}s left)')
                continue
            del cache[key]
            log('EXPIRED', f'{name} expired, removed from cache')

        # 3. forward to the upstream resolver
        log('CACHE MISS', f'{name} -> forwarding to {upstream[0]}')
        try:
            reply = ask_upstream(data, upstream, timeout)
            response = dns.message.from_wire(reply)
        except Exception as e:                 # timeout or unparsable reply
            log('ERROR', f'upstream did not answer ({type(e).__name__}), sending SERVFAIL')
            response = dns.message.make_response(query)
            response.set_rcode(dns.rcode.SERVFAIL)
            server_socket.sendto(response.to_wire(), client)
            continue

        # 4. cache for the smallest TTL in the answer, then reply to the client
        if response.answer:
            ttl = min(rrset.ttl for rrset in response.answer)
            cache[key] = (reply, time.time() + ttl)
            values = [rd.to_text() for rrset in response.answer for rd in rrset]
            log('CACHED', f'{name} -> {", ".join(values)} for {ttl}s')
        else:
            log('NOT CACHED', f'{name} -> {dns.rcode.to_text(response.rcode())}')
        server_socket.sendto(reply, client)


def main():
    p = argparse.ArgumentParser(description='DNS forwarder with a local TTL-based cache (UDP).')
    p.add_argument('port', nargs='?', type=int, default=5300,
                   help='UDP port to listen on (default 5300; port 53 needs root)')
    p.add_argument('--host', default='127.0.0.1', help='address to bind (default 127.0.0.1)')
    p.add_argument('--upstream', default='1.1.1.1', help='upstream resolver (default 1.1.1.1)')
    p.add_argument('--upstream-port', type=int, default=53, help='upstream port (default 53)')
    p.add_argument('--timeout', type=float, default=2.0, help='upstream timeout in seconds (default 2)')
    args = p.parse_args()

    try:
        serve(args.host, args.port, (args.upstream, args.upstream_port), args.timeout)
    except KeyboardInterrupt:
        log('STOP', 'server stopped')


if __name__ == '__main__':
    main()
