"""Run with: python3 -m unittest discover -s tests -v"""
import json
import os
from pathlib import Path
import pty
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ('dscacheutil', 'nc', 'curl', 'route', 'ping', 'ping6', 'ipconfig', 'ifconfig', 'nslookup')


class NetworkTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='network-test-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.calls_file = self.directory / 'calls.jsonl'
        for command in TOOLS:
            (self.directory / command).symlink_to(ROOT / 'tests' / 'mock_network_tools.py')
        self.env = {**os.environ, 'PATH': f'{self.directory}:{os.environ["PATH"]}',
                    'NETWORK_TEST_CALLS': str(self.calls_file)}

    def run_cli(self, *args, scenario='success', launcher=False):
        env = {**self.env, 'NETWORK_TEST_SCENARIO': scenario}
        command = ROOT / ('network-test.command' if launcher else 'network-test')
        # Command-mode tests must not inherit the terminal running the suite.
        result = subprocess.run([str(command), *args], cwd=self.directory, env=env,
                                stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=15)
        return result

    def calls(self, command):
        if not self.calls_file.exists():
            return []
        return [call[1:] for line in self.calls_file.read_text().splitlines()
                if (call := json.loads(line))[0] == command]

    def result(self, *args, scenario='success', expected=0):
        result = self.run_cli('--json', *args, scenario=scenario)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def test_success_has_separate_results_and_timings(self):
        report = self.result('example.com', '443')
        self.assertEqual(report['result'], 'passed')
        self.assertEqual(report['dns']['addresses'], ['203.0.113.10', '2001:db8::10'])
        self.assertEqual(report['tcp']['state'], 'passed')
        self.assertEqual(report['web']['tls'], 'verified')
        self.assertEqual(report['web']['remote_ip'], '203.0.113.10')
        self.assertEqual(report['web']['timings_seconds']['total'], 0.04)
        self.assertEqual(report['ping']['packet_loss_percent'], 0.0)
        self.assertEqual(report['ping']['average_ms'], 2.125)
        self.assertEqual(report['schema_version'], 1)

    def test_path_query_fragment_and_protocol(self):
        url = 'http://example.com:8080/health?ready=1&mode=full#details'
        report = self.result(url)
        self.assertEqual(report['target']['url'], url.split('#')[0])
        self.assertEqual(report['web']['tls'], 'skipped')
        calls = self.calls('curl')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], url.split('#')[0])
        self.assertEqual(calls[0][0], '-q')
        self.assertIn('--resolve', calls[0])
        self.assertEqual(calls[0][calls[0].index('--noproxy') + 1], '*')

    def test_fragment_without_path(self):
        report = self.result('https://example.com#details')
        self.assertEqual(report['target']['host'], 'example.com')
        self.assertEqual(report['target']['url'], 'https://example.com:443/')

    def test_query_without_path(self):
        report = self.result('https://example.com?ready=1')
        self.assertEqual(report['target']['url'], 'https://example.com:443/?ready=1')

    def test_port_override_and_leading_zeroes(self):
        report = self.result('https://example.com:8443/health', '00443')
        self.assertEqual(report['target']['port'], 443)
        self.assertEqual(report['target']['url'], 'https://example.com:443/health')

    def test_certificate_failure_is_not_success_or_http_fallback(self):
        report = self.result('https://example.com/health', scenario='certificate_expired', expected=1)
        self.assertEqual(report['tcp']['state'], 'passed')
        self.assertEqual(report['web']['tls'], 'failed')
        self.assertIsNone(report['web']['http_status'])
        self.assertIn('Certificate verification failed', report['summary'])
        self.assertNotIn('Everything looks good', report['summary'])
        self.assertEqual(len(self.calls('curl')), 1)

    def test_tls_handshake_failure(self):
        report = self.result('example.com', '443', scenario='tls_handshake', expected=1)
        self.assertEqual(report['web']['tls'], 'failed')

    def test_http_errors_are_reachable_but_failed(self):
        for code in ('401', '403', '404', '500', '503'):
            with self.subTest(code=code):
                report = self.result('https://example.com/health', scenario=f'http_{code}', expected=1)
                self.assertEqual(report['web']['http_status'], int(code))
                self.assertEqual(report['tcp']['state'], 'passed')
                self.assertEqual(report['web']['state'], 'failed')

    def test_redirect_is_reported_without_following(self):
        report = self.result('https://example.com/', scenario='http_302')
        self.assertEqual(report['web']['http_status'], 302)
        self.assertNotIn('-L', self.calls('curl')[-1])

    def test_partial_response_does_not_pass(self):
        report = self.result('https://example.com/', scenario='partial_response', expected=1)
        self.assertEqual(report['web']['http_status'], 200)
        self.assertEqual(report['web']['state'], 'failed')
        self.assertEqual(report['web']['tls'], 'verified')

    def test_web_timeout(self):
        report = self.result('https://example.com/', scenario='curl_timeout', expected=1)
        self.assertIn('timed out', report['web']['error'])

    def test_web_without_status_is_inconclusive(self):
        report = self.result('https://example.com/', scenario='web_empty', expected=3)
        self.assertEqual(report['web']['state'], 'unknown')

    def test_dns_failure_skips_network_checks(self):
        report = self.result('missing.example', '443', scenario='dns_failure', expected=1)
        self.assertEqual(report['dns']['state'], 'failed')
        self.assertEqual(report['tcp']['state'], 'skipped')
        self.assertEqual(self.calls('nc'), [])
        self.assertEqual(self.calls('curl'), [])

    def test_dns_command_failure(self):
        report = self.result('missing.example', scenario='dns_error', expected=1)
        self.assertIn('exit code 1', report['dns']['error'])

    def test_dns_timeout_is_bounded(self):
        started = time.monotonic()
        report = self.result('--timeout', '1', 'example.com', scenario='dns_timeout', expected=1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn('timed out', report['dns']['error'])

    def test_tcp_refusal_and_timeout(self):
        for scenario in ('tcp_refused', 'tcp_reported_timeout'):
            with self.subTest(scenario=scenario):
                report = self.result('example.com', '443', scenario=scenario, expected=1)
                self.assertEqual(len(report['tcp']['attempts']), 2)
                self.assertEqual(report['web']['state'], 'skipped')
                self.assertEqual(self.calls('curl'), [])

    def test_tcp_hang_is_bounded(self):
        started = time.monotonic()
        report = self.result('--timeout', '1', '127.0.0.1', '5432', scenario='tcp_timeout', expected=1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn('Timed out', report['tcp']['error'])

    def test_second_address_uses_matching_route_ping_and_tls(self):
        report = self.result('example.com', '443', scenario='first_address_refused')
        self.assertEqual(report['route']['target_ip'], '2001:db8::10')
        self.assertEqual(report['web']['remote_ip'], '2001:db8::10')
        self.assertEqual(self.calls('route')[-1], ['-n', 'get', '-inet6', '2001:db8::10'])
        self.assertEqual(self.calls('ping6')[-1][-1], '2001:db8::10')
        self.assertEqual(self.calls('curl')[-1][self.calls('curl')[-1].index('--resolve') + 1], 'example.com:443:[2001:db8::10]')

    def test_ipv6_literal_uses_ping6_and_bracketed_web_url(self):
        report = self.result('[2001:db8::1]:443')
        self.assertEqual(report['target']['url'], 'https://[2001:db8::1]:443/')
        self.assertEqual(self.calls('dscacheutil'), [])
        self.assertEqual(self.calls('ping'), [])
        self.assertEqual(self.calls('ping6')[-1][-1], '2001:db8::1')
        self.assertIn('-6', self.calls('nc')[-1])

    def test_bare_ipv6_and_scoped_ipv6(self):
        report = self.result('2001:db8::1', '5432')
        self.assertEqual(report['target']['host'], '2001:db8::1')
        report = self.result('http://[fe80::1%25en7]/health')
        self.assertEqual(report['target']['host'], 'fe80::1%en7')
        self.assertEqual(report['target']['url'], 'http://[fe80::1%25en7]:80/health')

    def test_family_options_filter_resolved_addresses(self):
        for family, ip in (('4', '203.0.113.10'), ('6', '2001:db8::10')):
            with self.subTest(family=family):
                report = self.result(f'--ipv{family}', 'example.com', '443')
                self.assertEqual(report['dns']['addresses'], [ip])
                self.assertIn(f'-{family}', self.calls('nc')[-1])
                self.assertIn(f'-{family}', self.calls('curl')[-1])

    def test_family_mismatch_is_invalid(self):
        self.assertEqual(self.run_cli('--ipv6', '127.0.0.1').returncode, 2)
        self.assertEqual(self.run_cli('--ipv4', '2001:db8::1').returncode, 2)
        self.assertEqual(self.run_cli('--ipv4', '--ipv6', 'example.com').returncode, 2)

    def test_quick_skips_ping(self):
        report = self.result('--quick', 'example.com', '443')
        self.assertEqual(report['ping']['state'], 'skipped')
        self.assertEqual(self.calls('ping'), [])
        self.assertEqual(self.calls('ping6'), [])

    def test_blocked_ping_does_not_fail_a_working_service(self):
        report = self.result('example.com', '5432', scenario='ping_blocked')
        self.assertEqual(report['ping']['state'], 'unanswered')
        self.assertEqual(report['web']['state'], 'skipped')
        self.assertIn('Authentication and application behavior were not tested', report['summary'])

    def test_no_port_has_clear_success_and_inconclusive_results(self):
        report = self.result('example.com')
        self.assertIn('No service port was tested', report['summary'])
        self.result('example.com', scenario='ping_blocked', expected=3)
        self.result('--quick', 'example.com', expected=3)

    def test_nonweb_protocol_is_not_probed_as_http(self):
        for target in ('ssh://example.com', 'ftp://example.com', 'tcp://example.com:443'):
            with self.subTest(target=target):
                report = self.result(target)
                self.assertEqual(report['web']['state'], 'skipped')
        self.assertEqual(self.calls('curl'), [])

    def test_target_route_uses_actual_interface(self):
        report = self.result('example.com', '5432')
        self.assertEqual(report['route']['interface'], 'en7')
        self.assertEqual(report['route']['interface_address'], '192.0.2.50')
        self.assertEqual(self.calls('ipconfig')[-1], ['getifaddr', 'en7'])
        report = self.result('example.com', '443', scenario='vpn')
        self.assertTrue(report['route']['tunnel_interface'])

    def test_route_inspection_failure_does_not_override_connection(self):
        report = self.result('example.com', '5432', scenario='route_unknown')
        self.assertEqual(report['route']['state'], 'unknown')

    def test_json_escapes_errors(self):
        report = self.result('example.com', '443', scenario='json_error', expected=1)
        self.assertIn('"bad"', report['web']['error'])
        self.assertIn('\\ authority', report['web']['error'])
        self.assertIn('\nsecond line: café', report['web']['error'])

    def test_repeat_writes_json_lines_and_aggregates_failure(self):
        result = self.run_cli('--json', '--repeat', '2', '--interval', '0', 'example.com', '443', scenario='first_repeat_failed')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['run'] for r in reports], [1, 2])
        self.assertEqual([r['result'] for r in reports], ['failed', 'passed'])

    def test_repeat_has_fresh_state(self):
        result = self.run_cli('--json', '--repeat', '2', '--interval', '0', 'example.com', '443')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        reports = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(reports), 2)
        self.assertEqual([len(r['tcp']['attempts']) for r in reports], [1, 1])

    def test_output_matches_stdout_and_preserves_failure_exit(self):
        destination = self.directory / 'report with spaces.jsonl'
        result = self.run_cli('--json', '--output', str(destination), 'example.com', '443', scenario='certificate_expired')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(destination.read_text(), result.stdout)
        self.assertEqual(json.loads(result.stdout)['result'], 'failed')

    def test_invalid_target_does_not_truncate_report(self):
        destination = self.directory / 'existing.txt'
        destination.write_text('keep me')
        result = self.run_cli('--output', str(destination), 'invalid host')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(destination.read_text(), 'keep me')

    def test_unwritable_report_is_setup_error(self):
        result = self.run_cli('--output', str(self.directory), 'example.com', '443')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Could not write report', result.stderr)
        self.assertEqual(self.calls('dscacheutil'), [])

    def test_interactive_launcher_and_saved_report(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        destination = self.directory / 'interactive.txt'
        process = subprocess.Popen([str(ROOT / 'network-test.command'), '--output', str(destination)],
                                   stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=self.directory, env=self.env, text=True)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        os.write(master, b'example.com\n443\n\n')
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertIn('TCP connected', stdout)
        self.assertIn('Bye!', stdout)
        self.assertEqual(destination.read_text(), stdout)

    def test_invalid_input_and_options_do_not_start_checks(self):
        cases = [('',), ('example .com',), ('-malicious',), ('999.1.2.3',), ('2001:db8::gg',),
                 ('example..com',), ('example.com:',), ('example.com', '65536'),
                 ('example.com', '0'), ('example.com', '$(touch injected)'),
                 ('[2001:db8::1',), ('[2001:db8::1]garbage',),
                 ('https://2001:db8::1/',), ('https://user:pass@example.com',),
                 ('gopher://example.com',), ('--timeout', '0', 'example.com'),
                 ('--repeat', '0', 'example.com'), ('--interval', '-1', 'example.com'),
                 ('--timeout',), ('--unknown',), ('example.com', '443', 'extra')]
        for args in cases:
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.calls('dscacheutil'), [])
        self.assertEqual(self.calls('nc'), [])
        self.assertFalse((self.directory / 'injected').exists())

    def test_help_and_missing_address(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('--timeout', result.stdout)
        self.assertEqual(self.run_cli('--json').returncode, 2)
        self.assertEqual(self.run_cli().returncode, 2)

    def test_launcher_works_from_another_directory(self):
        result = self.run_cli('--json', '127.0.0.1', '5432', launcher=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['target']['host'], '127.0.0.1')

    def test_text_report_and_share_command_preserve_get_url(self):
        result = self.run_cli('http://example.com/health?ready=1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('TCP connected', result.stdout)
        self.assertIn('Interface: en7', result.stdout)
        self.assertNotIn('curl -sI', result.stdout)
        self.assertIn('GET http://example.com:80/health?ready=1', result.stdout)
        self.assertIn('\n\n  macOS / Linux (Terminal / Bash):\n', result.stdout)
        self.assertNotIn('macOS (Terminal):', result.stdout)
        self.assertNotIn('Linux (Bash):', result.stdout)
        self.assertIn('Windows (PowerShell):', result.stdout)
        self.assertIn('Note: Checks TCP and ping only. TLS and HTTP are not tested.', result.stdout)
        self.assertNotIn('\x1b', result.stdout)
        self.assertTrue(result.stdout.startswith('\nTesting '))
        self.assertFalse(any(line.startswith('\\n') for line in result.stdout.splitlines()))
        for heading in ('DNS lookup', 'TCP connection on port 80', 'Route to 203.0.113.10',
                        'Ping 203.0.113.10', 'HTTP request', 'Summary', 'Share test commands'):
            self.assertIn(f'\n\n{heading}\n', result.stdout)

    def test_shared_command_quotes_url_and_disables_curl_globbing(self):
        url = "http://example.com:80/health?filter[0]=a&value=$(touch${IFS}injected)&quote='x'"
        result = self.run_cli(url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shared_commands = [line.strip() for line in result.stdout.splitlines()
                           if line.strip().startswith('nslookup ')]
        self.assertEqual(len(shared_commands), 1)
        shared = shared_commands[0]
        for shell in ('zsh', 'bash'):
            with self.subTest(shell=shell):
                repeated = subprocess.run([shell, '-c', shared], cwd=self.directory, env=self.env, text=True, capture_output=True, timeout=15)
                self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                self.assertEqual(self.calls('curl')[-1][-1], url)
                self.assertIn('--globoff', self.calls('curl')[-1])
                self.assertFalse((self.directory / 'injected').exists())

    def test_shared_hostname_resolves_again_without_pinning_ip_or_family(self):
        result = self.run_cli('example.com', '443', scenario='first_address_refused')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shared = result.stdout.split('\nShare test commands\n', 1)[1]
        self.assertIn('nslookup example.com; ping -c 3 example.com; nc -vz -w 8 example.com 443;', shared)
        self.assertIn("Test-NetConnection -ComputerName 'example.com' -Port 443", shared)
        self.assertNotIn('--resolve', shared)
        self.assertNotIn('203.0.113.10', shared)
        self.assertNotIn('2001:db8::10', shared)
        self.assertNotIn(' -6 ', shared)

    def test_shared_hostname_preserves_explicit_address_family(self):
        for family in ('4', '6'):
            with self.subTest(family=family):
                result = self.run_cli(f'--ipv{family}', 'example.com', '443')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                shared = result.stdout.split('\nShare test commands\n', 1)[1]
                self.assertIn(f'nc -{family} -vz -w 8 example.com 443', shared)
                self.assertIn(f"--noproxy '*' -{family}", shared)
                self.assertIn(f'{"ping6" if family == "6" else "ping"} -c 3 example.com', shared)
                linux_shared = shared.split('Linux (Bash):\n', 1)[1].split('Windows (PowerShell):', 1)[0]
                self.assertIn(f'ping -{family} -c 3 example.com', linux_shared)
                self.assertNotIn('ping6', linux_shared)
                self.assertNotIn('--resolve', shared)

    def test_shared_numeric_ipv6_stays_numeric(self):
        result = self.run_cli('[2001:db8::1]:443')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shared = result.stdout.split('\nShare test commands\n', 1)[1]
        self.assertNotIn('nslookup', shared)
        self.assertIn('ping6 -c 3 2001:db8::1', shared)
        self.assertIn('ping -6 -c 3 2001:db8::1', shared)
        self.assertIn('nc -6 -vz -w 8 2001:db8::1 443', shared)
        self.assertIn("Test-NetConnection -ComputerName '2001:db8::1' -Port 443", shared)

    def test_shared_quick_ipv6_combines_identical_commands(self):
        result = self.run_cli('--quick', '--ipv6', 'example.com', '443')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shared = result.stdout.split('\nShare test commands\n', 1)[1]
        self.assertIn('macOS / Linux (Terminal / Bash):', shared)
        self.assertEqual(shared.count('nslookup example.com;'), 1)
        self.assertIn('nc -6 -vz -w 8 example.com 443', shared)
        self.assertNotIn('ping -', shared)
        self.assertNotIn('ping6', shared)


if __name__ == '__main__':
    unittest.main()
