# Copyright (C) 2015-2020 Jurriaan Bremer <jbr@cuckoo.sh>
# This file is part of HTTPReplay - http://jbremer.org/httpreplay/
# See the file 'LICENSE' for copying permission.

import dpkt
import logging
import socket
import tlslite
import binascii

from httpreplay.exceptions import (UnknownTcpSequenceNumber, UnexpectedTcpData, InvalidTcpPacketOrder)
from httpreplay.misc import JA3, patch_dpkt_ssl_tlshello_unpacks
from httpreplay.shoddy import Protocol

# Patch dpkt v1.8.7 to support reading TLShello extensions. See method
# description for further info.
patch_dpkt_ssl_tlshello_unpacks()

log = logging.getLogger(__name__)

class Packet(bytes):
    ts = None

class TLSInfo(object):
    def __init__(self, JA3, JA3S, JA3_params, JA3S_params, client_hello,
                 server_hello):
        self.JA3 = JA3
        self.JA3S = JA3S
        self.JA3_params = JA3_params
        self.JA3S_params = JA3S_params
        self.client_hello = client_hello
        self.server_hello = server_hello

    def __repr__(self):
        return "<JA3=%s, JA3S=%s>" % (self.JA3, self.JA3S)

class TCPPacketStreamer(Protocol):
    """Translates TCP/IP packet streams into rich streams of stitched
    together packets that can be processed further by specific protocol
    handlers on top of the TCP/IP protocol (e.g., HTTP, SMTP, etc)."""

    def init(self, handlers):
        self.streams = {}
        self.handlers = handlers
        self.spurious = {}

    def init_handler(self, handler):
        # Follow the handler all the way to the end (so to support nested
        # protocol interpreters such as, e.g., HTTPS) and put our parent,
        # which happens to be the pcap reader generally speaking, as parent.
        while handler.parent:
            handler = handler.parent
        handler.parent = self.parent

    def handler(self, sn):
        (srcip, srcport, dstip, dstport) = sn
        if srcport in self.handlers:
            return self.handlers[srcport]
        elif dstport in self.handlers:
            return self.handlers[dstport]
        elif "generic" in self.handlers:
            return self.handlers["generic"]

        # Returning the abstract Protocol class here so all packets will
        # end up in nowhere but at least there will still be a parent.
        log.warning("Unhandled protocol port=%s/%s", srcport, dstport)
        return Protocol

    def stream(self, ip, tcp, reverse=False):
        return (
            socket.inet_ntoa(ip.dst), tcp.dport,
            socket.inet_ntoa(ip.src), tcp.sport,
        ) if reverse else (
            socket.inet_ntoa(ip.src), tcp.sport,
            socket.inet_ntoa(ip.dst), tcp.dport,
        )

    def process(self, ts, ip, tcp):
        sn = self.stream(ip, tcp)
        sr = self.stream(ip, tcp, reverse=True)

        # This is a new connection.
        if sn not in self.streams and tcp.flags == dpkt.tcp.TH_SYN:
            # Pick a handler for this stream.
            handler = self.handler(sn)

            # Initialize the handler.
            handler = handler()
            self.init_handler(handler)

            # Create a new instance of this handler.
            self.streams[sn] = TCPStream(handler, sn)

        if sn in self.streams:
            s = self.streams[sn]
            to_server = True
        elif sr in self.streams:
            s = self.streams[sr]
            to_server = False
        else:
            log.warning("Unknown stream %s:%s -> %s:%s!", *sn)
            return

        s.process(ts, tcp, to_server)

    def finish(self):
        for stream in self.streams.values():
            stream.finish()

class TCPRetransmission(Protocol):
    """Indicates a dead host, one that we were not able to connect to during
    the time that this PCAP was alive and kicking."""

class TCPStream(Protocol):
    """Concatenates rich TCP/IP streams into question/response sequences."""

    def init(self, s):
        self.s = s
        self.packets = {}
        self.origins = {}
        self.sent = []
        self.recv = []
        self.conn = False
        self.ts = None

        # Sequence numbers for the client and server, respectively.
        self.cli = None
        self.srv = None

        # The state of this TCP stream.
        self.state = "init_syn"

    def state_init_syn(self, ts, tcp, to_server):
        # When no service is listening on the other end a server may send RST
        # packets back after which the state will be reverted to "init_syn".
        # And thus we have to handle any additional RSTs here as well. (Note
        # that we don't really change the state here, so RSTs from the
        # opposite side will also end up here).
        if tcp.flags & dpkt.tcp.TH_RST:
            return

        if not to_server and tcp.flags != dpkt.tcp.TH_SYN:
            raise InvalidTcpPacketOrder(tcp)

        if not to_server and tcp.data:
            raise UnexpectedTcpData(tcp)

        self.ts = ts
        self.cli = tcp.seq
        self.state = "init_syn_ack"

    def state_init_syn_ack(self, ts, tcp, to_server):
        # Retransmission of the SYN packet. Indicates that the server is not
        # responding within the given timeframe and thus might be a dead host.
        if to_server and tcp.flags == dpkt.tcp.TH_SYN:
            # self.parent.handle(self.s, ts, TCPRetransmission(),
            #                    None, special="deadhost")
            return

        # The reply from a server when no service is listening on the given
        # port. Generally speaking the client will retry sending SYN packets.
        if tcp.flags & dpkt.tcp.TH_RST:
            # self.parent.handle(self.s, ts, None, None, special="deadhost")
            self.state = "init_syn"
            return

        # Some PCAPs completely miss out on incoming traffic. Not really sure
        # whether trying to parse this really makes sense, but here we go.
        if to_server and tcp.flags == dpkt.tcp.TH_ACK:
            self.cli, self.srv = tcp.seq, tcp.ack
            return self.state_init_ack(ts, tcp, to_server)

        # Not much to comment here really.
        if not to_server and tcp.flags == dpkt.tcp.TH_ACK:
            log.warning("Server replied with an ACK to a SYN packet "
                        "(timestamp %f).", ts)
            return

        # A best guess would be; the SYN ACK/ACK packets were not captured.
        if to_server and tcp.flags & dpkt.tcp.TH_ACK and tcp.data:
            log.warning(
                "We didn't receive SYN ACK or ACK packets but are proceeding "
                "straight away to the TCP data (timestamp %f).", ts
            )
            self.cli, self.srv, self.state = tcp.seq, tcp.ack, "conn"
            return self.state_conn(ts, tcp, to_server)

        if not to_server and tcp.flags == (dpkt.tcp.TH_PUSH | dpkt.tcp.TH_ACK):
            self.state = "init_syn"
            return

        if to_server or tcp.flags != (dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK):
            raise InvalidTcpPacketOrder(tcp)

        if tcp.data:
            raise UnexpectedTcpData(tcp)

        self.cli = tcp.ack
        self.srv = tcp.seq + 1
        self.state = "init_ack"

    def state_init_ack(self, ts, tcp, to_server):
        # Retransmission of the SYN packet. Let's ignore that for now.
        if to_server and tcp.flags == dpkt.tcp.TH_SYN:
            # self.parent.handle(self.s, ts, TCPRetransmission(),
            #                    None, special="deadhost")
            return

        # Retransmission of the SYN ACK packet. Indicates that the client is
        # not responding within the given timeframe; a potential SYN flood?
        if not to_server and tcp.flags == (dpkt.tcp.TH_SYN | dpkt.tcp.TH_ACK):
            # self.parent.handle(self.s, ts, TCPRetransmission(),
            #                    None, special="synflood")
            return

        # The client has retransmitted the SYN ACK packet twice (usually) and
        # now gives up through a RST packet.
        if not to_server and tcp.flags == dpkt.tcp.TH_RST:
            # self.parent.handle(self.s, ts, TCPRetransmission(),
            #                    None, special="synflood")
            return

        # The client has received a SYN ACK but is no longer interested in
        # connecting to this service and thus quits through a RST.
        if to_server and tcp.flags == dpkt.tcp.TH_RST:
            return

        if not to_server:
            log.warning("The server is spamming the client even though an "
                        "ACK has not been provided yet (timestamp %f).", ts)
            return

        # It is possible that a client sends out a request straight away along
        # with the ACK packet (the push flag might also be set)
        if tcp.flags & dpkt.tcp.TH_ACK and tcp.data:
            self.state = "conn"
            self.state_conn(ts, tcp, to_server)
            return

        # You know, let's send a FIN packet.
        if to_server and tcp.flags & dpkt.tcp.TH_FIN:
            self.state = "conn_finish"
            return

        if tcp.flags != dpkt.tcp.TH_ACK:
            raise InvalidTcpPacketOrder(tcp)

        if tcp.seq != self.cli:
            raise UnknownTcpSequenceNumber(tcp)

        if tcp.ack != self.srv:
            raise UnknownTcpSequenceNumber(tcp)

        if tcp.data:
            raise UnexpectedTcpData(tcp)

        self.state = "conn"

    def ack_packets(self, seq, ack, to_server):
        packets = []

        while (seq, ack) in self.packets:
            buf = self.packets.pop((seq, ack))
            packets.insert(0, buf)
            seq -= len(buf)
            self.origins.pop((seq, ack), None)

        if not self.ts and packets:
            self.ts = packets[0].ts

        if to_server:
            self.sent += packets
        else:
            self.recv += packets

    def state_conn(self, ts, tcp, to_server):
        if tcp.flags & dpkt.tcp.TH_ACK:
            self.ack_packets(tcp.ack, tcp.seq, not to_server)

        if tcp.flags & dpkt.tcp.TH_RST:
            self.state = "conn_closed"
            self.ack_packets(tcp.ack, tcp.seq - 1, not to_server)

        tcp_seq = tcp.seq + len(tcp.data)

        # If this is the final packet then the TCP sequence should be +1'd.
        if tcp.flags & dpkt.tcp.TH_FIN:
            self.state = "conn_finish"

            if to_server:
                self.cli = tcp_seq + 1
            else:
                self.srv = tcp_seq + 1

        if not tcp.data:
            return

        if tcp.data and to_server and self.recv:
            self.parent.handle(self.s, self.ts, "tcp", b"".join(self.sent), b"".join(self.recv), None)
            self.sent, self.recv = [], []
            self.ts = None

        # Switch bytes to str
        packet = Packet(tcp.data)
        packet.ts = ts

        if (tcp.seq, tcp.ack) in self.origins or (tcp_seq, tcp.ack) in self.packets:
            # We do not want to prefer the retransmission here (?)
            if (tcp_seq, tcp.ack) in self.packets:
                dup = self.packets[tcp_seq, tcp.ack]
            else:
                dup = self.packets[self.origins[tcp.seq, tcp.ack]]

            # Only make it a warning when the packet size is actually
            # different - same length packets doesn't matter much for us.
            (log.warning if len(dup) != len(packet) else log.debug)(
                "Found a retransmitted packet possibly with a different size "
                "than the original packet: %s vs %s (timestamps %f vs %f)!",
                len(dup), len(packet), dup.ts, packet.ts,
            )
        else:
            self.origins[tcp.seq, tcp.ack] = tcp_seq, tcp.ack
            self.packets[tcp_seq, tcp.ack] = packet

    def state_conn_closed(self, ts, tcp, to_server):
        # Enqueue this packet if any is provided.
        self.state_conn(ts, tcp, to_server)

        # And let packets loose straight away.
        self.ack_packets(tcp.seq + len(tcp.data), tcp.ack, to_server)

    def state_conn_finish(self, ts, tcp, to_server):
        # Still acknowledging older packets.
        if self.cli != tcp.ack and self.srv != tcp.ack:
            self.state_conn(ts, tcp, to_server)
            return

        if tcp.flags & dpkt.tcp.TH_ACK:
            if to_server:
                if self.srv != tcp.ack:
                    raise InvalidTcpPacketOrder(tcp)

                # Process any final packets.
                tcp.ack -= 1
                self.state_conn(ts, tcp, to_server)

                # Indicate the end of this connection.
                self.srv = None

            if not to_server:
                if self.cli != tcp.ack:
                    raise InvalidTcpPacketOrder(tcp)

                # Process any final packets.
                tcp.ack -= 1
                self.state_conn(ts, tcp, to_server)

                # Indicate the end of this connection.
                self.cli = None

        if tcp.flags & dpkt.tcp.TH_FIN:
            if to_server:
                self.cli = tcp.seq + 1
            else:
                self.srv = tcp.seq + 1

    states = {
        "init_syn": state_init_syn,
        "init_syn_ack": state_init_syn_ack,
        "init_ack": state_init_ack,
        "conn": state_conn,
        "conn_closed": state_conn_closed,
        "conn_finish": state_conn_finish,
    }

    def process(self, ts, tcp, to_server):
        self.states[self.state](self, ts, tcp, to_server)

    def finish(self):
        if self.sent or self.recv:
            self.parent.handle(
                self.s, self.ts, "tcp", b"".join(self.sent), b"".join(self.recv)
            )

        if self.packets:
            log.warning(
                "There are still packets in the pipeline. It is likely these "
                "were originally sent, then retransmitted with an extended "
                "length, acknowledged before the retransmission, and then "
                "sort of forgotten (timestamps %s).",
                " ".join("%f" % packet.ts for packet in self.packets.values())
            )

class _TLSStream(tlslite.tlsrecordlayer.TLSRecordLayer):
    """Helper class for TLS stream decryption. This class wraps around
    functionality found in the tlslite library which does the actual TLS
    decryption."""

    tls_versions = {
        dpkt.ssl.SSL3_V: (3, 0),
        dpkt.ssl.TLS1_V: (3, 1),
        dpkt.ssl.TLS11_V: (3, 2),
        dpkt.ssl.TLS12_V: (3, 3),
    }

    def init_cipher(self, tls_version, cipher_suite, master_secret,
                    client_random, server_random, cipher_implementations):
        self._client = True
        self.version = self.tls_versions[tls_version]

        try:
            self._calcPendingStates(
                cipher_suite, master_secret, client_random,
                server_random, cipher_implementations
            )
        except AssertionError:
            log.critical("Unsupported TLS cipher suite: 0x%x.", cipher_suite)
            return

        self.server_state = self._recordLayer._pendingReadState
        self.client_state = self._recordLayer._pendingWriteState
        return True

    def decrypt(self, state, record_type, buf):
        self._recordLayer._readState = state
        if state.encContext.isBlockCipher:
            return bytes(self._recordLayer._decryptThenMAC(
                record_type, bytearray(buf)
            ))
        elif state.encContext.isAEAD:
            return bytes(self._recordLayer._decryptAndUnseal(
                record_type, bytearray(buf)
            ))
        else:
            return bytes(self._recordLayer._decryptStreamThenMAC(
                record_type, bytearray(buf)
            ))

    def decrypt_server(self, record_type, buf):
        try:
            return self.decrypt(self.server_state, record_type, buf)
        except tlslite.errors.TLSBadRecordMAC:
            log.warning("Bad MAC record, cannot decrypt server stream.")
            return b""
        except tlslite.errors.TLSDecryptionFailed:
            log.warning(
                "Invalid data length. Data modulo blocklength was not 0"
            )
            return b""

    def decrypt_client(self, record_type, buf):
        try:
            return self.decrypt(self.client_state, record_type, buf)
        except tlslite.errors.TLSBadRecordMAC:
            log.warning("Bad MAC record, cannot decrypt client stream.")
            return b""
        except tlslite.errors.TLSDecryptionFailed:
            log.warning(
                "Invalid data length. Data modulo blocklength was not 0"
            )
            return b""

class TLSStream(Protocol):
    """Decrypts TLS streams into a TCPStream-like session."""

    def init(self, secrets=None):
        self.secrets = secrets
        self.state = "init"
        self.tls = _TLSStream(None)
        self.sent = []
        self.recv = []
        self.raw_sent = b""
        self.raw_recv = b""

    def parse_record(self, record):
        if record.type not in dpkt.ssl.RECORD_TYPES:
            raise dpkt.ssl.SSL3Exception(
                "Invalid record type: %d" % record.type
            )

        return dpkt.ssl.RECORD_TYPES[record.type](record.data)

    def state_init(self, s, ts):
        if not self.sent or not self.recv:
            return

        self.client_hello = self.parse_record(self.sent.pop(0))
        self.server_hello = self.parse_record(self.recv.pop(0))

        if not isinstance(self.client_hello.data, dpkt.ssl.TLSClientHello):
            log.info(
                "Stream %s:%d -> %s:%d doesn't appear to be a proper TLS "
                "stream (perhaps the client is outdated), skipping it.", *s
            )
            self.state = "done"
            return

        if not isinstance(self.server_hello.data, dpkt.ssl.TLSServerHello):
            log.info(
                "Stream %s:%d -> %s:%d doesn't appear to be a proper TLS "
                "stream (perhaps the server is outdated), skipping it.", *s
            )
            self.state = "done"
            return

        client_random = self.client_hello.data.random
        server_random = self.server_hello.data.random

        # The master secret can be obtained through the session id or
        # a (client random, server random) tuple or the client random only.
        if self.server_hello.data.session_id in self.secrets:
            master_secret = self.secrets[self.server_hello.data.session_id]
            log.debug("Master Secret: Session id found")
        elif client_random in self.secrets:
            master_secret = self.secrets[client_random]
            log.debug("Master Secret: Client random found")
        elif (client_random, server_random) in self.secrets:
            master_secret = self.secrets[client_random, server_random]
            log.debug("Master Secret: (client_random, server_random) found")
        else:
            log.info("Could not find TLS master secret for stream "
                     "%s:%d -> %s:%d, skipping it.", *s)
            self.state = "done"
            return

        # It could be the cipher suite passed along by the server is not
        # supported, in that case we can't decrypt this TLS stream.
        cipher_success = self.tls.init_cipher(
            self.client_hello.data.version,
            self.server_hello.data.cipher_suite,
            master_secret, client_random, server_random,
            tlslite.handshakesettings.CIPHER_IMPLEMENTATIONS
        )

        if not cipher_success:
            self.state = "done"
            return True

        self.state = "client"
        return True

    def state_client(self, s, ts):
        # Wait for the "Change Cipher Spec" record.
        while self.sent:
            if self.sent.pop(0).type == 20:
                self.state = "server"
                return True

    def state_server(self, s, ts):
        # Wait for the "Change Cipher Spec" record.
        while self.recv:
            if self.recv.pop(0).type == 20:
                self.state = "decrypt"
                return True

    def state_decrypt(self, s, ts):
        if not self.sent or not self.recv:
            return

        record = self.recv.pop(0)
        self.tls.decrypt_server(record.type, record.data)

        record = self.sent.pop(0)
        self.tls.decrypt_client(record.type, record.data)

        self.state = "stream"
        return True

    def state_stream(self, s, ts):
        if self.sent and self.recv:
            sent = []
            while self.sent:
                record = self.sent.pop(0)
                sentdata = self.tls.decrypt_client(record.type, record.data)
                if sentdata:
                    if isinstance(sentdata,str):
                        sentdata=bytes(sentdata,'utf8')
                    if isinstance(sentdata,bytes):
                        sent.append(sentdata)

            recv = []
            while self.recv:
                record = self.recv.pop(0)

                try:
                    recvdata = self.tls.decrypt_server(record.type, record.data)
                    if recvdata:
                        if isinstance(recvdata,str):
                            recvdata=bytes(recvdata,'utf8')
                        if isinstance(recvdata,bytes):
                            recv.append(recvdata)

                except tlslite.errors.TLSProtocolException:
                    log.info(
                        "Error decrypting TLS content, perhaps something "
                        "went wrong during the process of stitching packets "
                        "back together in the right order (timestamp %f).",
                        ts,
                    )
            
            if not isinstance(sent[0], bytes):
                sent = [ord(c) if len(c) != 0 else b"" for c in sent]
            if not isinstance(recv[0], bytes):
                recv = [ord(c) if len(c) != 0 else b"" for c in recv]
            ja3, ja3s, ja3_p, ja3s_p = None, None, None, None
            try:
                ja3, ja3_p = JA3.JA3(self.client_hello.data)
            except ValueError as e:
                log.warning("Failed to calculate JA3: %s", e)

            try:
                ja3s, ja3s_p = JA3.JA3S(self.server_hello.data)
            except ValueError as e:
                log.warning("Failed to calculate JA3S: %s", e)

            tlsinfo = TLSInfo(
                JA3=ja3, JA3S=ja3s, JA3_params=ja3_p, JA3S_params=ja3s_p,
                client_hello=self.client_hello, server_hello=self.server_hello
            )
            self.parent.handle(s, ts, "tls", b"".join(sent), b"".join(recv), tlsinfo)
            return True

    def state_done(self, s, ts):
        while self.sent:
            self.sent.pop(0)

        while self.recv:
            self.recv.pop(0)

    states = {
        "init": state_init,
        "client": state_client,
        "server": state_server,
        "decrypt": state_decrypt,
        "stream": state_stream,
        "done": state_done,
    }

    def handle(self, s, ts, protocol, sent, recv, tlsinfo=None):
        if protocol != "tcp":
            self.parent.handle(s, ts, protocol, sent, recv, tlsinfo)
            return

        try:
            # Parse sent TLS records.
            self.raw_sent += sent
            records, length = dpkt.ssl.tls_multi_factory(sent)
            self.raw_sent = self.raw_sent[length:]
            self.sent += records

            # Parse received TLS records.
            self.raw_recv += recv
            records, length = dpkt.ssl.tls_multi_factory(recv)
            self.raw_recv = self.raw_recv[length:]
            self.recv += records
        except dpkt.ssl.SSL3Exception:
            # This is not a TLS stream or we're unable to decrypt it so we
            # skip it and forward it straight ahead to our parent.
            self.parent.handle(s, ts, protocol, sent, recv)
            return

        # Keep going while non-False is returned.
        while self.states[self.state](self, s, ts):
            pass

# Until our pull request (https://github.com/tomato42/tlslite-ng/pull/96) is
# accepted we're going to monkey patch tlslite to contain our desired changes.
_cs = tlslite.constants.CipherSuite
if 0xc009 not in _cs.ietfNames:
    _cs.TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA = 0xC009
    _cs.ietfNames[0xC009] = "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA"
    _cs.TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA = 0xC00A
    _cs.ietfNames[0xC00A] = "TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA"
    _cs.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256 = 0xC02B
    _cs.ietfNames[0xC02B] = "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256"
    _cs.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384 = 0xC02C
    _cs.ietfNames[0xC02C] = "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384"
    _cs.aes128Suites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA)
    _cs.aes256Suites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA)
    _cs.shaSuites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA)
    _cs.shaSuites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_256_CBC_SHA)
    _cs.aes128GcmSuites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256)
    _cs.aes256GcmSuites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384)
    _cs.aeadSuites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256)
    _cs.aeadSuites.append(_cs.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384)

