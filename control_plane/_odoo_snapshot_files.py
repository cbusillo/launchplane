CLEAN_SNAPSHOT_STATE_DIRECTORY = ".local/state/launchplane-clean-testing-snapshots"

SNAPSHOT_FILES_LIBRARY = (
    f"CLEAN_SNAPSHOT_STATE_DIRECTORY = {CLEAN_SNAPSHOT_STATE_DIRECTORY!r}\n"
    + r"""
import hashlib
import os
import selectors
import subprocess
import json
import tempfile
import time

inherited_lock_descriptors = ()


def snapshot_file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def capture_snapshot_file(arguments, path, bound, error_tail_limit=16385, timeout_seconds=None):
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        pass_fds=inherited_lock_descriptors)
    errors = b''
    error_bytes = 0
    size = 0
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    try:
        with path.open('xb') as output:
            os.chmod(path, 0o600)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'capture')
                selector.register(process.stderr, selectors.EVENT_READ, 'error')
                while selector.get_map():
                    if deadline is not None and time.monotonic() >= deadline:
                        raise RuntimeError('Source copy exceeded its execution budget')
                    for event, _ in selector.select(timeout=1 if deadline is not None else None):
                        chunk = os.read(event.fd, 65536)
                        if not chunk:
                            selector.unregister(event.fileobj)
                        elif event.data == 'error':
                            error_bytes += len(chunk)
                            errors = (errors + chunk)[-error_tail_limit:]
                        else:
                            size += len(chunk)
                            if size > bound:
                                raise RuntimeError('Source copy exceeds its reviewed storage bound')
                            output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        status = process.wait(timeout=30)
        if not size and not status:
            raise RuntimeError('Incomplete source copy')
        return status, errors, error_bytes > len(errors)
    finally:
        process.stdout.close()
        process.stderr.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def sync_snapshot_directory(path):
    for entry in path.rglob('*'):
        if entry.is_file():
            with entry.open('rb') as stream:
                os.fsync(stream.fileno())
    directories = [entry for entry in path.rglob('*') if entry.is_dir()]
    for directory in sorted(directories, key=lambda entry: len(entry.parts), reverse=True) + [path]:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_snapshot_receipt(path, payload):
    with tempfile.NamedTemporaryFile(mode='w', prefix='.pending-', dir=path.parent, delete=False) as stream:
        temporary = stream.name
        json.dump(payload, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


"""
)
