import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load('publication_gate', 'public-preflight.py')


class PublicationTests(unittest.TestCase):
    def test_clean_templates_and_versions_pass(self):
        for value in ('NS="<your-namespace>"', 'host=192.0.2.10', 'http://127.0.0.1:8000',
                      'Description=server release 1.4.10.4', 'mail=operator@example.invalid'):
            with self.subTest(value=value):
                self.assertEqual(gate.inspect('config.example', value.encode(), []), [])

    def test_url_userinfo_is_not_a_mailbox(self):
        confusable = "'https://your-bank.example@" + 'discord' + '.gg/x' + "' reads as a bank link"
        self.assertEqual(gate.inspect('links.js', confusable.encode(), []), [])
        mailbox = 'contact operator@' + 'discord' + '.gg for access'
        self.assertTrue(gate.inspect('links.js', mailbox.encode(), []))

    def test_every_identifier_prefix_is_blocked(self):
        for prefix in ('1', '2', '9'):
            value = prefix + ''.join(str(number % 10) for number in range(17))
            findings = gate.inspect('config.py', ('OWNER_ID=' + value).encode(), [])
            self.assertTrue(findings)
            self.assertNotIn(value, json.dumps(findings))

    def test_raw_host_identifier_and_private_address_block(self):
        host = 'a1b2' * 4
        address = '.'.join(('172', '16', '9', '7'))
        for value in ('HOST_ID="' + host + '"', 'ALLOWED_SOURCE_IPS="' + address + '"', 'MQGIP=' + address):
            self.assertTrue(gate.inspect('config.py', value.encode(), []))

    def test_private_values_and_paths_never_echo(self):
        value = 'private-fixture-value'
        patterns = [re.compile(re.escape(value))]
        result = gate.inspect(value + '.txt', value.encode(), patterns)
        self.assertTrue(result)
        self.assertNotIn(value, json.dumps(result))

    def test_private_inventory_is_required_and_validated(self):
        with self.assertRaises(ValueError):
            gate.private_patterns(None, True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'inventory.json'
            for data in ({'patterns': []}, {'patterns': ['']}, {'patterns': [None]}, {'patterns': ['[']}):
                path.write_text(json.dumps(data))
                path.chmod(0o600)
                with self.assertRaises(ValueError):
                    gate.private_patterns(path, True)
            path.write_text(json.dumps({'patterns': ['fixture-only']}))
            self.assertEqual(len(gate.private_patterns(path, True)), 1)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                gate.private_patterns(path, True)

    def test_ignored_secrets_and_binary_data_block(self):
        for name in ('.env', '.env.local', 'private/note.txt', 'data/snapshot.db', 'capture.pcap', 'docs/RE-INDEX.md'):
            self.assertTrue(gate.inspect(name, b'fixture', []), name)
        self.assertFalse(gate.inspect('.env.example', b'TOKEN=<configure locally>', []))
        self.assertTrue(gate.inspect('docs/img/picture.png', b'\x89PNG\0fixture', []))

    def test_private_details_are_detected_even_in_checker_source(self):
        value = 'vp' + 'tr = pointer'
        self.assertTrue(gate.inspect('scripts/public-preflight.py', value.encode(), []))

    def test_inventory_rejects_symlinks_fifo_and_nested_git(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'outside-link').symlink_to(ROOT / 'README.md')
            with self.assertRaises((ValueError, OSError)):
                list(gate.inventory(root, 'directory'))
            (root / 'outside-link').unlink()
            os.mkfifo(root / 'pipe')
            with self.assertRaises(ValueError):
                list(gate.inventory(root, 'directory'))
            (root / 'pipe').unlink()
            (root / 'nested' / '.git').mkdir(parents=True)
            with self.assertRaises(ValueError):
                list(gate.inventory(root, 'directory'))

    def test_empty_and_oversized_candidates_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                list(gate.inventory(root, 'directory'))
            (root / 'large.txt').write_text('too large')
            with patch.object(gate, 'MAX_FILE_BYTES', 3), self.assertRaises(ValueError):
                list(gate.inventory(root, 'directory'))

    def test_staged_scan_cannot_be_fooled_by_clean_working_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertFalse((root / '.git').exists())
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            path = root / 'candidate.txt'
            path.write_text('private-fixture')
            subprocess.run(['git', '-C', str(root), 'add', 'candidate.txt'], check=True)
            path.write_text('clean working copy')
            rules = [re.compile('private-fixture')]
            staged = [hit for name, raw in gate.inventory(root, 'staged') for hit in gate.inspect(name, raw, rules)]
            working = [hit for name, raw in gate.inventory(root, 'tracked') for hit in gate.inspect(name, raw, rules)]
            self.assertTrue(staged)
            self.assertFalse(working)

    def test_required_inventory_cli_fails_before_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'readme.md').write_text('fixture')
            result = subprocess.run(['python3', str(ROOT / 'scripts/public-preflight.py'), str(root),
                                     '--require-private-patterns'], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)['status'], 'refused')

    def test_outgoing_committer_metadata_is_scanned_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'repo'
            root.mkdir()
            self.assertFalse((root / '.git').exists())
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'readme.md').write_text('clean fixture')
            subprocess.run(['git', '-C', str(root), 'add', 'readme.md'], check=True)
            env = {**os.environ, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_COMMITTER_NAME': 'Fixture',
                   'GIT_AUTHOR_EMAIL': 'fixture@users.noreply.github.com',
                   'GIT_COMMITTER_EMAIL': 'fixture@users.noreply.github.com'}
            command = ['git', '-C', str(root), 'commit', '-q', '--allow-empty', '-m', 'fixture']
            subprocess.run(command, env=env, check=True)
            base = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD']).decode().strip()
            private = 'fixture-private@example.invalid'
            subprocess.run(command, env={**env, 'GIT_COMMITTER_EMAIL': private}, check=True)
            rules = parent / 'rules.json'
            rules.write_text(json.dumps({'patterns': [re.escape(private)]}))
            rules.chmod(0o600)
            result = subprocess.run(['python3', str(ROOT / 'scripts/public-preflight.py'), '--staged',
                '--commit-range', base + '..HEAD', '--private-patterns', str(rules)], cwd=root,
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn('commit-metadata', result.stdout)
            self.assertNotIn(private, result.stdout)

    def test_gitleaks_exception_does_not_hide_other_credentials(self):
        binary = os.environ.get('GITLEAKS_BIN') or shutil.which('gitleaks')
        if not binary:
            self.skipTest('set GITLEAKS_BIN to verify the scanner exception')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            target = root / 'scripts/tags-data.json'
            target.write_text(json.dumps({'job_skill_blocks': {'fixture': ['Skills.Key.BeneGesserit1']}}))
            command = [binary, 'dir', '--redact=100', '--no-banner', '--config', str(ROOT / '.gitleaks.toml'), str(root)]
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 0)
            fake = 'gh' + 'p_' + hashlib.sha256(b'nonfunctional-test-fixture').hexdigest()[:36]
            target.write_text(json.dumps({'api_key': fake, 'tag': 'Skills.Key.BeneGesserit1'}))
            self.assertEqual(subprocess.run(command, capture_output=True).returncode, 1)


class ConfigurationTests(unittest.TestCase):
    def test_chat_sender_missing_configuration_never_executes(self):
        bridge = load('public_chat_bridge', 'dune-chat-send.py')
        job = {'scope': 'map', 'mode': 'apply', 'message': 'fixture'}
        with patch.object(bridge, 'CIELAGO_HOST_ID', ''), patch.object(bridge.sys, 'stdin', io.StringIO(json.dumps(job))), \
             patch.object(bridge.subprocess, 'run') as run, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                bridge.main()
            self.assertFalse(json.loads(output.getvalue())['success'])
            run.assert_not_called()

    def test_command_credentials_have_no_builtin_fallback(self):
        for name in ('dune-server-command.py', 'dune-service-broadcast.py'):
            with self.subTest(name=name):
                module = load(name.replace('-', '_'), name)
                self.assertFalse(hasattr(module, 'BUILTIN_AUTH_TOKEN'))
                with patch.dict(os.environ, {'DUNE_COMMAND_AUTH_TOKEN': ''}), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        module.load_token(None, 'fixture')
                with patch.dict(os.environ, {'DUNE_COMMAND_AUTH_TOKEN': 'fixture-only-value'}):
                    self.assertEqual(module.load_token(None, 'fixture'), 'fixture-only-value')

    def test_dispatcher_requires_explicit_relay_allowlist(self):
        env = {key: value for key, value in os.environ.items() if key not in ('LASTSIETCH_RELAY_ALLOWED_IPS', 'SSH_CLIENT', 'SSH_ORIGINAL_COMMAND')}
        result = subprocess.run(['bash', str(ROOT / 'scripts/dune-relay-dispatch.sh')], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('configure LASTSIETCH_RELAY_ALLOWED_IPS', result.stderr)

    def test_storage_moves_default_off(self):
        with patch.dict(os.environ):
            os.environ.pop('LASTSIETCH_STORAGE_MOVE_ENABLED', None)
            writer = load('public_storage_default', 'dune-storage-write.py')
        self.assertFalse(writer.MOVE_ENABLED)

    def test_missing_augment_catalog_is_not_compatibility(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'DUNE_AUGMENT_CATALOG': str(Path(directory) / 'absent.json')}):
            module = load('public_augment_catalog', 'dune-augment.py')
            self.assertEqual(module.catalog(), {})

    def test_updater_is_disabled_and_notifications_require_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'DUNE_UPD_WORKDIR': directory, 'DUNE_UPD_ENABLE': '0', 'DUNE_UPD_OWNER_ID': ''}
            command = ['bash', str(ROOT / 'ops/dune-update-auto/dune-update-orchestrator.sh')]
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0)
            result = subprocess.run(command + ['test-notify'], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)
            self.assertIn('configure DUNE_UPD_OWNER_ID', result.stderr)


if __name__ == '__main__':
    unittest.main()
