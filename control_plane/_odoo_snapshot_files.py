SNAPSHOT_FILES_LIBRARY = r"""
import hashlib
import os
import selectors
import subprocess


def snapshot_file_digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def capture_snapshot_file(arguments, path, bound, error_tail_limit=16385):
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    errors = b''
    error_bytes = 0
    size = 0
    try:
        with path.open('xb') as output:
            os.chmod(path, 0o600)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, 'capture')
                selector.register(process.stderr, selectors.EVENT_READ, 'error')
                while selector.get_map():
                    for event, _ in selector.select():
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
        if not size:
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
"""
