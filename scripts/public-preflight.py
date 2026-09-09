#!/usr/bin/env python3
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

MAX_FILES = 5000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
SKIP_DIRS = {'__pycache__', 'node_modules', '.venv', 'venv', '.svelte-kit', '.pytest_cache', '.ruff_cache'}
DENIED = re.compile(
    r'(?i)(?:^|/)(?:\.git|private|secrets?|snapshots?|logs?|k3s-bootstrap|funcom-procs[^/]*|'
    r'cvar-catalog|discord-export|community-scrape|node_modules|\.preflight-local)(?:/|$)'
    r'|\.(?:pak|utoc|ucas|uasset|umap|glb|gltf|mask|pcap|pcapng|pem|key|p12|pfx|jks|'
    r'sqlite3?|db|dump|log|jwt|token|zip|gz|tar)$'
    r'|(?:^|/)(?:\.env(?!\.(?:example|sample)$)(?:\..*)?|id_rsa|id_ed25519|kubeconfig|seed_users\.py|SERVER-ADMIN-GUIDE[^/]*|'
    r'GETTING-STARTED\.md|RE-INDEX[^/]*|DUNERESUME[^/]*|.*-secret\.(?:json|ya?ml))$'
    r'|Default(?:Game|Engine)\.ini$|_all\.sql$|layout_\d+\.(?:bin|json)$|ops/artifacts/'
    r'|DISCORD-INTEL-|INTEL-PULL-|COMPETITOR-|SECURITY-AUDIT|dune-icons/|pak-meta'
    r'|keys for raffle/|textKeys\.txt$|rollback-.*\.json$')
CONTENT = [
    ('credential', re.compile(r'Func[A-Za-z0-9]{30,}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.|'
                             r'xox[baprs]-|gh[pousr]_[A-Za-z0-9]{20,}|discord(?:app)?\.com/api/webhooks/')),
    ('deployment namespace', re.compile(r'(?i)funcom-seabass-sh-[0-9a-f]{16}|\bsh-[0-9a-f]{16}-')),
    ('private reference', re.compile(r'(?i)(?:feedback|reference|project)_[a-z]+_[a-z_]{4,}|'
                                    r'\[\[[a-z][a-z0-9]*[_-][a-z0-9_-]*\]\]|ops/maint-\d{4}-\d{2}')),
    ('local workstation path', re.compile(r'(?i)/mnt/[a-z]/Users/|[A-Z]:\\Users\\|'
                                         r'(?:/home/|/Users/)[^/\s]+/(?:Source|Documents|Desktop|Tools)/|'
                                         r'(?:~/|["\'])Source/(?:Security|Private|Internal)/')),
    ('private native detail', re.compile(r'(?i)\bvptr\b|\bfile_off\b|\+0x[0-9a-f]{3,}')),
]
SNOWFLAKE = re.compile(r'(?<![A-Za-z0-9])[0-9]{17,20}(?![A-Za-z0-9])')
INTEGER_BOUNDS = {'9223372036854775807', '9223372036854775808', '18446744073709551615'}
HOST_ID = re.compile(r'(?i)\b(?:[a-z_]*(?:host|sender|account)_?id|uuid)\b[^\n]{0,80}?["\']?\b([0-9a-f]{16})\b')
EMAIL = re.compile(r'[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
IPV4 = re.compile(r'(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])')
IP_CONTEXT = re.compile(r'(?i)https?://|\b(?:ssh|scp|ping|curl)\s|\b[a-z_]*(?:ip|ips|host|hosts|hostname|address|addr)(?:_[a-z0-9_]+)?\s*[:=]')
OPT_PREFIX = re.compile(r'/opt/[a-z0-9][a-z0-9._-]*')
OPT_ALLOWED = re.compile(r'^/opt/(?:lastsietch[a-z0-9-]*|cielago|backups?|rabbitmq|erlang|hol[a-z0-9-]*)$')
DOC_NETWORKS = tuple(ipaddress.ip_network(value) for value in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24'))


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError('Git inventory unavailable')
    return result.stdout


def private_patterns(path, required):
    if path is None:
        if required:
            raise ValueError('publisher requires an external private pattern inventory')
        return []
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('private inventory must be a regular file')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('private inventory must be an owned regular mode0600 file')
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('private inventory exceeds bound')
    try:
        data = json.loads(raw)
        if (set(data) != {'patterns'} or not isinstance(data['patterns'], list) or not 1 <= len(data['patterns']) <= 100
                or any(not isinstance(value, str) or not value for value in data['patterns'])):
            raise ValueError()
        return [re.compile(value, re.I) for value in data['patterns']]
    except (ValueError, TypeError, re.error):
        raise ValueError('invalid private pattern inventory') from None


def safe_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or any(ord(char) < 32 for char in name):
        raise ValueError('unreviewed candidate path')


def read_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('candidate file must be regular')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise ValueError('non-regular or oversized candidate file')
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('candidate file exceeds bound')
    return data


def inventory(root, mode):
    entries = []
    if mode in ('staged', 'tracked'):
        for raw in git(root, 'ls-files', '--stage', '-z').split(b'\0'):
            if not raw:
                continue
            meta, name = raw.split(b'\t', 1)
            file_mode, oid, stage = meta.decode().split()
            name = name.decode('utf-8')
            safe_name(name)
            if file_mode not in ('100644', '100755') or stage != '0':
                raise ValueError('symlink, submodule or unresolved index entry')
            entries.append((name, oid if mode == 'staged' else None))
    else:
        def unreadable(_):
            raise ValueError('candidate directory is unreadable')
        visited = 0
        for parent, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
            visited += 1 + len(directories) + len(files)
            if visited > MAX_FILES * 2:
                raise ValueError('candidate directory inventory exceeds bound')
            for name in list(directories):
                path = Path(parent) / name
                if path.is_symlink():
                    raise ValueError('candidate directory symlink refused')
                if name in SKIP_DIRS or (path.parent == root and name == '.git'):
                    directories.remove(name)
                elif name == '.git':
                    raise ValueError('nested private Git history refused')
            for name in files:
                path = Path(parent) / name
                if path.parent == root and name == '.git':
                    continue
                relative = path.relative_to(root).as_posix()
                safe_name(relative)
                entries.append((relative, None))
            if len(entries) > MAX_FILES:
                raise ValueError('candidate file count exceeds bound')
    if not entries or len(entries) > MAX_FILES:
        raise ValueError('empty or oversized candidate inventory')
    total = 0
    for name, oid in sorted(entries):
        if oid:
            if int(git(root, 'cat-file', '-s', oid)) > MAX_FILE_BYTES:
                raise ValueError('index blob exceeds bound')
            raw = git(root, 'cat-file', 'blob', oid)
        else:
            path = root / name
            if root not in path.resolve().parents:
                raise ValueError('candidate escapes selected root')
            raw = read_file(path)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('candidate total exceeds bound')
        yield name, raw


def inspect(name, raw, patterns):
    findings = []
    reported_name = name
    for pattern in patterns:
        reported_name = pattern.sub('[private]', reported_name)
    def block(kind, line=None):
        findings.append({'file': reported_name, 'line': line, 'reason': kind})
    if DENIED.search(name):
        block('denied file class')
    if any(pattern.search(name) for pattern in patterns):
        block('private inventory match')
    try:
        text = raw.decode('utf-8')
        if '\0' in text:
            raise ValueError()
    except (UnicodeDecodeError, ValueError):
        block('binary requires separate publication review')
        return findings
    for number, line in enumerate(text.splitlines(), 1):
        if any(pattern.search(line) for pattern in patterns):
            block('private inventory match', number)
        for label, pattern in CONTENT:
            if pattern.search(line):
                block(label, number)
        for match in SNOWFLAKE.finditer(line):
            entity_floor = (name in ('scripts/dune-grant-schema.sql', 'scripts/dune-grant.sh')
                            and match.group() == '5' + '0' * 18
                            and re.search(r'\bSTART WITH\b|\bnew_entity_id\s*<', line))
            if match.group() not in INTEGER_BOUNDS and not entity_floor:
                block('literal long account or message identifier', number)
        if HOST_ID.search(line):
            block('literal deployment account identifier', number)
        for match in EMAIL.finditer(line):
            domain = match.group().rsplit('@', 1)[1].lower()
            if domain not in ('example.com', 'example.org', 'example.net', 'example.invalid', 'users.noreply.github.com'):
                block('mailbox address', number)
        for match in IPV4.finditer(line):
            if not IP_CONTEXT.search(line):
                continue
            try:
                address = ipaddress.ip_address(match.group())
            except ValueError:
                continue
            if not (address.is_loopback or address.is_unspecified or any(address in net for net in DOC_NETWORKS)):
                block('literal service address', number)
        for match in OPT_PREFIX.finditer(line):
            if not OPT_ALLOWED.fullmatch(match.group()):
                block('foreign install prefix', number)
    return findings


def main():
    parser = argparse.ArgumentParser(description='Bounded public publication gate. Findings never print matched values.')
    parser.add_argument('directory', nargs='?', type=Path)
    parser.add_argument('--staged', action='store_true', help='scan every actual Git index blob, not working files')
    parser.add_argument('--private-patterns', type=Path, help='external mode0600 JSON with a patterns array')
    parser.add_argument('--require-private-patterns', action='store_true')
    parser.add_argument('--manifest', type=Path, help='write the clean candidate path/hash manifest outside the candidate')
    parser.add_argument('--commit-range', help='also inspect outgoing commit messages and author/committer metadata')
    args = parser.parse_args()
    if args.directory is not None and args.staged:
        raise ValueError('choose directory or staged mode')
    root = (args.directory or Path.cwd()).resolve()
    if root in (Path(root.anchor), Path.home(), Path('/proc'), Path('/dev')) or not root.is_dir():
        raise ValueError('select a repository or dedicated candidate directory')
    for external in (args.private_patterns, args.manifest):
        if external and (external.resolve() == root or root in external.resolve().parents):
            raise ValueError('private inventory and manifest must stay outside the public candidate')
    patterns = private_patterns(args.private_patterns, args.require_private_patterns)
    mode = 'directory' if args.directory else 'staged' if args.staged else 'tracked'
    files, findings = [], []
    for name, raw in inventory(root, mode):
        findings.extend(inspect(name, raw, patterns))
        files.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    if args.commit_range:
        if not re.fullmatch(r'[A-Za-z0-9_./-]+\.\.[A-Za-z0-9_./-]+', args.commit_range) or args.commit_range.startswith('-'):
            raise ValueError('invalid outgoing commit range')
        metadata = git(root, 'log', '--format=%an%n%ae%n%cn%n%ce%n%B', args.commit_range)
        if len(metadata) > MAX_FILE_BYTES:
            raise ValueError('outgoing metadata exceeds bound')
        findings.extend(inspect('commit-metadata', metadata, patterns))
    result = {'status': 'blocked' if findings else 'clean', 'mode': mode, 'files': len(files),
              'private_inventory_loaded': bool(patterns), 'findings': findings,
              'coverage': 'candidate content only; separate secret, history and author-metadata review required'}
    if args.manifest and not findings:
        with args.manifest.open('x') as output:
            json.dump({'schema': 1, 'mode': mode, 'files': files}, output, indent=2)
    print(json.dumps(result), flush=True)
    return int(bool(findings))


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'refused', 'error_class': type(error).__name__}), flush=True)
        raise SystemExit(2)
