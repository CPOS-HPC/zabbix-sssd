#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat <<'EOF'
Usage: install_zabbix_sssd.sh [--dry-run] [--no-restart] DOMAIN USER

Install the SSSD monitoring plugin for an existing Zabbix Agent 2 installation.
  --dry-run     Print generated configuration without changing the system.
  --no-restart  Install and validate without restarting zabbix-agent2.
  -h, --help    Show this help.
EOF
}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

dry_run=0
restart_agent=1
positional=()
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --no-restart) restart_agent=0 ;;
        -h|--help) usage; exit 0 ;;
        -*) die "Unknown option: $arg" ;;
        *) positional+=("$arg") ;;
    esac
done
[[ ${#positional[@]} -eq 2 ]] || { usage >&2; exit 1; }
domain=${positional[0]}
monitor_user=${positional[1]}
[[ $domain =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die 'Invalid domain'
[[ $monitor_user =~ ^[A-Za-z0-9_][A-Za-z0-9_.@-]*$ ]] || die 'Invalid monitoring user'

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
script_path=/etc/zabbix/scripts/sssd-monitor.py
parameter_path=/etc/zabbix/zabbix_agent2.d/sssd.conf
sudoers_path=/etc/sudoers.d/zabbix-sssd
agent_conf=/etc/zabbix/zabbix_agent2.conf
for file in scripts/sssd-monitor.py zabbix_agent2.d/sssd.conf sudoers.d/zabbix-sssd; do
    [[ -f $source_dir/$file ]] || die "Missing source file: $source_dir/$file"
done

render() {
    sed -e "s/ipa\.example\.com/$domain/g" -e "s/sssd_monitor/$monitor_user/g" "$source_dir/$1"
}

if (( dry_run )); then
    printf 'Install %s -> %s (root:root, 0755)\n' "$source_dir/scripts/sssd-monitor.py" "$script_path"
    printf '\n%s (root:root, 0644):\n' "$parameter_path"
    render zabbix_agent2.d/sssd.conf
    printf '\n%s (root:root, 0440):\n' "$sudoers_path"
    render sudoers.d/zabbix-sssd
    printf '\nEnsure agent Include covers the plugin and fallback Timeout is at least 25s.\n'
    printf 'Validate sudoers and Agent 2 configuration; restart agent: %s\n' "$restart_agent"
    exit 0
fi

[[ $(id -u) -eq 0 ]] || die 'Run as root, or use --dry-run to preview'
for command in install mktemp sed date cp rm visudo zabbix_agent2; do
    command -v "$command" >/dev/null || die "Required command not found: $command"
done
for command in /usr/bin/python3 /usr/bin/systemctl /usr/bin/getent /usr/sbin/sssctl /usr/bin/sudo; do
    [[ -x $command ]] || die "Required executable not found: $command"
done
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' || die 'Python 3.6 or later is required'
id zabbix >/dev/null 2>&1 || die 'The zabbix system account does not exist'
[[ -f $agent_conf ]] || die "Agent configuration not found: $agent_conf"

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
render zabbix_agent2.d/sssd.conf > "$stage/sssd.conf"
render sudoers.d/zabbix-sssd > "$stage/zabbix-sssd"
visudo -cf "$stage/zabbix-sssd"

# Preserve existing Include patterns and timeout values of 25s or more.
/usr/bin/python3 - "$agent_conf" "$parameter_path" "$stage/agent.conf" <<'PY'
import fnmatch
import re
import sys
from pathlib import Path

source, parameter, target = sys.argv[1:]
text = Path(source).read_text()
patterns = re.findall(r'^\s*Include\s*=\s*(.*?)\s*$', text, re.MULTILINE)
covered = any(fnmatch.fnmatchcase(parameter, pattern)
              or pattern.rstrip('/') == str(Path(parameter).parent)
              for pattern in patterns)
if not covered:
    text = text.rstrip('\n') + '\nInclude=/etc/zabbix/zabbix_agent2.d/*.conf\n'
timeouts = re.findall(r'^[ \t]*Timeout[ \t]*=[ \t]*(\d+)[ \t]*$', text, re.MULTILINE)
if not timeouts:
    text = text.rstrip('\n') + '\nTimeout=25\n'
elif int(timeouts[0]) < 25:
    text = re.sub(r'^[ \t]*Timeout[ \t]*=[ \t]*\d+[ \t]*$', 'Timeout=25', text, flags=re.MULTILINE)
Path(target).write_text(text)
PY

backup_suffix=.bak.$(date -u +%Y%m%dT%H%M%SZ).$$
targets=("$script_path" "$parameter_path" "$sudoers_path" "$agent_conf")
existed=()
for target in "${targets[@]}"; do
    if [[ -e $target ]]; then
        [[ -f $target && ! -L $target ]] || die "Expected a regular file: $target"
        cp -p -- "$target" "$target$backup_suffix"
        existed+=(1)
    else
        [[ ! -L $target ]] || die "Refusing dangling symlink: $target"
        existed+=(0)
    fi
done

was_active=0
if (( restart_agent )) && /usr/bin/systemctl is-active --quiet zabbix-agent2; then
    was_active=1
fi
restart_attempted=0
rollback() {
    local result=$1 i
    trap - ERR INT TERM
    set +e
    printf 'Installation failed; restoring previous files.\n' >&2
    for i in "${!targets[@]}"; do
        if (( existed[i] )); then
            cp -pf -- "${targets[i]}$backup_suffix" "${targets[i]}"
        else
            rm -f -- "${targets[i]}"
        fi
    done
    if (( restart_attempted && was_active )); then
        /usr/bin/systemctl restart zabbix-agent2 || printf 'Could not restart agent after rollback.\n' >&2
    fi
    exit "$result"
}
trap 'rollback "$?"' ERR
trap 'rollback 130' INT
trap 'rollback 143' TERM

install -d -o root -g root -m 0755 /etc/zabbix/scripts /etc/zabbix/zabbix_agent2.d
install -o root -g root -m 0755 "$source_dir/scripts/sssd-monitor.py" "$script_path"
install -o root -g root -m 0644 "$stage/sssd.conf" "$parameter_path"
install -o root -g root -m 0440 "$stage/zabbix-sssd" "$sudoers_path"
# Preserve the main agent configuration's ownership and permissions.
cat "$stage/agent.conf" > "$agent_conf"
visudo -c
zabbix_agent2 -c "$agent_conf" -T
if (( restart_agent )); then
    restart_attempted=1
    /usr/bin/systemctl restart zabbix-agent2
fi
trap - ERR INT TERM
printf 'Installed SSSD monitoring for %s / %s.\n' "$domain" "$monitor_user"
printf 'Existing files were backed up with suffix %s\n' "$backup_suffix"
if (( ! restart_agent )); then
    printf 'Restart zabbix-agent2 when ready to load the new configuration.\n'
fi
printf 'Import the Zabbix template and validate sudo/PAM checks on the target client.\n'
