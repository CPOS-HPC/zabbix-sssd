import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
loader = importlib.machinery.SourceFileLoader('monitor', str(ROOT / 'scripts/sssd-monitor.py'))
spec = importlib.util.spec_from_loader(loader.name, loader)
monitor = importlib.util.module_from_spec(spec)
loader.exec_module(monitor)

class ProbeTest(unittest.TestCase):
    def test_pam_requires_one_unambiguous_success(self):
        self.assertTrue(monitor.pam_success('testing pam_acct_mgmt\npam_acct_mgmt: Success\n'))
        for output in ('pam_acct_mgmt: System error\n', 'pam_acct_mgmt: Permission denied\n',
                       'other message\n', 'pam_acct_mgmt: Success but failed\n',
                       'pam_acct_mgmt: Success\npam_acct_mgmt: System error\n'):
            self.assertFalse(monitor.pam_success(output))

    def test_real_subprocess_reads_stderr_forces_locale_and_has_no_input(self):
        script = ('import os,sys; assert os.environ["LC_ALL"] == "C"; '
                  'assert os.environ["LANG"] == "C"; assert sys.stdin.read() == ""; '
                  'sys.stderr.write("pam_acct_mgmt: Success\\n")')
        with patch.dict(os.environ, {'LC_ALL': 'zh_TW.UTF-8', 'LANG': 'zh_TW.UTF-8'}):
            rc, out, latency, error = monitor.run_command([sys.executable, '-c', script], 2)
        self.assertEqual(rc, 0)
        self.assertIsNone(error)
        self.assertTrue(monitor.pam_success(out))
        self.assertGreaterEqual(latency, 0)

    def test_timeout_discards_partial_success(self):
        script = 'import time; print("pam_acct_mgmt: Success", flush=True); time.sleep(10)'
        rc, out, latency, error = monitor.run_command([sys.executable, '-c', script], .1)
        self.assertEqual((rc, out, error), (124, '', 'timeout'))
        self.assertLess(latency, 2000)

    def test_timeout_also_kills_pam_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'child-survived'
            child = 'import time; time.sleep(.4); open({!r}, "w").close()'.format(str(marker))
            parent = ('import subprocess,sys,time; '
                      'subprocess.Popen([sys.executable,"-c",{!r}]); time.sleep(10)').format(child)
            result = monitor.run_command([sys.executable, '-c', parent], .15)
            time.sleep(.5)
            self.assertEqual(result[3], 'timeout')
            self.assertFalse(marker.exists())

    def test_missing_command(self):
        result = monitor.run_command(['/nonexistent/sssd-monitor-test'], 1)
        self.assertEqual((result[0], result[3]), (127, 'exec_error'))

    def test_nss_canonical_name_and_record_validation(self):
        self.assertTrue(monitor.nss_success('sssd_monitor@ipa.example.com:*:1001:1001::/home/monitor:/bin/bash\n'))
        for output in ('sssd_monitor:broken', '', 'user:*:uid:gid::/:/bin/sh',
                       'a:*:1:1::/:/bin/sh\nb:*:2:2::/:/bin/sh\n'):
            self.assertFalse(monitor.nss_success(output))

    def health(self, pam=(0, 'pam_acct_mgmt: Success\n', 42, None),
               domain=(0, 'Online status: Online\n', 1, None), nss_rc=0):
        replies = [(0, '', 1, None), domain,
                   (nss_rc, 'sssd_monitor@ipa.example.com:*:1001:1001::/home/monitor:/bin/bash\n', 1, None), pam]
        with patch.object(monitor, 'run_command', side_effect=replies) as run:
            result = monitor.healthcheck('ipa.example.com', 'sssd_monitor')
        self.assertEqual(run.call_args_list[2][0][0][1:3], ['-s', 'sss'])
        self.assertEqual(run.call_args_list[3][0][0][1:],
                         ['user-checks', '-a', 'acct', '-s', 'sshd', 'sssd_monitor'])
        self.assertEqual([call[0][1] for call in run.call_args_list], [2, 4, 4, 8])
        return result

    def test_healthy_client(self):
        data = self.health()
        self.assertEqual([data[k] for k in ('service_up', 'domain_status', 'nss_ok', 'pam_ok')], [1, 1, 1, 1])
        self.assertEqual((data['pam_latency_ms'], data['pam_result']), (42, 'success'))

    def test_zero_exit_does_not_override_pam_denial(self):
        data = self.health(pam=(0, 'pam_acct_mgmt: Permission denied\n', 10, None))
        self.assertEqual((data['pam_ok'], data['pam_result']), (0, 'denied_or_error'))

    def test_pam_errors_and_nonzero_exit_never_healthy(self):
        for rc, error in ((124, 'timeout'), (127, 'exec_error'), (1, None)):
            data = self.health(pam=(rc, 'pam_acct_mgmt: Success\n', 8000, error))
            self.assertEqual(data['pam_ok'], 0)
            self.assertEqual(data['pam_result'], error or 'denied_or_error')

    def test_domain_offline_is_distinct_from_failed_command(self):
        for rc, out, error, expected in (
                (0, 'Online status: Offline\n', None, 'offline'),
                (0, 'unrecognized output', None, 'error'),
                (1, 'Online status: Online\n', None, 'error'),
                (124, '', 'timeout', 'timeout'), (127, '', 'exec_error', 'exec_error')):
            data = self.health(domain=(rc, out, 1, error))
            self.assertEqual((data['domain_status'], data['domain_result']), (0 if expected == 'offline' else 2, expected))

    def test_nss_nonzero_even_with_record_is_failure(self):
        self.assertEqual(self.health(nss_rc=2)['nss_ok'], 0)

    def test_service_active_but_pam_system_error(self):
        fixture = json.loads((ROOT / 'tests/fixtures/service-active-pam-system-error.json').read_text())
        original_run = monitor.run_command

        def run(argv, timeout):
            command = Path(argv[0]).name
            key = 'pam' if command == 'sssctl' and argv[1] == 'user-checks' else command
            record = fixture['commands'][key]
            script = ('import sys; sys.stdout.write({!r}); sys.stderr.write({!r}); '
                      'sys.exit({})').format(record['stdout'], record['stderr'], record['returncode'])
            return original_run([sys.executable, '-c', script], timeout)

        for timestamp in (1000, 1060):
            with patch.object(monitor, 'run_command', side_effect=run), patch.object(monitor.time, 'time', return_value=timestamp):
                data = monitor.healthcheck('ipa.example.com', 'sssd_monitor')
            self.assertEqual((data['service_up'], data['nss_ok'], data['pam_ok']), (1, 1, 0))


class CommandLineTest(unittest.TestCase):
    def test_invalid_arguments_never_run_commands(self):
        for args in ([], ['domain'], ['--help', 'user'],
                     ['ipa.example.com', '--help'], ['ipa.example.com', 'x;touch /tmp/no']):
            with patch.object(monitor, 'run_command') as run, patch('sys.stderr'):
                self.assertEqual(monitor.main(args), 1)
                run.assert_not_called()

    def test_json_goes_to_stdout_without_local_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            previous_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                with patch.object(monitor, 'run_command', return_value=(127, '', 0, 'exec_error')), patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(monitor.main(['ipa.example.com', 'sssd_monitor']), 0)
                data = json.loads(output.getvalue())
                self.assertEqual((data['pam_ok'], data['pam_result']), (0, 'exec_error'))
                self.assertEqual(list(Path(tmp).iterdir()), [])
            finally:
                os.chdir(previous_cwd)

    def test_service_mode_is_unprivileged_and_only_runs_systemctl(self):
        for rc, error, expected in ((0, None, '1'), (3, None, '0'), (124, 'timeout', '0')):
            with patch.object(monitor, 'run_command', return_value=(rc, '', 1, error)) as run, patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(monitor.main(['--service']), 0)
                self.assertEqual(output.getvalue().strip(), expected)
                run.assert_called_once_with(['/usr/bin/systemctl', 'is-active', '--quiet', 'sssd.service'], 2)


class TemplateTest(unittest.TestCase):
    def template_uuids(self):
        values = []

        def collect(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key == 'uuid':
                        values.append(child)
                    else:
                        collect(child)
            elif isinstance(node, list):
                for child in node:
                    collect(child)

        collect(json.loads((ROOT / 'zabbix/sssd-functional-health-7.0.json').read_text()))
        return values

    def test_all_exported_uuids_are_unique_uuidv4(self):
        values = self.template_uuids()
        self.assertTrue(values)
        self.assertEqual(len(values), len(set(values)))
        for value in values:
            with self.subTest(uuid=value):
                self.assertRegex(value, r'\A[0-9a-f]{32}\Z')
                parsed = uuid.UUID(hex=value)
                self.assertEqual(parsed.version, 4)
                self.assertEqual(parsed.variant, uuid.RFC_4122)

    def test_yaml_and_json_export_the_same_uuids(self):
        text = (ROOT / 'zabbix/sssd-functional-health-7.0.yaml').read_text()
        values = re.findall(r'^\s*(?:-\s+)?uuid:\s*([0-9a-f]+)\s*$', text, re.MULTILINE)
        self.assertEqual(values, self.template_uuids())
        self.assertEqual(len(values), len(re.findall(r'^\s*(?:-\s+)?uuid:', text, re.MULTILINE)))

    def test_template_fields_resolve_against_probe_output(self):
        export = json.loads((ROOT / 'zabbix/sssd-functional-health-7.0.json').read_text())['zabbix_export']
        with patch.object(monitor, 'run_command', return_value=(127, '', 0, 'exec_error')):
            data = monitor.healthcheck('ipa.example.com', 'sssd_monitor')
        items = {item['key']: item for item in export['templates'][0]['items']}
        for item in items.values():
            if item.get('type') == 'DEPENDENT':
                self.assertIn(item['master_item']['key'], items)
                field = item['preprocessing'][0]['parameters'][0][2:]
                self.assertIn(field, data)
                if item['value_type'] == 'UNSIGNED':
                    self.assertIsInstance(data[field], int)
                    self.assertGreaterEqual(data[field], 0)
        self.assertEqual(items['sssd.service']['delay'], '30s')
        self.assertEqual(items['sssd.monitor']['delay'], '60s')
        self.assertEqual(items['sssd.monitor']['timeout'], '25s')
        self.assertNotIn('preprocessing', items['sssd.monitor'])
        watchdog = [i for i in items.values() if i.get('value_type') == 'LOG'][0]
        self.assertEqual(watchdog['status'], 'DISABLED')


@unittest.skipUnless(shutil.which('zabbix_agent2'), 'Zabbix Agent 2 is not installed')
class AgentIntegrationTest(unittest.TestCase):
    def test_agent_executes_userparameters_and_returns_probe_json(self):
        # Use the real agent and script with fixture commands. No sudo/root or
        # real host identity queries are needed for this collection-path test.
        fixture = json.loads((ROOT / 'tests/fixtures/service-active-pam-system-error.json').read_text())
        with tempfile.TemporaryDirectory(prefix='sssd-agent-') as tmp:
            directory = Path(tmp)
            fake_command = directory / 'command.py'
            fake_command.write_text(
                '#!/usr/bin/python3\nimport json,sys\n'
                'records = json.loads({!r})\n'
                'key = "pam" if "user-checks" in sys.argv else sys.argv[1]\n'
                'record = records[key]\n'
                'sys.stdout.write(record["stdout"])\n'
                'sys.stderr.write(record["stderr"])\n'
                'sys.exit(record["returncode"])\n'.format(json.dumps(fixture['commands'])))
            fake_command.chmod(0o755)
            script = (ROOT / 'scripts/sssd-monitor.py').read_text()
            for binary, key in (('/usr/bin/systemctl', 'systemctl'),
                                ('/usr/bin/getent', 'getent'), ('/usr/sbin/sssctl', 'sssctl')):
                script = script.replace(repr(binary), repr(str(fake_command)) + ', ' + repr(key))
            installed = directory / 'sssd-monitor.py'
            installed.write_text(script)
            installed.chmod(0o755)
            conf = (ROOT / 'zabbix_agent2.d/sssd.conf').read_text()
            conf = conf.replace('/usr/bin/sudo -n ', '').replace('/etc/zabbix/scripts/sssd-monitor.py', str(installed))
            include = directory / 'sssd.conf'
            include.write_text(conf)
            config = directory / 'agent.conf'
            config.write_text('LogType=console\nServer=127.0.0.1\nHostname=sssd-test\nTimeout=25\n'
                              'ControlSocket={0}/control.sock\nPluginSocket={0}/plugin.sock\n'
                              'Include={0}/sssd.conf\n'.format(tmp))
            for key in ('sssd.monitor', 'sssd.service'):
                result = subprocess.run([shutil.which('zabbix_agent2'), '-c', str(config), '-t', key],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        universal_newlines=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout)
                if key == 'sssd.monitor':
                    payload = result.stdout.split('[s|', 1)[1].rsplit(']', 1)[0]
                    data = json.loads(payload)
                    self.assertEqual((data['service_up'], data['nss_ok'], data['pam_ok']), (1, 1, 0))
                else:
                    self.assertIn('[s|1]', result.stdout)


if __name__ == '__main__':
    unittest.main()
