"""Exercise deployment failure boundaries with fake Docker, without SSH or a NAS."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from deploy_nas import main, parse_args

REMOTE = Path(__file__).with_name('deploy_nas_remote.sh')
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
with open(os.environ['DEPLOY_TEST_LOG'], 'a') as f: f.write(json.dumps(a)+'\n')
if a[0] == 'image':
    print('linux/arm64' if 'Architecture' in a[-1] else 'sha256:fixture')
elif a[0] == 'run':
    if 'python' in a:
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
        self.old_env = 'POSTGRES_PASSWORD=retain-this-value\nAPP_IMAGE=oms:old\n'
        (self.target / '.env').write_text(self.old_env)
        (self.target / 'compose.cloudflare.yaml').write_text('old-compose\n')
        self.token = self.target / 'token'
        self.token.write_text('fake-token\n')
        for name, content in [('app-image.tar', b'fixture'), ('image-id', b'sha256:fixture\n'),
                              ('compose.cloudflare.yaml', b'new-compose\n')]:
            (self.release / name).write_bytes(content)
        (self.release / 'SHA256SUMS').write_text(''.join(
            hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
            for p in sorted(self.release.iterdir())))
        self.log = self.root / 'calls.jsonl'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        DEPLOY_TEST_LOG=str(self.log), DEPLOY_TEST_TOKEN=str(self.token))

    def deploy(self, failure=''):
        return subprocess.run(['sh', str(REMOTE), str(self.target), str(self.release),
                               'oms', 'oms:new', 'docker', '0'],
                              env=dict(self.env, DEPLOY_TEST_FAIL=failure),
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
