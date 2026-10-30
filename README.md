# godstress

Universal bypass tool: origin discovery + WAF + anti-bot + proxy + firewall + rate limit bypass.

## Bypass methods

### Origin discovery
- cert transparency (crt.sh)
- subdomain scan (origin, direct, backend, api, mail, ftp, cpanel, webmail, mc, play, node, panel, game, srv, dynmap, map)
- SPF/DKIM/DMARC records
- SecurityTrails API (if key provided)
- Dynmap/BlueMap leak detection
- Discord webhook leak detection

### Cloudflare Spectrum bypass
- detects CF IP ranges
- skips CF IPs, floods origin directly
- Dynmap/BlueMap on origin IP (common leak)

### WAF bypass
- spoofed headers (X-Forwarded-For, X-Real-IP, CF-Connecting-IP, True-Client-IP)
- 5 rotating User-Agents
- randomized accept/language/encoding

### Anti-bot bypass (Minecraft)
- spoofed player names (real-looking)
- randomized protocol versions
- random delays between joins
- login state switching

### Firewall bypass
- SYN flood (conntrack exhaustion)
- IP fragmentation flood (reassembly buffer exhaustion)

### Rate limit bypass
- slow requests (partial data, long delays)
- distributed across IPs

### Proxy bypass
- direct origin hit with Host header = domain

## Usage

```bash
# discover origin only
python3 godstress.py example.com --discover

# check Dynmap/BlueMap leak
python3 godstress.py example.com --dynmap

# full bypass pipeline (discover + verify + flood)
python3 godstress.py example.com -p 25565 -c 1000 -d 300 -m mc_login --bypass --confirm

# flood known origin
python3 godstress.py example.com -p 25565 -c 1000 -d 300 -m mc_login syn slow --origin ORIGIN_IP --confirm

# L7 bypass on origin
python3 godstress.py example.com -p 443 -c 2000 -d 300 -m l7 syn slow --origin ORIGIN_IP --ssl --confirm
```

## Install
```bash
pip install aiohttp
# optional
pip install uvloop dnspython
```
