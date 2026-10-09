import ipaddress, socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

TRACKING = {"gclid", "fbclid", "ref", "source"}

def canonicalize(url: str) -> tuple[str, str]:
    p = urlsplit(url.strip())
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    host = p.hostname.lower().rstrip(".")
    try: ip = ipaddress.ip_address(host)
    except ValueError: ip = None
    if ip and (not ip.is_global): raise ValueError("Private or reserved network address is not allowed")
    clean = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    normalized = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", urlencode(clean), ""))
    return host.removeprefix("www."), normalized

def assert_public_host(host: str):
    try: addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try: addresses = [ipaddress.ip_address(x[4][0]) for x in socket.getaddrinfo(host, None)]
        except OSError as exc: raise ValueError("Host could not be resolved") from exc
    if not addresses or any(not ip.is_global for ip in addresses): raise ValueError("Host resolves to a private or reserved address")

