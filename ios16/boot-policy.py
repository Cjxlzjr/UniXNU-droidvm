#!/usr/bin/env python3
"""Offline owner authorization for an iOS 16 DroidVM kernel probe."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import subprocess
from tempfile import TemporaryDirectory
import time
import uuid

DOMAIN = 'org.unixnu.droidvm.ios16.probe.v1'
TARGET = 'iPhone10,3/D22AP/20H392'
LIFETIME = 600
SHA256 = re.compile(r'[0-9a-f]{64}')
ED25519_SPKI = bytes.fromhex('302a300506032b6570032100')


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode()


def read_small(path, limit=16 << 10):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError(f'Invalid file size for {path}')
    return data


def write_once(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def openssl(*arguments, error):
    result = subprocess.run(['openssl', *map(str, arguments)], stdin=subprocess.DEVNULL,
                            capture_output=True, timeout=20)
    if result.returncode:
        raise ValueError(error)
    return result.stdout


def public_der(path, *, private=False):
    kind = [] if private else ['-pubin']
    der = openssl('pkey', *kind, '-in', path, '-pubout', '-outform', 'DER',
                  error='Cannot read the owner key')
    if len(der) != len(ED25519_SPKI) + 32 or not der.startswith(ED25519_SPKI):
        raise ValueError('Require an Ed25519 owner key')
    return der


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_window(ticket):
    if not ticket['issued_at'] <= int(time.time()) < ticket['expires_at']:
        raise ValueError('Launch challenge is outside its validity window')


def read_ticket(state):
    if (state / 'redeemed').exists():
        raise ValueError('Launch challenge has already been redeemed')
    raw = read_small(state / 'challenge.json')
    ticket = json.loads(raw)
    if (not isinstance(ticket, dict)
            or set(ticket) != {'domain', 'target', 'vm_id', 'owner_key_sha256', 'challenge', 'issued_at', 'expires_at'}
            or ticket['domain'] != DOMAIN or ticket['target'] != TARGET
            or str(uuid.UUID(ticket['vm_id'])) != ticket['vm_id']
            or not SHA256.fullmatch(ticket['owner_key_sha256']) or not SHA256.fullmatch(ticket['challenge'])
            or type(ticket['issued_at']) is not int or type(ticket['expires_at']) is not int
            or ticket['expires_at'] != ticket['issued_at'] + LIFETIME or encode(ticket) != raw):
        raise ValueError('Invalid local launch challenge')
    check_window(ticket)
    return ticket


def issue(args):
    if not SHA256.fullmatch(args.owner_key_sha256):
        raise ValueError('Require the enrolled public-key fingerprint from key-id')
    now = int(time.time())
    ticket = {'domain': DOMAIN, 'target': TARGET, 'vm_id': str(args.vm_id),
              'owner_key_sha256': args.owner_key_sha256, 'challenge': secrets.token_hex(32),
              'issued_at': now, 'expires_at': now + LIFETIME}
    args.state.mkdir(mode=0o700)
    write_once(args.state / 'challenge.json', encode(ticket))


def measure(path):
    if not path.is_file():
        raise ValueError(f'Require a regular artifact file: {path}')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        image_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if before.st_size <= 0 or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError(f'Artifact was empty or changed during measurement: {path}')
    return {'bytes': after.st_size, 'sha256': image_hash}


def unique_fields(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError('VM configuration contains duplicate fields')
    return result


def manifest_for(args, ticket):
    raw = read_small(args.vm_config, 64 << 10)
    config = json.loads(raw, object_pairs_hook=unique_fields)['config']
    if (config['id'], config['backend'], config['hypervisor']) != (ticket['vm_id'], 'crosvm', 'gunyah'):
        raise ValueError('VM configuration differs from the enrolled identity or platform')
    disks = config['disks']
    if len(disks) != 1 or disks[0]['readonly'] is not True or disks[0]['bus'] != 'virtio':
        raise ValueError('Probe policy requires one read-only VirtIO boot disk')
    firmware, disk = config['boot']['uefi']['firmware'], disks[0]['path']
    if PurePosixPath(firmware).name != args.firmware.name or PurePosixPath(disk).name != args.boot_disk.name:
        raise ValueError('Measured filenames differ from the VM configuration')
    return {**ticket, 'artifacts': {
        'firmware': {'vm_path': firmware, **measure(args.firmware)},
        'boot_disk': {'vm_path': disk, **measure(args.boot_disk)},
        'vm_config': {'bytes': len(raw), 'sha256': digest(raw)},
    }}


def sign(args):
    ticket = read_ticket(args.state)
    if digest(public_der(args.private_key, private=True)) != ticket['owner_key_sha256']:
        raise ValueError('Signing key differs from the enrolled owner key')
    manifest = manifest_for(args, ticket)
    with TemporaryDirectory() as temp:
        content = Path(temp) / 'manifest.json'
        content.write_bytes(encode(manifest))
        signature = openssl('pkeyutl', '-sign', '-rawin', '-inkey', args.private_key, '-in', content,
                            error='Could not sign the launch manifest')
    if len(signature) != 64:
        raise ValueError('Invalid Ed25519 signature size')
    check_window(ticket)
    write_once(args.envelope, encode({'manifest': manifest, 'signature': base64.b64encode(signature).decode()}))


def verify(args):
    ticket = read_ticket(args.state)
    der = public_der(args.public_key)
    if digest(der) != ticket['owner_key_sha256']:
        raise ValueError('Public key differs from the enrolled owner key')
    raw = read_small(args.envelope)
    envelope = json.loads(raw)
    if not isinstance(envelope, dict) or set(envelope) != {'manifest', 'signature'} or encode(envelope) != raw:
        raise ValueError('Invalid launch envelope')
    signature = base64.b64decode(envelope['signature'], validate=True)
    if len(signature) != 64 or base64.b64encode(signature).decode() != envelope['signature']:
        raise ValueError('Invalid Ed25519 signature encoding')
    message = encode(envelope['manifest'])
    with TemporaryDirectory() as temp:
        work = Path(temp)
        content, key, sig = (work / name for name in ('manifest.json', 'key.der', 'manifest.sig'))
        content.write_bytes(message)
        key.write_bytes(der)
        sig.write_bytes(signature)
        openssl('pkeyutl', '-verify', '-rawin', '-pubin', '-keyform', 'DER',
                '-inkey', key, '-in', content, '-sigfile', sig, error='Invalid launch manifest signature')
    if message != encode(manifest_for(args, ticket)):
        raise ValueError('Signed manifest differs from the challenge or measured VM inputs')
    check_window(ticket)
    try:
        write_once(args.state / 'redeemed', (digest(message) + '\n').encode())
    except FileExistsError as exc:
        raise ValueError('Launch challenge has already been redeemed') from exc
    print(f'Authorized the offline probe inputs for {ticket["vm_id"]}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    key_id = commands.add_parser('key-id')
    key_id.add_argument('--public-key', type=Path, required=True)
    new = commands.add_parser('issue')
    new.add_argument('--state', type=Path, required=True)
    new.add_argument('--vm-id', type=uuid.UUID, required=True)
    new.add_argument('--owner-key-sha256', required=True)
    for action, key_option in ((sign, '--private-key'), (verify, '--public-key')):
        sub = commands.add_parser(action.__name__)
        for option in ('--state', '--vm-config', '--firmware', '--boot-disk', '--envelope', key_option):
            sub.add_argument(option, type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'key-id':
            print(digest(public_der(args.public_key)))
        else:
            {'issue': issue, 'sign': sign, 'verify': verify}[args.command](args)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f'{exc}\n')


if __name__ == '__main__':
    main()
