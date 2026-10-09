#!/usr/bin/env python3
"""Deterministic macOS tool fixtures. No network traffic is generated."""
import json
import os
from pathlib import Path
import sys
import time

command = Path(sys.argv[0]).name
args = sys.argv[1:]
scenario = os.environ.get('NETWORK_TEST_SCENARIO', 'success')
with open(os.environ['NETWORK_TEST_CALLS'], 'a', encoding='utf-8') as log:
    log.write(json.dumps([command, *args]) + '\n')

if command == 'dscacheutil':
    if scenario == 'dns_timeout':
        time.sleep(30)
    elif scenario == 'dns_error':
        sys.exit(1)
    elif scenario != 'dns_failure':
        print('name: example.com\nip_address: 203.0.113.10\nipv6_address: 2001:db8::10')
elif command == 'nc':
    if scenario == 'tcp_timeout':
        time.sleep(30)
    if scenario == 'tcp_refused' or (scenario == 'first_address_refused' and args[-2] == '203.0.113.10'):
        print('nc: connect failed: Connection refused', file=sys.stderr)
        sys.exit(1)
    if scenario == 'tcp_reported_timeout':
        print('nc: connect failed: Operation timed out', file=sys.stderr)
        sys.exit(1)
    print('Connection succeeded!', file=sys.stderr)
elif command in ('ping', 'ping6'):
    if scenario in ('ping_blocked', 'first_repeat_failed'):
        print('3 packets transmitted, 0 packets received, 100.0% packet loss')
        sys.exit(2)
    print('3 packets transmitted, 3 packets received, 0.0% packet loss')
    print('round-trip min/avg/max/stddev = 1.000/2.125/3.000/0.000 ms')
elif command == 'route':
    if scenario == 'route_unknown':
        sys.exit(1)
    interface = 'utun4' if scenario == 'vpn' else 'en7'
    print(f'   gateway: 192.0.2.1\n interface: {interface}')
elif command == 'ipconfig':
    print('192.0.2.50')
elif command == 'ifconfig':
    print('en7: flags=8863<UP>\n inet6 2001:db8::50 prefixlen 64')
elif command == 'curl':
    code, rc, verification = '200', 0, '0'
    if scenario == 'certificate_expired':
        print('curl: (60) SSL certificate problem: certificate has expired', file=sys.stderr)
        code, rc, verification = '000', 60, '10'
    elif scenario == 'tls_handshake':
        print('curl: (35) SSL connect error', file=sys.stderr)
        code, rc = '000', 35
    elif scenario == 'curl_timeout':
        print('curl: (28) Operation timed out', file=sys.stderr)
        code, rc = '000', 28
    elif scenario == 'partial_response':
        print('curl: (18) transfer closed with outstanding data', file=sys.stderr)
        rc = 18
    elif scenario == 'web_empty':
        code = '000'
    elif scenario == 'json_error':
        print('curl: (60) Certificate "bad" \\ authority\nsecond line: café', file=sys.stderr)
        code, rc, verification = '000', 60, '10'
    elif scenario.startswith('http_'):
        code = scenario[5:]
    elif scenario == 'first_repeat_failed':
        with open(os.environ['NETWORK_TEST_CALLS'], encoding='utf-8') as log:
            count = sum(json.loads(line)[0] == 'curl' for line in log)
        if count == 1:
            code = '500'
    remote_ip = '203.0.113.10'
    if '--resolve' in args:
        resolve = args[args.index('--resolve') + 1]
        remote_ip = resolve.split(':', 2)[2].strip('[]')
    else:
        from urllib.parse import urlsplit
        remote_ip = urlsplit(args[args.index('--url') + 1]).hostname
    print(f'\n{code}|{remote_ip}|0.001|0.010|0.020|0.030|0.040|{verification}')
    sys.exit(rc)
