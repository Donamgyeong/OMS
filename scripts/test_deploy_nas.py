"""Exercise deployment failure boundaries with fake Docker, without SSH or a NAS."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

from deploy_nas import archive_config_id, deploy, main, parse_args, preflight_script

REMOTE = Path(__file__).with_name('deploy_nas_remote.sh')
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as f: f.write(json.dumps(a)+'\n')
if a[0] == 'image':
    print('linux/arm64' if 'Architecture' in a[-1] else os.environ.get('DEPLOY_TEST_IMAGE_ID', 'sha256:fixture'))
elif a[0] == 'run':
    if '--mount' in a:
        if os.environ.get('DEPLOY_TEST_FAIL') == 'token-mount': sys.exit(15)
    elif 'python' in a:
        print(json.load(sys.stdin)['secrets']['cloudflared_token']['file'])
    else: print('media-backup')
elif a[0] == 'compose':
    action = next(x for x in a if x in ('config','run','up','stop','exec','ps'))
    if action == 'config' and '--format' in a:
        print(json.dumps({'secrets':{'cloudflared_token':{'file':os.environ['DEPLOY_TEST_TOKEN']}}}))
    elif action == 'run' and 'check' in a and os.environ.get('DEPLOY_TEST_FAIL') == 'preflight':
        sys.exit(11)
    elif action == 'run' and 'migrate' in a and os.environ.get('DEPLOY_TEST_FAIL') == 'migration':
        sys.exit(12)
    elif action == 'exec' and 'db' in a:
        print('database-backup')
        if os.environ.get('DEPLOY_TEST_FAIL') == 'backup': sys.exit(13)
    elif action == 'up' and 'app' in a and os.environ.get('DEPLOY_TEST_FAIL') == 'startup':
        sys.exit(14)
    elif action == 'ps' and 'app' in a: print('old-app-id')
'''
FAKE_SUDO = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as f: f.write(json.dumps(['sudo']+sys.argv[1:])+'\n')
if sys.argv[1:] == ['-v']: sys.exit(0)
assert sys.argv[1] == '-n' and os.path.basename(sys.argv[2]) == 'docker'
os.execv(sys.argv[2], [sys.argv[2]]+sys.argv[3:])
'''


class RemoteDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / 'nas folder'
        self.release = self.target / 'releases' / 'test-release'
        self.release.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'docker').write_text(FAKE_DOCKER)
        (self.bin / 'docker').chmod(0o755)
        (self.bin / 'sudo').write_text(FAKE_SUDO)
        (self.bin / 'sudo').chmod(0o755)
        self.old_env = 'POSTGRES_PASSWORD=retain-this-value\nAPP_IMAGE=oms:old\n'
        (self.target / '.env').write_text(self.old_env)
        (self.target / 'compose.cloudflare.yaml').write_text('old-compose\n')
        self.token = self.target / 'token'
        self.token.write_text('fake-token\n')
        for name, content in [('app-image.tar', b'fixture'), ('image-id', b'sha256:fixture\n'),
                              ('image-config-id', b'sha256:classic-fixture\n'),
                              ('compose.cloudflare.yaml', b'new-compose\n')]:
            (self.release / name).write_bytes(content)
        (self.release / 'SHA256SUMS').write_text(''.join(
            hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
            for p in sorted(self.release.iterdir())))
        self.log = self.root / 'calls.jsonl'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        DEPLOY_TEST_LOG=str(self.log), DEPLOY_TEST_TOKEN=str(self.token))

    def deploy(self, failure='', docker_mode='docker', trailing_slash=False, image_id='sha256:fixture'):
        target = str(self.target) + ('/' if trailing_slash else '')
        return subprocess.run(['sh', str(REMOTE), target, str(self.release),
                               'oms', 'oms:new', docker_mode, '0'],
                              env=dict(self.env, DEPLOY_TEST_FAIL=failure, DEPLOY_TEST_IMAGE_ID=image_id),
                              text=True, capture_output=True)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_success_backs_up_before_migrating_and_preserves_secrets(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        stop = next(i for i, a in enumerate(calls) if 'stop' in a)
        dump = next(i for i, a in enumerate(calls) if any('pg_dump' in x for x in a))
        migrate = next(i for i, a in enumerate(calls) if 'migrate' in a)
        self.assertLess(stop, dump)
        self.assertLess(dump, migrate)
        backup = self.target / 'backups' / 'test-release'
        self.assertEqual((backup / 'env.before').read_text(), self.old_env)
        self.assertTrue((backup / 'media.tar.gz').is_file())
        self.assertTrue((backup / 'database.dump').is_file())
        self.assertEqual((self.target / '.env').read_text(), self.old_env.replace('oms:old', 'oms:new'))
        self.assertEqual((self.target / 'compose.cloudflare.yaml').read_text(), 'new-compose\n')
        self.assertFalse((self.target / '.deploy-lock').exists())

    def test_preflight_failure_does_not_stop_existing_service(self):
        result = self.deploy('preflight')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any('stop' in a for a in self.calls()))
        self.assertEqual((self.target / '.env').read_text(), self.old_env)
        self.assertFalse((self.target / '.deploy-lock').exists())
        self.assertIn('django-deployment-check', result.stderr)
        self.assertIn('Migrations were not started.', result.stderr)
        self.assertNotIn('rollback', result.stderr)

    def test_classic_docker_config_id_is_accepted(self):
        result = self.deploy(image_id='sha256:classic-fixture')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_image_id_is_rejected_with_actual_and_expected_values(self):
        result = self.deploy(image_id='sha256:unexpected')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Image ID mismatch: actual=sha256:unexpected', result.stderr)
        self.assertIn('image-identity', result.stderr)
        self.assertFalse(any('stop' in a for a in self.calls()))

    def test_trailing_slash_does_not_duplicate_backup_path_separators(self):
        result = self.deploy(trailing_slash=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('//backups/', result.stdout + result.stderr)
        self.assertTrue((self.target / 'backups/test-release/database.dump').is_file())

    def test_sudo_authentication_preserves_piped_input_and_backups(self):
        result = self.deploy(docker_mode='sudo')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0], ['sudo', '-v'])
        self.assertTrue(any(a[:2] == ['sudo', '-n'] and Path(a[2]).name == 'docker' for a in calls))
        self.assertTrue((self.target / 'backups/test-release/database.dump').is_file())
        self.assertTrue((self.target / 'backups/test-release/media.tar.gz').is_file())

    def test_migration_failure_keeps_backup_and_does_not_start_new_app(self):
        result = self.deploy('migration')
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.target / 'backups/test-release/database.dump').is_file())
        self.assertFalse(any('up' in a and 'app' in a for a in self.calls()))
        self.assertEqual((self.target / '.env').read_text(), self.old_env)
        self.assertIn('migration', result.stderr)

    def test_missing_token_fails_before_maintenance(self):
        self.token.unlink()
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertFalse(any('stop' in a for a in self.calls()))

    def test_token_mount_is_verified_before_maintenance(self):
        result = self.deploy('token-mount')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('tunnel-secret-mount', result.stderr)
        self.assertFalse(any('stop' in a for a in self.calls()))
        self.assertFalse(any('migrate' in a for a in self.calls()))

    def test_connector_is_recreated_with_current_secret_path(self):
        result = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        mount = next(a for a in calls if '--mount' in a)
        self.assertIn('type=bind,src=' + str(self.token) + ',dst=/tmp/oms-cloudflared-token,readonly', mount)
        up = next(a for a in calls if 'up' in a and 'cloudflared' in a)
        self.assertIn('--force-recreate', up)
        self.assertIn('--no-deps', up)

    def test_failed_backup_does_not_run_migrations(self):
        self.assertNotEqual(self.deploy('backup').returncode, 0)
        self.assertFalse(any('migrate' in a for a in self.calls()))
        self.assertFalse((self.target / 'backups/test-release/database.dump').exists())
        self.assertEqual((self.target / '.env').read_text(), self.old_env)

    def test_failed_health_check_does_not_publish_new_configuration(self):
        self.assertNotEqual(self.deploy('startup').returncode, 0)
        self.assertTrue((self.target / 'backups/test-release/database.dump').exists())
        self.assertEqual((self.target / '.env').read_text(), self.old_env)
        self.assertEqual((self.target / 'compose.cloudflare.yaml').read_text(), 'old-compose\n')

    def test_concurrent_deployment_is_rejected(self):
        (self.target / '.deploy-lock').mkdir()
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertTrue((self.target / '.deploy-lock').is_dir())

    def test_tampered_payload_is_rejected_before_loading(self):
        (self.release / 'app-image.tar').write_bytes(b'tampered')
        self.assertNotEqual(self.deploy().returncode, 0)
        self.assertFalse(self.log.exists())


class LocalDeploymentTests(unittest.TestCase):
    @staticmethod
    def image_archive(path):
        config = json.dumps({'os': 'linux', 'architecture': 'arm64'}).encode()
        manifest = json.dumps([{'RepoTags': ['oms:test'], 'Config': 'config.json'}]).encode()
        with tarfile.open(path, 'w') as archive:
            for name, data in [('manifest.json', manifest), ('config.json', config)]:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return 'sha256:' + hashlib.sha256(config).hexdigest()

    def test_archive_id_uses_config_digest_instead_of_host_index_id(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'image.tar'
            expected = self.image_archive(path)
            self.assertEqual(archive_config_id(path, 'oms:test'), expected)

    def test_nas_path_normalization(self):
        for path in ['/volume1/oms/', '/volume1//oms///', '//volume1/./oms/']:
            with self.subTest(path=path):
                self.assertEqual(parse_args(['--host', 'nas', '--path', path]).path, '/volume1/oms')
        self.assertEqual(parse_args(['--host', 'nas']).path, '/volume1/oms')

    def test_preflight_reports_missing_env_before_sudo(self):
        with tempfile.TemporaryDirectory() as folder:
            args = parse_args(['--host', 'nas', '--path', folder])
            result = subprocess.run(['sh', '-c', preflight_script(args)], text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('ERROR: .env exists in ' + folder, result.stderr)
            self.assertNotIn('CHECK: sudo', result.stdout)

    def test_preflight_identifies_docker_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.env').write_text('fixture')
            (root / 'docker').write_text('#!/bin/sh\nexit 17\n')
            (root / 'docker').chmod(0o755)
            args = parse_args(['--host', 'nas', '--path', folder, '--no-sudo'])
            result = subprocess.run(['sh', '-c', preflight_script(args)], text=True, capture_output=True,
                                    env=dict(os.environ, PATH=folder + ':' + os.environ['PATH']))
            self.assertEqual(result.returncode, 17)
            self.assertIn('CHECK: Docker daemon access', result.stdout)
            self.assertIn('ERROR: Docker daemon access (exit 17)', result.stderr)

    def test_check_only_does_not_build_or_transfer(self):
        from unittest.mock import patch
        args = parse_args(['--host', 'nas', '--password', '--check-only'])
        with patch('deploy_nas.run') as run, patch('deploy_nas.subprocess.check_output') as output:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(deploy(args, '/tmp/test-control'), 0)
            self.assertEqual(run.call_count, 1)
            self.assertIn('-tt', run.call_args.args[0])
            output.assert_not_called()

    def test_password_mode_uses_tty_for_sudo_and_no_tty_for_binary_transfer(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'scripts').mkdir()
            (root / 'compose.cloudflare.yaml').write_text('fixture')
            (root / 'scripts/deploy_nas_remote.sh').write_text('fixture')
            args = parse_args(['--host', 'nas', '--password', '--skip-build', '--image', 'oms:test'])
            def fake_run(command, **kwargs):
                if command[:2] == ['docker', 'save']:
                    self.image_archive(Path(command[3]))
            with patch('deploy_nas.ROOT', root), patch('deploy_nas.run', side_effect=fake_run) as run:
                with patch('deploy_nas.subprocess.check_output', side_effect=['linux/arm64\n', 'sha256:fixture\n']):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(deploy(args, root / 'socket'), 0)
            ssh_calls = [call.args[0] for call in run.call_args_list if call.args[0][0] == 'ssh']
            self.assertEqual(len(ssh_calls), 3)
            for command in ssh_calls:
                self.assertIn('BatchMode=no', command)
                self.assertIn('PubkeyAuthentication=no', command)
                self.assertIn('ControlPath=' + str(root / 'socket'), command)
            self.assertIn('-tt', ssh_calls[0])
            self.assertIn('sudo -v', ssh_calls[0][-1])
            self.assertIn('-T', ssh_calls[1])
            self.assertIn('-tt', ssh_calls[2])

    def test_connection_is_closed_when_deployment_fails(self):
        from unittest.mock import patch
        with patch('deploy_nas.deploy', side_effect=RuntimeError('failed')):
            with patch('deploy_nas.subprocess.run') as run:
                with self.assertRaises(RuntimeError):
                    main(['--host', 'nas', '--password'])
            command = run.call_args.args[0]
            self.assertEqual(command[-3:], ['-O', 'exit', 'nas'])

    def test_dry_run_has_no_subprocess_or_artifact_changes(self):
        from unittest.mock import patch
        with patch('deploy_nas.subprocess.run') as run, patch('deploy_nas.subprocess.check_output') as output:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['--host', 'nas', '--dry-run']), 0)
            run.assert_not_called()
            output.assert_not_called()

    def test_invalid_host_and_unversioned_image_are_rejected(self):
        for args in [['--host', '-bad'], ['--host', 'nas', '--skip-build'],
                     ['--host', 'nas', '--image', 'oms:latest']]:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parse_args(args)


if __name__ == '__main__':
    unittest.main()
