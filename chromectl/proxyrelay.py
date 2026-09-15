#!/usr/bin/env python3
"""
A tiny authenticating proxy relay, for `chromectl start --proxy`.

Chrome accepts `--proxy-server=...` but has no way to take *credentials* on the
command line: it pops an auth dialog, which is useless headless. So when the
proxy needs a username/password we launch this relay on 127.0.0.1, point Chrome
at it, and it adds the credentials on the way upstream.

    python -m chromectl.proxyrelay --listen 9422 --upstream http://user:pass@host:8080

Speaks plain HTTP proxy to Chrome (CONNECT tunnels + absolute-form requests);
upstream may be an HTTP proxy or a SOCKS5 proxy. Stdlib only.
"""
import argparse
import base64
import os
import selectors
import socket
import struct
import sys
import threading
from urllib.parse import urlsplit, unquote

BUFSIZE = 65536
CONNECT_TIMEOUT = 30


def parse_proxy(url, user=None, password=None):
    """'socks5://u:p@host:1080' → dict(scheme, host, port, user, password)."""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "socks5h":            # curl's remote-DNS spelling; same thing here
        scheme = "socks5"
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise ValueError(f"unsupported proxy scheme {parts.scheme!r} "
                         "(use http, https, socks4 or socks5)")
    if not parts.hostname:
        raise ValueError(f"proxy has no host: {url!r}")
    port = parts.port or (1080 if scheme.startswith("socks") else 8080)
    return {"scheme": scheme, "host": parts.hostname, "port": port,
            "user": user if user is not None else (unquote(parts.username) if parts.username else None),
            "password": password if password is not None else (unquote(parts.password) if parts.password else None)}


def proxy_str(p, redact=True):
    """Render a parsed proxy back to a URL, with the password masked by default."""
    auth = ""
    if p.get("user"):
        secret = "***" if redact else (p.get("password") or "")
        auth = f"{p['user']}:{secret}@" if p.get("password") else f"{p['user']}@"
    return f"{p['scheme']}://{auth}{p['host']}:{p['port']}"


# --------------------------------------------------------------------------
# upstream dialers — return a socket already tunnelled to (host, port)
# --------------------------------------------------------------------------
def _dial_http(up, host, port):
    s = socket.create_connection((up["host"], up["port"]), CONNECT_TIMEOUT)
    req = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
    if up.get("user"):
        token = base64.b64encode(f"{up['user']}:{up.get('password') or ''}".encode()).decode()
        req.append(f"Proxy-Authorization: Basic {token}")
    req.append("Proxy-Connection: Keep-Alive")
    s.sendall(("\r\n".join(req) + "\r\n\r\n").encode())
    head = _read_headers(s)
    status = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if " 200" not in status:
        s.close()
        raise OSError(f"upstream proxy refused CONNECT: {status.strip()}")
    return s


def _dial_socks5(up, host, port):
    s = socket.create_connection((up["host"], up["port"]), CONNECT_TIMEOUT)
    try:
        if up.get("user"):
            s.sendall(b"\x05\x02\x00\x02")
        else:
            s.sendall(b"\x05\x01\x00")
        ver, method = _recv_exact(s, 2)
        if ver != 5:
            raise OSError("not a SOCKS5 proxy")
        if method == 0x02:
            u = (up.get("user") or "").encode()
            p = (up.get("password") or "").encode()
            if len(u) > 255 or len(p) > 255:
                raise OSError("SOCKS5 username/password too long (max 255 bytes)")
            s.sendall(b"\x01" + bytes([len(u)]) + u + bytes([len(p)]) + p)
            _, status = _recv_exact(s, 2)
            if status != 0:
                raise OSError("SOCKS5 proxy rejected the username/password")
        elif method != 0x00:
            raise OSError("SOCKS5 proxy wants an auth method we don't speak"
                          if method != 0xFF else "SOCKS5 proxy rejected our auth methods")
        h = host.encode() if host.isascii() else host.encode("idna")
        if len(h) > 255:
            raise OSError("hostname too long for SOCKS5")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
        rep = _recv_exact(s, 4)
        if rep[1] != 0:
            raise OSError(f"SOCKS5 CONNECT failed (code {rep[1]})")
        atyp = rep[3]                       # drain the bound address
        if atyp == 1:
            _recv_exact(s, 4 + 2)
        elif atyp == 3:
            _recv_exact(s, _recv_exact(s, 1)[0] + 2)
        elif atyp == 4:
            _recv_exact(s, 16 + 2)
        return s
    except Exception:
        s.close()
        raise


def dial(up, host, port):
    if up["scheme"] == "socks5":
        return _dial_socks5(up, host, port)
    return _dial_http(up, host, port)


# --------------------------------------------------------------------------
# socket helpers
# --------------------------------------------------------------------------
def _recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise OSError("upstream closed the connection mid-handshake")
        buf += chunk
    return buf


def _read_headers(s, cap=65536):
    """Read up to and including the blank line that ends a request/status head."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(BUFSIZE)
        if not chunk:
            break
        buf += chunk
        if len(buf) > cap:
            raise OSError("header block too large")
    return buf


def _pump(a, b):
    """Shovel bytes both ways until either side hangs up."""
    sel = selectors.DefaultSelector()
    sel.register(a, selectors.EVENT_READ, b)
    sel.register(b, selectors.EVENT_READ, a)
    try:
        while True:
            for key, _ in sel.select(timeout=300):
                try:
                    data = key.fileobj.recv(BUFSIZE)
                except OSError:
                    return
                if not data:
                    return
                try:
                    key.data.sendall(data)
                except OSError:
                    return
    finally:
        sel.close()


# --------------------------------------------------------------------------
# client side: a minimal HTTP proxy
# --------------------------------------------------------------------------
def _rewrite_request(head, host, port):
    """Absolute-form → origin-form, drop hop-by-hop proxy headers, force one
    request per connection (so every request gets re-authed upstream)."""
    lines = head.split(b"\r\n")
    method, target, version = lines[0].split(b" ", 2)
    if target.startswith(b"http://") or target.startswith(b"https://"):
        rest = target.split(b"//", 1)[1]
        slash = rest.find(b"/")
        path = rest[slash:] if slash >= 0 else b"/"
    else:
        path = target
    out = [b" ".join([method, path, version])]
    seen_host = False
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith(b"proxy-connection:") or low.startswith(b"proxy-authorization:"):
            continue
        if low.startswith(b"connection:"):
            continue
        if low.startswith(b"host:"):
            seen_host = True
        out.append(ln)
    if not seen_host:
        hostline = f"Host: {host}" + (f":{port}" if port != 80 else "")
        out.insert(1, hostline.encode())
    # keep-alive would let later requests on this socket skip our auth injection
    out.insert(1, b"Connection: close")
    return b"\r\n".join(out)


def handle(client, up):
    upstream = None
    try:
        head = _read_headers(client)
        if not head:
            return
        first = head.split(b"\r\n", 1)[0]
        method, target = first.split(b" ")[:2]
        if method.upper() == b"CONNECT":
            host, _, port = target.decode().rpartition(":")
            upstream = dial(up, host, int(port))
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            _pump(client, upstream)
        else:
            parts = urlsplit(target.decode("latin-1"))
            if not parts.hostname:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            port = parts.port or 80
            if up["scheme"] == "socks5":
                upstream = _dial_socks5(up, parts.hostname, port)
                upstream.sendall(_rewrite_request(head.split(b"\r\n\r\n")[0], parts.hostname, port)
                                 + b"\r\n\r\n" + head.split(b"\r\n\r\n", 1)[1])
            else:                                  # plain HTTP through an HTTP proxy
                upstream = socket.create_connection((up["host"], up["port"]), CONNECT_TIMEOUT)
                hdr, _, body = head.partition(b"\r\n\r\n")
                lines = [ln for ln in hdr.split(b"\r\n")
                         if not ln.lower().startswith((b"proxy-authorization:", b"proxy-connection:"))]
                if up.get("user"):
                    token = base64.b64encode(
                        f"{up['user']}:{up.get('password') or ''}".encode()).decode()
                    lines.insert(1, f"Proxy-Authorization: Basic {token}".encode())
                upstream.sendall(b"\r\n".join(lines) + b"\r\n\r\n" + body)
            _pump(client, upstream)
    except Exception as e:
        try:
            client.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{e}".encode())
        except OSError:
            pass
    finally:
        for s in (client, upstream):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


def serve(listen_port, upstream, bind="127.0.0.1"):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, listen_port))
    srv.listen(128)
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            continue
        threading.Thread(target=handle, args=(client, upstream), daemon=True).start()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="chromectl.proxyrelay",
                                 description="authenticating proxy relay for chromectl")
    ap.add_argument("--listen", type=int, required=True, help="local port to listen on")
    ap.add_argument("--bind", default="127.0.0.1", help="local bind address")
    ap.add_argument("--upstream", required=True, help="scheme://[user:pass@]host:port")
    ap.add_argument("--user", help="upstream username (overrides the URL)")
    ap.add_argument("--password", help="upstream password (overrides the URL)")
    a = ap.parse_args(argv)
    # chromectl passes credentials this way: argv is readable by every user via `ps`
    user = a.user or os.environ.get("CHROMECTL_PROXY_USER")
    password = a.password or os.environ.get("CHROMECTL_PROXY_PASSWORD")
    try:
        up = parse_proxy(a.upstream, user, password)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    serve(a.listen, up, a.bind)


if __name__ == "__main__":
    sys.exit(main() or 0)
