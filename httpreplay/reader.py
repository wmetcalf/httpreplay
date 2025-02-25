# Copyright (C) 2015-2019 Jurriaan Bremer <jbr@cuckoo.sh>
# This file is part of HTTPReplay - http://jbremer.org/httpreplay/
# See the file 'LICENSE' for copying permission.

from past.builtins import basestring
import dpkt
import logging
import socket
import traceback

from httpreplay.exceptions import (
    UnknownDatalink, UnknownEthernetProtocol, UnknownIpProtocol,
    UnknownTcpSequenceNumber, InvalidTcpPacketOrder, UnexpectedTcpData
)

log = logging.getLogger(__name__)

def inet_to_str(inet):
    try:
        return socket.inet_ntop(socket.AF_INET, inet)
    except ValueError:
        return socket.inet_ntop(socket.AF_INET6, inet)


class PcapReader(object):
    """Iterates over a PCAP file and yields all interesting events after
    having each packet processed by the various callback functions that can be
    provided by the user."""

    def __init__(self, fp_or_filepath):
        self.tcp = None
        self.udp = None
        self.values = []

        # Disables exceptions raised by PcapReader while the pcap is being
        # read. If disabled, the exceptions are stored in the self.exceptions
        # attribute
        self.raise_exceptions = True
        self.exceptions = {}

        # Backwards compatibilty with httpreplay<=0.1.14.
        if isinstance(fp_or_filepath, basestring):
            fp_or_filepath = open(fp_or_filepath, "rb")

        try:
            self.pcap = dpkt.pcap.Reader(fp_or_filepath)
        except ValueError as e:
            if str(e) == "invalid tcpdump header":
                log.critical("Currently we don't support PCAP-NG files")
            self.pcap = None

    def set_tcp_handler(self, tcp):
        self.tcp = tcp

    def set_udp_handler(self, udp):
        self.udp = udp

    def _parse_ethernet(self, packet):
        try:
            return dpkt.ethernet.Ethernet(packet)
        except dpkt.NeedData as e:
            if e.message:
                log.critical(
                    "Unknown exception parsing ethernet packet: %s", e
                )

    def process(self):
        if not self.pcap:
            return

        for ts, packet in self.pcap:

            if isinstance(packet, bytes):
                if self.pcap.datalink() == dpkt.pcap.DLT_EN10MB:
                    packet = self._parse_ethernet(packet)
                elif self.pcap.datalink() == 101:
                    packet = dpkt.ip.IP(packet)
                elif self.raise_exceptions:
                    raise UnknownDatalink(packet)
                else:
                    self.exceptions[ts] = {
                        "exception": UnknownDatalink,
                        "data": packet,
                        "trace": traceback.extract_stack()
                    }
                    continue

            if isinstance(packet, dpkt.ethernet.Ethernet):
                if isinstance(packet.data, dpkt.ip.IP):
                    packet = packet.data
                elif isinstance(packet.data, dpkt.ip6.IP6):
                    packet = packet.data
                elif isinstance(packet.data, dpkt.arp.ARP):
                    packet = packet.data
                elif self.raise_exceptions:
                    raise UnknownEthernetProtocol(packet)
                else:
                    self.exceptions[ts] = {
                        "exception": UnknownEthernetProtocol,
                        "data": packet,
                        "trace": traceback.extract_stack()
                    }
                    continue

            if isinstance(packet, dpkt.ip.IP):
                ip = packet
                if packet.p == dpkt.ip.IP_PROTO_ICMP:
                    packet = packet.data
                elif packet.p == dpkt.ip.IP_PROTO_TCP:
                    packet = packet.data
                elif packet.p == dpkt.ip.IP_PROTO_UDP:
                    packet = packet.data
                elif packet.p == dpkt.ip.IP_PROTO_IGMP:
                    continue
                elif self.raise_exceptions:
                    raise UnknownIpProtocol(packet)
                else:
                    self.exceptions[ts] = {
                        "exception": UnknownIpProtocol,
                        "data": packet,
                        "trace": traceback.extract_stack()
                    }
                    continue

            else:
                ip = None

            if isinstance(packet, dpkt.tcp.TCP):
                try:
                    self.tcp and self.tcp.process(ts, ip, packet)
                except InvalidTcpPacketOrder as e:
                    log.error(
                        "Invalid TCP packet order. Ts: %s (%s -> %s). %s", ts,
                        inet_to_str(ip.src), inet_to_str(ip.dst), e
                    )
                except UnknownTcpSequenceNumber as e:
                    log.error(
                        "Unknown TCP sequence number. Ts: %s (%s -> %s). %s",
                        ts, inet_to_str(ip.src), inet_to_str(ip.dst), e
                    )
                except UnexpectedTcpData as e:
                    log.error(
                        "Unexpected TCP data. Ts: %s (%s -> %s). %s", ts,
                        inet_to_str(ip.src), inet_to_str(ip.dst), e
                    )

            if isinstance(packet, dpkt.udp.UDP):
                self.udp and self.udp.process(ts, ip, packet)

            while self.values:
                yield self.values.pop(0)

        self.tcp and self.tcp.finish()
        while self.values:
            yield self.values.pop(0)

        self.udp and self.udp.finish()
        while self.values:
            yield self.values.pop(0)

    def handle(self, s, ts, protocol, sent, recv, tlsinfo=None):
        self.values.append((s, ts, protocol, sent, recv))
