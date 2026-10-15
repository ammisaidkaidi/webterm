"""Packet capture, as a line of JSON per packet on stdout.

There is no tcpdump on every box, so this is the capture itself: an AF_PACKET
socket in SOCK_DGRAM mode, which hands back packets with the link-layer header
already stripped, so loopback and ethernet parse the same way.

Raw sockets are root-only, so server.py runs this through `sudo -n` and reads
its output. On its own:

    sudo python3 sniffer.py --port 25 --addr 127.0.0.1

Each line is one packet:

    {"ts":1.7e9,"iface":"lo","dir":"in","proto":"tcp","src":"127.0.0.1",
     "sport":54321,"dst":"127.0.0.1","dport":25,"len":84,"flags":"PA",
     "payload":"3235302d..."}
"""

import argparse
import collections
import json
import socket
import struct
import sys
import time

ETH_P_ALL = 3
PACKET_OUTGOING = 4          # the kernel tells us which direction it went

TCP_FLAGS = [(0x01, "F"), (0x02, "S"), (0x04, "R"), (0x08, "P"),
             (0x10, "A"), (0x20, "U"), (0x40, "E"), (0x80, "C")]


def tcp_flags(bits):
    return "".join(ch for mask, ch in TCP_FLAGS if bits & mask) or "-"


def parse_transport(proto, data):
    """Ports, flags and payload for the protocols worth showing individually."""
    if proto == 6 and len(data) >= 20:                      # TCP
        sport, dport = struct.unpack("!HH", data[:4])
        offset = (data[12] >> 4) * 4
        return "tcp", sport, dport, tcp_flags(data[13]), data[offset:]
    if proto == 17 and len(data) >= 8:                      # UDP
        sport, dport = struct.unpack("!HH", data[:4])
        return "udp", sport, dport, "", data[8:]
    if proto in (1, 58) and len(data) >= 4:                 # ICMP / ICMPv6
        name = "icmp" if proto == 1 else "icmpv6"
        return name, None, None, f"type {data[0]}", data[4:]
    return {47: "gre", 50: "esp", 89: "ospf", 132: "sctp"}.get(proto, str(proto)), \
        None, None, "", data


def parse_packet(data):
    """IPv4 or IPv6 to a flat dict, or None for anything else (ARP, STP...)."""
    if not data:
        return None
    version = data[0] >> 4
    if version == 4 and len(data) >= 20:
        ihl = (data[0] & 0x0F) * 4
        total, proto = struct.unpack("!H", data[2:4])[0], data[9]
        src = socket.inet_ntop(socket.AF_INET, data[12:16])
        dst = socket.inet_ntop(socket.AF_INET, data[16:20])
        rest = data[ihl:total] if total else data[ihl:]
    elif version == 6 and len(data) >= 40:
        proto = data[6]
        src = socket.inet_ntop(socket.AF_INET6, data[8:24])
        dst = socket.inet_ntop(socket.AF_INET6, data[24:40])
        rest = data[40:]
    else:
        return None
    name, sport, dport, flags, payload = parse_transport(proto, rest)
    return {"proto": name, "src": src, "sport": sport, "dst": dst,
            "dport": dport, "flags": flags, "len": len(data), "payload": payload}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=0, help="only this TCP/UDP port (0 = every port)")
    ap.add_argument("--addr", default="", help="only this local address (blank = every address)")
    ap.add_argument("--iface", default="", help="only this interface")
    ap.add_argument("--snap", type=int, default=512, help="payload bytes kept per packet")
    ap.add_argument("--rate", type=int, default=400, help="packets per second ceiling")
    args = ap.parse_args()

    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(ETH_P_ALL))
    except PermissionError:
        sys.exit("packet capture needs root")
    if args.iface:
        sock.bind((args.iface, 0))

    # 0.0.0.0 means "every interface" to a listening socket, so treat it as no
    # filter rather than as a literal address to match.
    addr = "" if args.addr in ("", "0.0.0.0", "::") else args.addr

    # Loopback hands the same packet over twice, once outgoing and once
    # incoming, so a local conversation reads as doubled. Remember the last
    # few and drop an exact repeat that arrives within a few milliseconds.
    recent = collections.deque(maxlen=64)

    second, sent, dropped = int(time.time()), 0, 0
    while True:
        try:
            data, meta = sock.recvfrom(65535)
        except OSError:
            continue
        pkt = parse_packet(data)
        if pkt is None:
            continue
        if args.port and args.port not in (pkt["sport"], pkt["dport"]):
            continue
        if addr and addr not in (pkt["src"], pkt["dst"]):
            continue

        now = time.time()
        key = (pkt["src"], pkt["sport"], pkt["dst"], pkt["dport"],
               pkt["len"], pkt["flags"], pkt["payload"][:16])
        if any(k == key and now - t < 0.05 for k, t in recent):
            continue
        recent.append((key, now))

        if int(now) != second:
            if dropped:
                print(json.dumps({"ts": now, "dropped": dropped}), flush=True)
            second, sent, dropped = int(now), 0, 0
        if sent >= args.rate:
            dropped += 1
            continue
        sent += 1

        # With a port filter, "in" means toward that port, which is what the
        # reader is watching; without one, the kernel's own direction is all
        # there is to go on.
        if args.port:
            direction = "in" if pkt["dport"] == args.port else "out"
        else:
            direction = "out" if meta[2] == PACKET_OUTGOING else "in"
        payload = pkt.pop("payload")[:args.snap]
        pkt.update(ts=round(now, 4), iface=meta[0], dir=direction,
                   payload=payload.hex())
        print(json.dumps(pkt), flush=True)


if __name__ == "__main__":
    main()
