/* ============================================================================
 * raw_tcp.c — cross-platform raw TCP packet crafter + sender
 * ============================================================================
 *
 * WHAT THIS DEMONSTRATES
 *   Hand-building an IPv4 + TCP packet byte-by-byte from the RFCs
 *   (RFC 791 IPv4, RFC 793 TCP, RFC 1071 checksum) and injecting it
 *   onto the wire with a raw socket.
 *
 * ABOUT "NO OS LIBRARIES"
 *   - The packet-CONSTRUCTION half of this file is 100 % portable C:
 *     every header structure, constant, byte-order helper and the IP
 *     parser are defined right here from the RFCs. Nothing is taken
 *     from <netinet/ip.h>, <linux/ip.h> or Winsock's protocol defs.
 *   - The INJECTION half cannot exist without the OS at all: a
 *     user-space program has no direct access to the network card,
 *     so every packet must finally pass through one OS call
 *     (socket / sendto). Only that gate is used here — the BSD
 *     socket API, identical on Windows (Winsock), Linux, macOS.
 *     One small #ifdef shim bridges the 3 cosmetic differences.
 *
 * BUILD
 *   Linux/macOS    : cc -O2 raw_tcp.c -o raw_tcp
 *   Windows (MinGW): gcc -O2 raw_tcp.c -o raw_tcp.exe -lws2_32
 *   Windows (MSVC) : cl /O2 raw_tcp.c ws2_32.lib
 *   (also compiles as C++: g++ -O2 -x c++ raw_tcp.c -o raw_tcp)
 *
 * RUN  (raw sockets need privileges: root/sudo on Linux & macOS,
 *       Administrator on Windows)
 *   ./raw_tcp <src_ip> <dst_ip> <src_port> <dst_port> [flags] [payload]
 *   flags = any of F S R P A U  (FIN SYN RST PSH ACK URG), default S
 *   example:
 *     sudo ./raw_tcp 192.168.1.50 192.168.1.1 40000 80 S "crafted-by-hand"
 *   built-in check:
 *     ./raw_tcp --selftest
 *
 * VERIFY
 *   Linux/macOS : sudo tcpdump -i any -X 'tcp port 80'
 *   Windows     : Wireshark -> display filter: tcp.port == 80
 *
 * NOTE ON SPOOFED SOURCE ADDRESSES
 *   - Linux/macOS: the frame leaves the machine exactly as built.
 *   - Windows (XP SP2+): the kernel OVERWRITES the source IP with the
 *     host's real address — spoofing is blocked by OS design.
 *   - Routers with uRPF/egress filtering drop packets whose source
 *     address cannot exist on that link.
 *
 * USE RESPONSIBLY: only against networks and hosts you own or are
 * explicitly authorized to test.
 * ==========================================================================*/

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>

/* =========================================================================
 * [1/4] PLATFORM SHIM — the ONLY place Windows vs Unix appears.
 * The BSD socket API is the near-universal packet-injection gate:
 * Linux/macOS have it natively; Windows ships it as Winsock.
 * ========================================================================= */
#ifdef _WIN32
  #include <winsock2.h>   /* socket(), sendto(), sockaddr_in, WSAStartup */
  #include <ws2tcpip.h>
  #pragma comment(lib, "ws2_32.lib")   /* MSVC auto-link (MinGW: use -lws2_32) */
#else
  #include <sys/socket.h> /* socket(), sendto(), sockaddr               */
  #include <netinet/in.h> /* sockaddr_in layout, IP_HDRINCL             */
  #include <unistd.h>     /* close()                                    */
  typedef int SOCKET;
  #define INVALID_SOCKET  (-1)
  #define SOCKET_ERROR    (-1)
  #define closesocket(s)  close((s))
#endif

#ifndef AF_INET
#define AF_INET 2
#endif
#ifndef SOCK_RAW
#define SOCK_RAW 3
#endif
#ifndef IPPROTO_IP
#define IPPROTO_IP 0
#endif
#ifndef IPPROTO_RAW
#define IPPROTO_RAW 255
#endif
#ifndef IP_HDRINCL
#define IP_HDRINCL 2
#endif
#define PROTO_TCP 6

/* =========================================================================
 * [2/4] RFC HEADER DEFINITIONS — built from the RFCs, not from OS headers.
 * pack(1) removes struct padding so memory layout == wire layout.
 * ========================================================================= */
#if defined(_MSC_VER)
  #pragma pack(push, 1)
  #define PACKED
#else
  #define PACKED __attribute__((packed))
#endif

/* RFC 791 — IPv4 header, 20 bytes (no options) */
typedef struct {
    uint8_t  ver_ihl;          /* hi nibble: version(4) | lo nibble: IHL(5) */
    uint8_t  type_of_service;  /* today: DSCP/ECN                           */
    uint16_t total_length;     /* IP header + payload, bytes                */
    uint16_t identification;   /* datagram id (fragmentation/reassembly)    */
    uint16_t flags_fragoff;    /* flags(3 bits) | fragment offset(13 bits)  */
    uint8_t  time_to_live;     /* decremented at every hop                  */
    uint8_t  protocol;         /* 6 = TCP, 17 = UDP, 1 = ICMP               */
    uint16_t header_checksum;  /* over the IP header only                   */
    uint32_t source_address;   /* big-endian                                */
    uint32_t dest_address;     /* big-endian                                */
} PACKED ipv4_header_t;

/* RFC 793 — TCP header, 20 bytes (no options) */
typedef struct {
    uint16_t source_port;
    uint16_t dest_port;
    uint32_t sequence_number;
    uint32_t acknowledgment_number;
    uint8_t  data_offset;      /* hi nibble: header length in 32-bit words  */
    uint8_t  flags;            /* FIN|SYN|RST|PSH|ACK|URG                   */
    uint16_t window;           /* flow-control window                       */
    uint16_t checksum;         /* over pseudo-header + TCP + payload        */
    uint16_t urgent_pointer;
} PACKED tcp_header_t;

/* Checksum pseudo-header (RFC 793 §3.1) — NEVER transmitted; exists only
 * to cryptographically-ish bind the TCP segment to its IP addresses.     */
typedef struct {
    uint32_t source_address;
    uint32_t dest_address;
    uint8_t  zero;
    uint8_t  protocol;
    uint16_t tcp_length;
} PACKED pseudo_header_t;

#if defined(_MSC_VER)
  #pragma pack(pop)
#endif
#undef PACKED

/* Compile-time proof that the compiler added no padding */
typedef char ipv4_size_ok[sizeof(ipv4_header_t) == 20 ? 1 : -1];
typedef char tcp_size_ok [sizeof(tcp_header_t)  == 20 ? 1 : -1];
typedef char pseudo_size_ok[sizeof(pseudo_header_t) == 12 ? 1 : -1];

/* =========================================================================
 * [3/4] BYTE ORDER + CHECKSUM — done by hand so nothing is hidden.
 * ========================================================================= */

/* All multi-byte protocol header fields are big-endian ("network order").
 * These replace htons()/htonl() so you can see exactly what they do.     */
static int host_is_little_endian(void)
{
    const uint16_t probe = 1;
    return *(const uint8_t *)&probe == 1;
}

static uint16_t to_be16(uint16_t v)
{
    return host_is_little_endian()
         ? (uint16_t)((uint16_t)(v >> 8) | (uint16_t)(v << 8))
         : v;
}

static uint32_t to_be32(uint32_t v)
{
    if (!host_is_little_endian())
        return v;
    return ((v & 0x000000FFu) << 24) |
           ((v & 0x0000FF00u) <<  8) |
           ((v & 0x00FF0000u) >>  8) |
           ((v & 0xFF000000u) >> 24);
}

/* RFC 1071 internet checksum: sum 16-bit big-endian words, fold the
 * carries, one's-complement. Used for both IP header and TCP segment.   */
static uint16_t internet_checksum(const void *data, size_t length)
{
    const uint8_t *p   = (const uint8_t *)data;
    uint32_t       sum = 0;

    while (length >= 2) {
        sum += (uint32_t)((p[0] << 8) | p[1]);  /* next 16-bit word        */
        p   += 2;
        length -= 2;
    }
    if (length == 1)                            /* odd trailing byte       */
        sum += (uint32_t)(p[0] << 8);

    while (sum >> 16)                           /* fold carries back in    */
        sum = (sum & 0xFFFFu) + (sum >> 16);

    return (uint16_t)(~sum & 0xFFFFu);
}

/* Hand-rolled dotted-quad parser — no inet_addr()/inet_pton(). */
static int parse_ipv4(const char *text, uint32_t *out_be)
{
    unsigned a, b, c, d;
    char     junk;
    if (text == NULL ||
        sscanf(text, "%u.%u.%u.%u%c", &a, &b, &c, &d, &junk) != 4)
        return 0;
    if (a > 255 || b > 255 || c > 255 || d > 255)
        return 0;
    *out_be = to_be32((a << 24) | (b << 16) | (c << 8) | d);
    return 1;
}

/* "SA" -> SYN|ACK, "S" -> SYN, "FPU" -> FIN|PSH|URG ... */
static uint8_t parse_flags(const char *spec)
{
    uint8_t flags = 0;
    if (spec == NULL)
        return 0x02;                              /* default: SYN */
    for (; *spec; spec++) {
        switch (*spec) {
        case 'F': case 'f': flags |= 0x01; break; /* FIN */
        case 'S': case 's': flags |= 0x02; break; /* SYN */
        case 'R': case 'r': flags |= 0x04; break; /* RST */
        case 'P': case 'p': flags |= 0x08; break; /* PSH */
        case 'A': case 'a': flags |= 0x10; break; /* ACK */
        case 'U': case 'u': flags |= 0x20; break; /* URG */
        default: break;
        }
    }
    return flags ? flags : 0x02;
}

static void hexdump(const void *data, size_t length)
{
    const uint8_t *p = (const uint8_t *)data;
    size_t i;
    printf("  ---- wire image ----");
    for (i = 0; i < length; i++) {
        if (i % 16 == 0)
            printf("\n  %04zx  ", i);
        printf("%02X ", p[i]);
    }
    printf("\n  ---------------------\n");
}

static int selftest(void)
{
    /* RFC 1071 worked example:
     * bytes 00 01 f2 03 f4 f5 f6 f7 -> checksum 0x220d                */
    static const uint8_t sample[8] =
        { 0x00, 0x01, 0xF2, 0x03, 0xF4, 0xF5, 0xF6, 0xF7 };
    const uint16_t got  = internet_checksum(sample, sizeof(sample));
    const uint16_t want = 0x220D;

    printf("checksum self-test : got 0x%04X, expected 0x%04X -> %s\n",
           got, want, got == want ? "PASS" : "FAIL");
    printf("header sizes       : IP=%zu TCP=%zu pseudo=%zu (all must match RFC)\n",
           sizeof(ipv4_header_t), sizeof(tcp_header_t), sizeof(pseudo_header_t));
    printf("little-endian host : %s\n",
           host_is_little_endian() ? "yes" : "no");
    return got == want ? EXIT_SUCCESS : EXIT_FAILURE;
}

/* =========================================================================
 * [4/4] ASSEMBLE + INJECT
 * ========================================================================= */
int main(int argc, char **argv)
{
    uint32_t       src_be, dst_be;
    const size_t   ip_len  = sizeof(ipv4_header_t);   /* 20 */
    const size_t   tcp_len = sizeof(tcp_header_t);    /* 20 */
    size_t         total, payload_len;
    uint8_t       *packet;
    tcp_header_t  *tcp;
    ipv4_header_t *ip;

    if (argc == 2 && strcmp(argv[1], "--selftest") == 0)
        return selftest();

    if (argc < 5) {
        fprintf(stderr,
            "raw TCP packet crafter — hand-built headers, raw socket injection\n"
            "usage: %s <src_ip> <dst_ip> <src_port> <dst_port> [flags] [payload]\n"
            "       %s --selftest\n"
            "  flags: F S R P A U (FIN SYN RST PSH ACK URG), default S\n"
            "example:\n"
            "  sudo %s 192.168.1.50 192.168.1.1 40000 80 S \"crafted-by-hand\"\n",
            argv[0], argv[0], argv[0]);
        return EXIT_FAILURE;
    }

    {
        const char    *src_text   = argv[1];
        const char    *dst_text   = argv[2];
        const uint16_t src_port   = (uint16_t)strtoul(argv[3], NULL, 0);
        const uint16_t dst_port   = (uint16_t)strtoul(argv[4], NULL, 0);
        const uint8_t  tcp_flags  = parse_flags(argc > 5 ? argv[5] : NULL);
        const char    *payload    = argc > 6 ? argv[6] : "";

        payload_len = strlen(payload);
        if (payload_len > 1480) {                 /* keep it in one Ethernet frame */
            fprintf(stderr, "error: payload too large for a single frame (max 1480)\n");
            return EXIT_FAILURE;
        }
        if (!parse_ipv4(src_text, &src_be) || !parse_ipv4(dst_text, &dst_be)) {
            fprintf(stderr, "error: bad IPv4 address ('%s' or '%s')\n",
                    src_text, dst_text);
            return EXIT_FAILURE;
        }

        /* -- one contiguous buffer: [IP header][TCP header][payload] ----- */
        total  = ip_len + tcp_len + payload_len;
        packet = (uint8_t *)calloc(1, total);
        if (packet == NULL) {
            fprintf(stderr, "error: out of memory\n");
            return EXIT_FAILURE;
        }

        /* -- TCP header -------------------------------------------------- */
        tcp = (tcp_header_t *)(packet + ip_len);
        tcp->source_port           = to_be16(src_port);
        tcp->dest_port             = to_be16(dst_port);
        tcp->sequence_number       = to_be32(0x11223344u);      /* demo ISN  */
        tcp->acknowledgment_number = to_be32(0);
        tcp->data_offset           = (uint8_t)((tcp_len / 4) << 4); /* 5 -> 0x50 */
        tcp->flags                 = tcp_flags;
        tcp->window                = to_be16(8192);
        tcp->checksum              = 0;                         /* next step */
        tcp->urgent_pointer        = 0;

        if (payload_len)
            memcpy(packet + ip_len + tcp_len, payload, payload_len);

        /* -- TCP checksum over: pseudo-header + TCP header + payload ----- */
        {
            pseudo_header_t pseudo;
            const size_t    ck_len = sizeof(pseudo) + tcp_len + payload_len;
            uint8_t        *ck     = (uint8_t *)calloc(1, ck_len);

            pseudo.source_address = src_be;
            pseudo.dest_address   = dst_be;
            pseudo.zero           = 0;
            pseudo.protocol       = PROTO_TCP;
            pseudo.tcp_length     = to_be16((uint16_t)(tcp_len + payload_len));

            if (ck == NULL) {
                fprintf(stderr, "error: out of memory\n");
                free(packet);
                return EXIT_FAILURE;
            }
            memcpy(ck,                  &pseudo, sizeof(pseudo));
            memcpy(ck + sizeof(pseudo), tcp,     tcp_len + payload_len);
            tcp->checksum = internet_checksum(ck, ck_len);
            free(ck);
        }

        /* -- IPv4 header ------------------------------------------------- */
        ip = (ipv4_header_t *)packet;
        ip->ver_ihl          = 0x45;                /* IPv4, IHL=5 -> 20 bytes  */
        ip->type_of_service  = 0;
        ip->total_length     = to_be16((uint16_t)total);
        ip->identification   = to_be16((uint16_t)(time(NULL) & 0xFFFF));
        ip->flags_fragoff    = to_be16(0x4000);     /* DF set, no fragmentation */
        ip->time_to_live     = 64;
        ip->protocol         = PROTO_TCP;
        ip->header_checksum  = 0;                   /* filled on the next line  */
        ip->source_address   = src_be;
        ip->dest_address     = dst_be;
        ip->header_checksum  = internet_checksum(ip, ip_len);

        printf("crafted packet, %zu bytes (IP %zu + TCP %zu + payload %zu)\n"
               "  %s:%u -> %s:%u  flags=0x%02X\n",
               total, ip_len, tcp_len, payload_len,
               src_text, src_port, dst_text, dst_port, tcp_flags);
        hexdump(packet, total);

        /* -- inject --------------------------------------------------------
         * This is the ONLY part that must touch the OS: a user program
         * cannot reach the NIC without the kernel. The calls below are the
         * BSD socket API — the same on Windows (Winsock), Linux, macOS.   */
#ifdef _WIN32
        {
            WSADATA wsa;
            if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
                fprintf(stderr, "error: WSAStartup failed\n");
                free(packet);
                return EXIT_FAILURE;
            }
        }
#endif
        {
            SOCKET sock = socket(AF_INET, SOCK_RAW, IPPROTO_RAW);
            struct sockaddr_in where;
#ifdef _WIN32
            DWORD on = 1;
#else
            int    on = 1;
#endif
            if (sock == INVALID_SOCKET) {
                perror("socket(AF_INET, SOCK_RAW, IPPROTO_RAW)");
                fprintf(stderr, "hint: raw sockets need root (Linux/macOS) "
                                "or Administrator (Windows).\n");
                free(packet);
                return EXIT_FAILURE;
            }

            /* WE already supply a full IP header — tell the kernel so */
            if (setsockopt(sock, IPPROTO_IP, IP_HDRINCL,
                           (const char *)&on, sizeof(on)) == SOCKET_ERROR) {
                perror("setsockopt(IP_HDRINCL)");
                closesocket(sock);
                free(packet);
                return EXIT_FAILURE;
            }

            memset(&where, 0, sizeof(where));
            where.sin_family      = AF_INET;
            where.sin_addr.s_addr = dst_be;  /* kernel routes by this     */
            where.sin_port        = 0;       /* ports live in TCP header  */

            if (sendto(sock, (const char *)packet, (int)total, 0,
                       (struct sockaddr *)&where, sizeof(where)) == SOCKET_ERROR) {
                perror("sendto");
                fprintf(stderr, "hint: the OS or the first router (uRPF) may "
                                "reject the chosen source address.\n");
                closesocket(sock);
                free(packet);
                return EXIT_FAILURE;
            }
            closesocket(sock);
        }
#ifdef _WIN32
        WSACleanup();
#endif
        printf("packet handed to the OS — verify with tcpdump / Wireshark\n");
        free(packet);
    }
    return EXIT_SUCCESS;
}
