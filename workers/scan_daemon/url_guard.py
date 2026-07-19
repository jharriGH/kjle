"""
SSRF guard for the WebSignalz scan daemon.

Co-tenancy risk: the daemon runs on RackNerd VPS 192.161.173.97 alongside:
  - DNC compliance service  (port 7070)
  - CC build dispatcher     (port 8091)

A public-facing form (ComplianceEnginez) feeds URLs into scan_jobs. Without this
guard, an attacker could submit http://127.0.0.1:7070 or http://localhost:8091 and
use our own scanner to probe and exfiltrate data from internal services.

Defense-in-depth: ComplianceEnginez validates at ingress; this module is the final
guard at the scanner itself. Do NOT remove or simplify these checks without
re-auditing the VPS co-tenancy topology.
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import urllib.parse

# Short timeout prevents a hostile/slow DNS from holding a worker slot.
_DNS_TIMEOUT_S = 5.0

# Cloud metadata endpoints that must never be reachable regardless of DNS.
_CLOUD_METADATA_HOSTS: frozenset[str] = frozenset({
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.goog",
})

# Suffixes that always indicate internal/local names.
_BLOCKED_SUFFIXES: tuple[str, ...] = (".localhost", ".local", ".internal")

# Reason strings that mean the target is unreachable (dead domain / DNS failure),
# as opposed to a genuine security rejection. Callers use this set to write
# scan_status='unreachable' instead of 'blocked'.
# dns_resolves_to_private is intentionally absent — that IS an SSRF attack vector.
UNREACHABLE_REASONS: frozenset[str] = frozenset({"dns_resolution_failed"})


def _ip_block_reason(addr_str: str) -> str | None:
    """
    Return a reason string if the IP is non-globally-routable, else None.
    addr_str must already be a valid IP string (no brackets, no scope ID).
    """
    ip = ipaddress.ip_address(addr_str)
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link_local"
    if ip.is_private:
        return "private_ip"
    if ip.is_reserved:
        return "reserved_ip"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    return None


def is_url_safe(url: str) -> tuple[bool, str]:
    """
    Return (True, "") if the URL is safe to scan, else (False, "<reason>").

    Reason strings (stable, machine-readable):
      bad_scheme, no_host, loopback, link_local, private_ip, reserved_ip,
      multicast, unspecified, blocked_hostname, cloud_metadata,
      dns_resolution_failed, dns_resolves_to_private.
    """
    parsed = urllib.parse.urlparse(url)

    # Scheme: only http and https
    if parsed.scheme not in ("http", "https"):
        return False, "bad_scheme"

    # hostname: lowercased, brackets stripped for IPv6, None if absent
    host = parsed.hostname
    if not host:
        return False, "no_host"

    host = host.rstrip(".")  # strip trailing dot (FQDN notation)

    # Explicit cloud metadata check (belt-and-suspenders before the IP check below)
    if host in _CLOUD_METADATA_HOSTS:
        return False, "cloud_metadata"

    # localhost exact match
    if host == "localhost":
        return False, "blocked_hostname"

    # Internal-network suffixes
    for suffix in _BLOCKED_SUFFIXES:
        if host.endswith(suffix):
            return False, "blocked_hostname"

    # Bare IP check: ipaddress.ip_address raises ValueError for hostnames
    try:
        ipaddress.ip_address(host)  # confirm it's an IP; ValueError means hostname
        reason = _ip_block_reason(host)
        if reason:
            return False, reason
        return True, ""  # globally-routable bare IP — DNS step not needed
    except ValueError:
        pass  # not a bare IP; fall through to DNS resolution

    # DNS resolution: resolve and reject if any address is non-global
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_DNS_TIMEOUT_S)
    try:
        try:
            addrinfos = socket.getaddrinfo(host, None)
        except (socket.gaierror, OSError):
            return False, "dns_resolution_failed"
    finally:
        socket.setdefaulttimeout(prev_timeout)

    if not addrinfos:
        return False, "dns_resolution_failed"

    for addrinfo in addrinfos:
        # addrinfo[4] is sockaddr: (ip, port) for IPv4, (ip, port, flow, scope) for IPv6
        addr = addrinfo[4][0].split("%")[0]  # strip IPv6 scope ID if present
        reason = _ip_block_reason(addr)
        if reason:
            return False, "dns_resolves_to_private"

    return True, ""


def check_redirect_chain(url: str, max_hops: int = 10) -> tuple[bool, str]:
    """
    Follow server-side HTTP redirects via Python http.client (no browser) and
    check each hop with is_url_safe() BEFORE following it.

    Returns (True, "") when every hop in the chain is safe.
    Returns (False, "redirect_to_<reason>") if any hop targets an unsafe address.

    This runs before the browser launches so the browser never connects to an
    internal host even when the initial URL resolves to an external safe server
    that 302-redirects to an internal address (the proven Slice 14 attack path).
    """
    current = url
    seen: set[str] = set()

    for _ in range(max_hops):
        if current in seen:
            return True, ""  # redirect loop; browser will time out naturally
        seen.add(current)

        safe, reason = is_url_safe(current)
        if not safe:
            return False, f"redirect_to_{reason}"

        parsed = urllib.parse.urlparse(current)
        netloc = parsed.netloc  # may include :port
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        try:
            if parsed.scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                    netloc, timeout=_DNS_TIMEOUT_S
                )
            else:
                conn = http.client.HTTPConnection(netloc, timeout=_DNS_TIMEOUT_S)

            conn.request(
                "HEAD", path,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            )
            resp = conn.getresponse()
            status = resp.status
            loc = resp.getheader("Location", "")
            conn.close()

            if status in (301, 302, 303, 307, 308) and loc:
                current = urllib.parse.urljoin(current, loc)
                continue
            return True, ""  # non-redirect or redirect with no Location

        except Exception:
            # Network error during preflight (timeout, refused connection, etc.)
            # Treat as safe: the browser will make its own attempt and will hit
            # the route handler + request-event monitor if anything is unsafe.
            return True, ""

    return True, ""  # too many hops; browser will give up on its own
