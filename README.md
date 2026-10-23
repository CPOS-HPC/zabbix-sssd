# zabbix-sssd-monitor

A lightweight Zabbix Agent 2 UserParameter plugin that checks identity lookup and SSHD PAM account authorization on FreeIPA/SSSD clients. Detects failures where SSSD is still running but PAM authorization fails.

The agent calls the script and receives JSON directly. Zabbix handles scheduling, history, and alerts. Requires Python 3.6 or later and uses only the standard library, with no additional daemon, systemd timer, or local state file.

## Installation

| Source file | Install path |
| --- | --- |
| `scripts/sssd-monitor.py` | `/etc/zabbix/scripts/sssd-monitor.py` |
| `zabbix_agent2.d/sssd.conf` | `/etc/zabbix/zabbix_agent2.d/sssd.conf` |
| `sudoers.d/zabbix-sssd` | `/etc/sudoers.d/zabbix-sssd` |

### Installer

Run the installer from the checkout with your FreeIPA domain and dedicated monitoring account. It substitutes both values in the agent configuration and sudoers rule, so there is no need to edit the examples first:

```bash
./install_zabbix_sssd.sh --dry-run ipa.example.com sssd_monitor
sudo ./install_zabbix_sssd.sh ipa.example.com sssd_monitor
```

The installer requires an existing Zabbix Agent 2 installation and the commands listed below. It backs up existing files with a `.bak.<UTC timestamp>.<PID>` suffix, installs the plugin, ensures the main agent configuration includes it, and raises the fallback `Timeout` to 25 seconds if needed. Existing timeout values of 25 seconds or more are preserved. It validates sudoers and the agent configuration before restarting `zabbix-agent2`; failures restore the previous files.

Use `--dry-run` to preview without root or system changes. Use `--no-restart` to install and validate while leaving the running agent alone; restart it later to load the configuration. The installer does not restart SSSD or import the Zabbix template. Validate the account's HBAC access and sudo authorization on the target client after installation.

### Manual installation

Replace **`ipa.example.com` and `sssd_monitor`** in both `sssd.conf` and the sudoers example with your domain and dedicated FreeIPA monitoring account. The values must match in both files. The account must be allowed by SSHD HBAC on the monitored host and must not be disabled or expired. The script does not need a password or open an SSH session.

Install on the target client as root:

```bash
install -d -o root -g root -m 0755 /etc/zabbix/scripts
install -d -o root -g root -m 0755 /etc/zabbix/zabbix_agent2.d
install -o root -g root -m 0755 scripts/sssd-monitor.py /etc/zabbix/scripts/sssd-monitor.py
install -o root -g root -m 0644 zabbix_agent2.d/sssd.conf /etc/zabbix/zabbix_agent2.d/sssd.conf
visudo -cf sudoers.d/zabbix-sssd
install -o root -g root -m 0440 sudoers.d/zabbix-sssd /etc/sudoers.d/zabbix-sssd
```

Required commands: `/usr/bin/python3`, `/usr/bin/systemctl`, `/usr/bin/getent`, `/usr/sbin/sssctl` (usually provided by `sssd-tools`), and `/usr/bin/sudo`. The script and its parent directories must be managed by root and must not be writable by the Zabbix account.

Agent configuration:

```ini
UserParameter=sssd.monitor,/usr/bin/sudo -n /etc/zabbix/scripts/sssd-monitor.py ipa.example.com sssd_monitor
UserParameter=sssd.service,/etc/zabbix/scripts/sssd-monitor.py --service
```

`sssctl` requires root privileges, so the sudoers rule allows only the exact script command with the specified domain and user. It uses no wildcards and does not allow arbitrary script arguments. The `--service` check runs as the Zabbix account.

Ensure `/etc/zabbix/zabbix_agent2.conf` includes the following line. Do not add it again if it is already present:

```ini
Include=/etc/zabbix/zabbix_agent2.d/*.conf
```

The four commands in a full probe have timeouts of 2, 4, 4, and 8 seconds. The template sets the `sssd.monitor` item timeout to **25s**; use the same timeout when creating the item manually. Zabbix 7.0 uses the item timeout configured in the frontend. Set `Timeout=25` in the main agent configuration for fallback behavior with older servers/proxies or local `-t` tests. See the [Zabbix timeout documentation](https://www.zabbix.com/documentation/7.0/en/manual/appendix/config/zabbix_agent2#timeout).

Test sudo authorization and agent collection:

```bash
sudo -u zabbix /usr/bin/sudo -n /etc/zabbix/scripts/sssd-monitor.py ipa.example.com sssd_monitor
sudo -u zabbix zabbix_agent2 -c /etc/zabbix/zabbix_agent2.conf -t sssd.monitor
sudo -u zabbix zabbix_agent2 -c /etc/zabbix/zabbix_agent2.conf -t sssd.service
systemctl restart zabbix-agent2
```

## Monitoring items

In Zabbix 7.0, open **Data collection → Templates → Import** and select either format:

- [YAML template](zabbix/sssd-functional-health-7.0.yaml)
- [JSON template](zabbix/sssd-functional-health-7.0.json)

Both files contain the same template, including UUIDs, items, triggers, and dependencies; import either one. Link **SSSD functional health** to the host. The template uses passive agent checks and requires a working agent interface.

| Item | Check | Interval |
| --- | --- | --- |
| `sssd.service` | SSSD service status: 1 healthy / 0 failed | 30 seconds |
| `sssd.monitor` | Full JSON master item | 60 seconds |
| `sssd.monitor.domain` | Domain status: 1 online / 0 offline / 2 query failed | Dependent |
| `sssd.monitor.nss` | `getent -s sss passwd <user>`: 1 success / 0 failure | Dependent |
| `sssd.monitor.pam` | `sssctl user-checks -a acct -s sshd <user>`: 1 success / 0 failure | Dependent |
| `sssd.monitor.pam.latency` | Duration of the entire PAM check command, in milliseconds | Dependent |

The JSON also includes the completion `timestamp`, a `service_up` snapshot, `domain_result`, and `pam_result`. All dependent items share one probe; they do not run separate PAM checks. The script sets `LC_ALL=C` and parses combined stdout/stderr. PAM success requires a normal command exit and an explicit `pam_acct_mgmt: Success` result; the exit code alone is insufficient.

### Alerts

| Condition | Severity |
| --- | --- |
| Both latest PAM probes failed, with at least two samples within three minutes | High |
| SSSD service stopped or its status query failed | High |
| Both latest NSS probes failed, with at least two samples within three minutes | Warning |
| Latest six domain samples are offline, their completion times span at least 300 seconds, and at least six samples arrived within seven minutes | Warning |
| Domain status cannot be queried, including timeouts and command errors | Warning |
| No `sssd.monitor` data received for three minutes | High |

Zabbix history determines consecutive failures. Every poll runs a fresh probe, so samples are not repeated reads of a stale JSON file. Collection failures trigger `nodata(...,3m)`. PAM, NSS, and domain triggers depend on the service-down and nodata triggers to reduce secondary alerts.

Watchdog monitoring is an optional active `logrt[]` item, disabled by default. If you have a dedicated SSSD text log readable by Zabbix, configure the `{$SSSD.WATCHDOG.LOG}` macro and active checks, then enable the item. Each match for `was terminated by own WATCHDOG` generates a separate Warning event that requires manual closure. The plugin does not install a log collection service.

## Validation and limitations

```bash
python3 -m unittest discover -s tests -v
```

All 25 tests currently pass. Coverage includes stderr handling, locale settings and timeouts using real subprocesses; an active SSSD service with a PAM System error; and UserParameter collection using local Zabbix Agent 2 7.0.21. Template tests verify unique UUIDv4 identifiers and matching UUIDs in the YAML and JSON exports. Installer tests use a temporary filesystem and substitute privileged system commands to check configuration rendering, permissions, backups, repeated installation, and rollback. The agent test substitutes system commands and bypasses sudo. **FreeIPA behavior, sudo authorization, deployment on a real client, and template import into Zabbix server still require validation in the target environment.** The agent integration test is skipped when Agent 2 is not installed.

Before a wider rollout, test an allowed account, an account denied by HBAC, recovery, and interrupted collection on a noncritical client. `tests/fixtures/service-active-pam-system-error.json` contains synthetic failure data, not captured host logs.

The PAM account check does not validate passwords, Kerberos TGTs, or complete SSH logins. NSS and PAM may use cached data; success does not guarantee a fresh FreeIPA connection on every poll. Latency includes the NSS and InfoPipe queries performed internally by `sssctl`. The plugin does not restart SSSD automatically.

When upgrading from the earlier timer version, disable and remove `zabbix-sssd-monitor.timer` and `.service`, then replace the agent configuration to avoid duplicate UserParameters. When reimporting the template, remove the old failure counters, offline-seconds item, and state-file freshness trigger. Zabbix history and nodata checks replace them.

For troubleshooting, inspect the output of `zabbix_agent2 -t` and `LC_ALL=C sssctl user-checks -a acct -s sshd <user> 2>&1`. For permission errors, check sudoers, root ownership, and SELinux AVC denials rather than running the entire agent as root.

References: [Zabbix UserParameter](https://www.zabbix.com/documentation/7.0/en/manual/config/items/userparameters), [SSSD user-checks source](https://github.com/SSSD/sssd/blob/master/src/tools/sssctl/sssctl_user_checks.c). See [LICENSE](LICENSE) for licensing terms.
