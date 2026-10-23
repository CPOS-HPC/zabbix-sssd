#!/usr/bin/python3
"""Bounded, read-only SSSD health probes for Zabbix dependent items.

Called directly by Zabbix Agent 2. Prints JSON; keeps no local state.
"""

import json
import os
import re
import signal
import subprocess
import sys
import time


def run_command(argv, timeout):
    start = time.monotonic()
    env = os.environ.copy()
    env.update(LC_ALL='C', LANG='C')
    try:
        with subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, encoding='utf-8',
                              errors='replace', env=env,
                              start_new_session=True) as proc:
            try:
                output, _ = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # PAM modules can spawn helpers. Bound the whole process group.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                return 124, '', round((time.monotonic() - start) * 1000), 'timeout'
            return proc.returncode, output, round((time.monotonic() - start) * 1000), None
    except OSError:
        return 127, '', round((time.monotonic() - start) * 1000), 'exec_error'


def pam_success(output):
    """Do not trust a 0 exit status alone; inspect PAM's actual decision."""
    decisions = re.findall(r'^\s*pam_acct_mgmt:[ \t]*(.*?)[ \t]*$', output, re.MULTILINE)
    return decisions == ['Success']


def nss_success(output):
    # getent queries only the SSS provider and may return a canonical/FQ name.
    # Its successful keyed lookup, not spelling equality, identifies the user.
    records = output.strip().splitlines()
    if len(records) != 1:
        return False
    fields = records[0].split(':')
    return (len(fields) == 7 and bool(fields[0])
            and re.fullmatch(r'[0-9]+', fields[2]) is not None
            and re.fullmatch(r'[0-9]+', fields[3]) is not None)


def healthcheck(domain, user):
    data = {
        'schema_version': 2,
        'domain': domain,
        'user': user,
        'timestamp': int(time.time()),
        'service_up': 0,
        'domain_status': 2,
        'nss_ok': 0,
        'pam_ok': 0,
        'pam_latency_ms': 0,
        'pam_result': 'not_run',
    }
    rc, _, _, _ = run_command(['/usr/bin/systemctl', 'is-active', '--quiet', 'sssd.service'], 2)
    data['service_up'] = int(rc == 0)

    rc, out, _, error = run_command(['/usr/sbin/sssctl', 'domain-status', domain], 4)
    status = re.findall(r'^Online status:[ \t]*(Online|Offline)[ \t]*$', out, re.MULTILINE)
    if rc == 0 and error is None and len(status) == 1:
        data['domain_status'] = int(status[0] == 'Online')
    data['domain_result'] = error or {0: 'offline', 1: 'online', 2: 'error'}[data['domain_status']]

    rc, out, _, _ = run_command(['/usr/bin/getent', '-s', 'sss', 'passwd', user], 4)
    data['nss_ok'] = int(rc == 0 and nss_success(out))

    rc, out, latency_ms, error = run_command(
        ['/usr/sbin/sssctl', 'user-checks', '-a', 'acct', '-s', 'sshd', user], 8)
    data['pam_latency_ms'] = latency_ms
    data['pam_ok'] = int(rc == 0 and error is None and pam_success(out))
    data['pam_result'] = ('timeout' if error == 'timeout' else
                          'exec_error' if error is not None else
                          'success' if data['pam_ok'] else
                          'denied_or_error')
    data['timestamp'] = int(time.time())
    return data


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if args == ['--service']:
        rc, _, _, error = run_command(
            ['/usr/bin/systemctl', 'is-active', '--quiet', 'sssd.service'], 2)
        print(int(rc == 0 and error is None))
        return 0
    if len(args) != 2:
        print('Usage: sssd-monitor.py DOMAIN USER | --service', file=sys.stderr)
        return 1
    domain, user = args
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', user) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', domain):
        print('Invalid monitor domain/user', file=sys.stderr)
        return 1
    print(json.dumps(healthcheck(domain, user), separators=(',', ':'), sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
