"""Official Arizona launcher directory, cached and fail-closed resolver."""
import ipaddress
import json
import os
import socket
import threading
import time
import urllib.request

SOURCE = 'https://api.arizona-five.com/launcher/servers'
CACHE_SECONDS = 600
_lock = threading.RLock()
_cache = {'when': 0, 'servers': [], 'error': None}


def normalize(address):
    return str(address or '').strip().lower().rstrip('.')


def extract_servers(payload):
    """Find server records recursively; supports root arrays and nested server lists."""
    result = []
    seen = set()
    def walk(obj, depth=0):
        if depth > 10: return
        if isinstance(obj, list):
            for child in obj: walk(child, depth+1)
        elif isinstance(obj, dict):
            host = normalize(obj.get('ip') or obj.get('host') or obj.get('address'))
            try: port = int(obj.get('port', 7777))
            except (TypeError, ValueError): port = 0
            if host and 0 < port < 65536 and len(host) < 254 and ('.' in host or ':' in host):
                # Require real host-ish data, no arbitrary text.
                if all(c.isalnum() or c in '.:-_' for c in host):
                    key = (host,port)
                    if key not in seen:
                        seen.add(key)
                        title = obj.get('name') or obj.get('title') or obj.get('serverName') or host.split('.')[0].capitalize()
                        if not isinstance(title,(str,int)): title=host
                        result.append({'host':host,'port':port,'name':str(title)[:100], 'key':host+':'+str(port)})
            for v in obj.values():
                if isinstance(v,(dict,list)): walk(v, depth+1)
    walk(payload)
    return result


def directory(force=False):
    with _lock:
        if not force and _cache['servers'] and time.monotonic()-_cache['when'] < CACHE_SECONDS:
            return list(_cache['servers'])
        if not force and not _cache['servers'] and time.monotonic()-_cache['when'] < 30:
            return []
        _cache['when'] = time.monotonic()
        try:
            req=urllib.request.Request(SOURCE,headers={'User-Agent':'ArizonaGroupFinder/1.1','Accept':'application/json'})
            with urllib.request.urlopen(req, timeout=7) as resp:
                raw=resp.read(2_000_001)
            if len(raw)>2_000_000: raise ValueError('directory too large')
            servers=extract_servers(json.loads(raw))
            if not servers: raise ValueError('No recognizable server records in official API')
            _cache.update({'servers':servers,'error':None})
        except Exception as exc:
            _cache['error']=str(exc)
            # Keep previously confirmed records during a temporary API outage.
        return list(_cache['servers'])


def resolve(address, port, records=None, dns_lookup=None):
    host=normalize(address)
    if not host or len(host)>253 or not all(c.isalnum() or c in '.:-_' for c in host):
        return None, 'invalid_address'
    try: port=int(port)
    except (TypeError,ValueError): return None,'invalid_port'
    if port<1 or port>65535: return None,'invalid_port'
    records = directory() if records is None else records
    exact = [r for r in records if normalize(r['host'])==host and r['port']==port]
    if len(exact)==1: return exact[0],None
    if len(exact)>1: return None,'ambiguous_match'
    try: ipaddress.ip_address(host)
    except ValueError: return None,'unknown_server'
    def default_dns(name):
        return {r[4][0] for r in socket.getaddrinfo(name,None,type=socket.SOCK_STREAM)}
    lookup = dns_lookup or default_dns
    matching=[]
    for rec in records:
        if rec['port'] != port: continue
        try:
            addresses=lookup(rec['host'])
            if host in addresses: matching.append(rec)
        except (OSError,ValueError): pass
    if len(matching)==1: return matching[0],None
    return None, 'ambiguous_match' if matching else 'unknown_server'
