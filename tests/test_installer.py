import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sssd-install-test-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.source = self.work / 'source'
        self.source.mkdir()
        self.etc = self.work / 'etc'
        self.commands = self.work / 'commands'
        self.commands.mkdir()
        self.log = self.work / 'calls.log'
        for relative in ('scripts/sssd-monitor.py', 'zabbix_agent2.d/sssd.conf', 'sudoers.d/zabbix-sssd'):
            dest = self.source / relative
            dest.parent.mkdir(exist_ok=True)
            dest.write_text((ROOT / relative).read_text().replace('/etc/', str(self.etc) + '/'))
        for command in ('id', 'visudo', 'zabbix_agent2', 'systemctl', 'sssctl'):
            self.make_command(command, '''
import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['SSSD_TEST_LOG'], 'a') as log:
    log.write(name + ' ' + ' '.join(sys.argv[1:]) + '\\n')
if name == os.environ.get('SSSD_TEST_FAIL'):
    sys.exit(1)
if name == 'id':
    print('0' if sys.argv[1:] == ['-u'] else 'uid=1000(zabbix)')
''')
        # Real install/cp/mode handling in a temporary tree; omit root ownership.
        self.make_command('install', '''
import subprocess, sys
args = []
index = 1
while index < len(sys.argv):
    if sys.argv[index] in ('-o', '-g'):
        index += 2
    else:
        args.append(sys.argv[index])
        index += 1
sys.exit(subprocess.call([{!r}] + args))
'''.format(shutil.which('install')))
        source = (ROOT / 'install_zabbix_sssd.sh').read_text()
        source = source.replace('/etc/', str(self.etc) + '/')
        source = source.replace('/usr/bin/systemctl', str(self.commands / 'systemctl'))
        source = source.replace('/usr/sbin/sssctl', str(self.commands / 'sssctl'))
        self.installer = self.source / 'install_zabbix_sssd.sh'
        self.installer.write_text(source)
        self.agent_conf = self.etc / 'zabbix/zabbix_agent2.conf'
        self.targets = [self.etc / 'zabbix/scripts/sssd-monitor.py',
                        self.etc / 'zabbix/zabbix_agent2.d/sssd.conf',
                        self.etc / 'sudoers.d/zabbix-sssd', self.agent_conf]
        self.originals = {}
        for target in self.targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = 'Server=127.0.0.1\nTimeout=3\n' if target == self.agent_conf else 'previous ' + target.name + '\n'
            target.write_text(content)
            target.chmod(0o640)
            self.originals[target] = content
        self.env = dict(os.environ, PATH=str(self.commands) + os.pathsep + os.environ['PATH'],
                        SSSD_TEST_LOG=str(self.log))

    def make_command(self, name, content):
        path = self.commands / name
        path.write_text('#!' + sys.executable + '\n' + content)
        path.chmod(0o755)

    def run_installer(self, *flags, **env):
        return subprocess.run(['bash', str(self.installer)] + list(flags) + ['ipa.test.example', 'probe_user'],
                              env=dict(self.env, **env), cwd=str(self.work),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=15)

    def assert_originals(self):
        for target, content in self.originals.items():
            self.assertEqual(target.read_text(), content)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_dry_run_and_invalid_arguments_do_not_change_files(self):
        result = self.run_installer('--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('ipa.test.example probe_user', result.stdout)
        self.assertFalse(self.log.exists())
        result = self.run_installer('--unknown-option')
        self.assertNotEqual(result.returncode, 0)
        result = subprocess.run(['bash', str(self.installer), '--dry-run', 'ipa.test.example', 'user;id'],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertNotEqual(result.returncode, 0)
        self.assert_originals()

    def test_install_without_restart_renders_matching_config_and_backs_up(self):
        result = self.run_installer('--no-restart')
        self.assertEqual(result.returncode, 0, result.stdout)
        for target, mode in zip(self.targets, (0o755, 0o644, 0o440, 0o640)):
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
            backup = list(target.parent.glob(target.name + '.bak.*'))
            self.assertEqual(len(backup), 1)
            self.assertEqual(backup[0].read_text(), self.originals[target])
        for target in self.targets[1:3]:
            self.assertIn('sssd-monitor.py ipa.test.example probe_user', target.read_text())
        self.assertIn('Timeout=25\n', self.agent_conf.read_text())
        self.assertIn('Include=' + str(self.etc) + '/zabbix/zabbix_agent2.d/*.conf', self.agent_conf.read_text())
        self.assertIn('zabbix_agent2 -c ', self.log.read_text())
        self.assertNotIn('systemctl restart', self.log.read_text())

    def test_reinstall_preserves_timeout_and_avoids_duplicate_include(self):
        self.agent_conf.write_text('Server=127.0.0.1\nTimeout=30\nInclude=' + str(self.etc) + '/zabbix/zabbix_agent2.d/*.conf\n')
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stdout)
        content = self.agent_conf.read_text()
        self.assertIn('Timeout=30\n', content)
        self.assertEqual(content.count('Include='), 1)
        self.assertEqual(self.log.read_text().count('systemctl restart zabbix-agent2'), 2)

    def test_agent_validation_failure_restores_files(self):
        result = self.run_installer(SSSD_TEST_FAIL='zabbix_agent2')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('restoring previous files', result.stdout)
        self.assert_originals()
        self.assertNotIn('systemctl restart', self.log.read_text())

    def test_sudoers_validation_failure_leaves_files_untouched(self):
        result = self.run_installer(SSSD_TEST_FAIL='visudo')
        self.assertNotEqual(result.returncode, 0)
        self.assert_originals()
        self.assertEqual(list(self.etc.rglob('*.bak.*')), [])

    def test_failed_first_install_removes_new_files(self):
        for target in self.targets[:-1]:
            target.unlink()
        result = self.run_installer(SSSD_TEST_FAIL='zabbix_agent2')
        self.assertNotEqual(result.returncode, 0)
        for target in self.targets[:-1]:
            self.assertFalse(target.exists())
        self.assertEqual(self.agent_conf.read_text(), self.originals[self.agent_conf])


if __name__ == '__main__':
    unittest.main()
