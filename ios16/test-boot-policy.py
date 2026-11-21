"""Exercise the boot policy CLI using real OpenSSL keys and independent processes."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
import uuid

CLI = [sys.executable, str(Path(__file__).with_name('boot-policy.py'))]


class BootPolicyTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.state, self.envelope = self.root / 'state', self.root / 'policy.json'
        self.private, self.public = self.key_pair('owner')
        self.vm = str(uuid.uuid4())
        self.config = self.root / 'vm.json'
        self.efi, self.disk = self.root / 'firmware.fd', self.root / 'probe.img'
        self.config.write_text(json.dumps({'config': {
            'id': self.vm, 'backend': 'crosvm', 'hypervisor': 'gunyah',
            'boot': {'uefi': {'firmware': '/sdcard/firmware.fd'}},
            'disks': [{'path': '/sdcard/probe.img', 'bus': 'virtio', 'readonly': True}],
        }}))
        self.efi.write_bytes(b'firmware image fixture')
        self.disk.write_bytes(b'one measured read-only disk')
        self.issue(self.state)
        self.command('sign', *self.inputs(self.state), '--private-key', self.private)

    def key_pair(self, stem):
        private, public = (self.root / (stem + name) for name in ('.pem', '.pub'))
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', private], check=True)
        subprocess.run(['openssl', 'pkey', '-in', private, '-pubout', '-out', public], check=True)
        return private, public

    def command(self, *arguments, rejection=None):
        result = subprocess.run([*CLI, *map(str, arguments)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1 if rejection else 0, result.stderr)
        if rejection:
            self.assertIn(rejection, result.stderr)
        return result.stdout.strip()

    def issue(self, state):
        pin = self.command('key-id', '--public-key', self.public)
        self.command('issue', '--state', state, '--vm-id', self.vm, '--owner-key-sha256', pin)

    def inputs(self, state):
        return ('--state', state, '--envelope', self.envelope, '--vm-config', self.config,
                '--firmware', self.efi, '--boot-disk', self.disk)

    def verify(self, *, state=None, public=None, rejection=None):
        return self.command('verify', *self.inputs(state or self.state), '--public-key', public or self.public,
                            rejection=rejection)

    def write_json(self, path, value):
        path.write_bytes((json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode())

    def test_valid_and_replay(self):
        self.verify()
        self.verify(rejection='already been redeemed')
        self.assertTrue((self.state / 'redeemed').is_file())

    def test_new_challenge_rejects_old_signature(self):
        later = self.root / 'later'
        self.issue(later)
        self.verify(state=later, rejection='differs from the challenge')
        self.assertFalse((later / 'redeemed').exists())

    def test_modified_inputs_and_retry(self):
        for path in (self.disk, self.efi, self.config):
            with self.subTest(path=path.name):
                intact = path.read_bytes()
                path.write_bytes(intact + b' ')
                self.verify(rejection='differs from the challenge')
                self.assertFalse((self.state / 'redeemed').exists())
                path.write_bytes(intact)
        self.verify()

    def test_wrong_vm_or_owner(self):
        _, alternate = self.key_pair('alternate')
        self.verify(public=alternate, rejection='differs from the enrolled owner key')
        config = json.loads(self.config.read_bytes())
        config['config']['id'] = str(uuid.uuid4())
        self.write_json(self.config, config)
        self.verify(rejection='differs from the enrolled identity')

    def test_duplicate_vm_fields(self):
        raw = self.config.read_bytes()
        changed = raw.replace(b'"backend": "crosvm"', b'"backend": "other", "backend": "crosvm"')
        self.assertNotEqual(changed, raw)
        self.config.write_bytes(changed)
        self.verify(rejection='duplicate fields')

    def test_modified_manifest_and_signature(self):
        intact = self.envelope.read_bytes()
        modified = json.loads(intact)
        modified['manifest']['challenge'] = '0' * 64
        self.write_json(self.envelope, modified)
        self.verify(rejection='Invalid launch manifest signature')
        modified = json.loads(intact)
        raw = base64.b64decode(modified['signature'])
        modified['signature'] = base64.b64encode(bytes([raw[0] ^ 1]) + raw[1:]).decode()
        self.write_json(self.envelope, modified)
        self.verify(rejection='Invalid launch manifest signature')
        self.envelope.write_bytes(intact)
        self.verify()

    def test_expired_challenge(self):
        file = self.state / 'challenge.json'
        ticket = json.loads(file.read_bytes())
        elapsed = int(time.time()) - ticket['expires_at'] - 5
        ticket['issued_at'] += elapsed
        ticket['expires_at'] += elapsed
        self.write_json(file, ticket)
        self.verify(rejection='outside its validity window')

    def test_only_one_concurrent_verification_succeeds(self):
        def verify_once():
            command = [*CLI, 'verify', *map(str, self.inputs(self.state)), '--public-key', str(self.public)]
            return subprocess.run(command, capture_output=True, text=True, timeout=30)

        with ThreadPoolExecutor(2) as workers:
            results = list(workers.map(lambda _: verify_once(), range(2)))
        self.assertEqual(sorted(result.returncode for result in results), [0, 1])
        self.assertIn('already been redeemed', next(result.stderr for result in results if result.returncode))


if __name__ == '__main__':
    unittest.main()
