"""Value-free, read-only validation inside the isolated rehearsal runner."""

ATTACHMENT_VALIDATION_PROGRAM = r"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


def validate_attachment_files(rows, root, deadline, *, check_content=True, classify_size=False,
        failure_reference=None):
    if root.resolve() != root.absolute() or not root.is_dir():
        return 'attachment_path_unsafe'
    for name, checksum, size in rows:
        if deadline and time.monotonic() >= deadline:
            return 'incomplete'
        if (not isinstance(checksum, str) or not re.fullmatch('[a-f0-9]{40}', checksum)
                or name != checksum[:2]+'/'+checksum
                or type(size) is not int or size < 0):
            return 'attachment_metadata_invalid'
        path = root/name
        descriptor = None
        try:
            if path.parent.is_symlink() or path.is_symlink():
                return 'attachment_path_unsafe'
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return 'attachment_path_unsafe'
            size_mismatch = metadata.st_size != size
            if size_mismatch and not classify_size:
                return 'attachment_size_mismatch'
            if not check_content and not size_mismatch:
                continue
            value = hashlib.sha1()
            with os.fdopen(descriptor, 'rb') as stream:
                descriptor = None
                while True:
                    if deadline and time.monotonic() >= deadline:
                        return 'incomplete'
                    chunk = stream.read(1048576)
                    if not chunk:
                        break
                    value.update(chunk)
                after = os.fstat(stream.fileno())
                if (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    return 'attachment_unreadable'
            if value.hexdigest() != checksum:
                if size_mismatch and failure_reference is not None:
                    failure_reference.update({'name':name,'checksum':checksum,'size':size,
                        'copied_size':metadata.st_size,'copied_sha1':value.hexdigest()})
                return 'attachment_size_and_hash_mismatch' if size_mismatch else 'attachment_hash_mismatch'
            if size_mismatch:
                return 'attachment_content_matches_checksum_size_mismatch'
        except FileNotFoundError:
            return 'attachment_missing'
        except OSError:
            return 'attachment_unreadable'
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return 'verified'


def attachment_validation_main(*, failure_reference=None):
    try:
        import psycopg2
    except ImportError:
        return 'dependency_unavailable'
    try:
        database = os.environ['ODOO_DB_NAME']
        user = os.environ['ODOO_DB_USER']
        password = os.environ['ODOO_DB_PASSWORD']
        bounded = sys.argv[1] in {'observe','compare'}
    except (KeyError, IndexError):
        return 'environment_unavailable'
    try:
        connection = psycopg2.connect(host='database', user=user, password=password,
            dbname=database, connect_timeout=5)
    except Exception:
        return 'database_unavailable'
    try:
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
        deadline = time.monotonic()+600 if bounded else None
        # A server cursor keeps a large attachment table out of client memory.
        # Inspect metadata/presence across the table before spending the read
        # budget hashing early large files. Both passes use the same snapshot.
        for check_content in (False, True):
            with connection.cursor(name='launchplane_rehearsal_attachments') as cursor:
                cursor.itersize = 256
                cursor.execute('SELECT store_fname,checksum,file_size FROM ir_attachment WHERE store_fname IS NOT NULL')
                reason = validate_attachment_files(cursor,
                    Path('/volumes/data/filestore')/database, deadline,
                    check_content=check_content, classify_size=bounded,
                    failure_reference=failure_reference if sys.argv[1]=='compare' else None)
                if reason != 'verified':
                    return reason
        return 'verified'
    except Exception:
        return 'attachment_query_unavailable'
    finally:
        for cleanup in (connection.rollback, connection.close):
            try:
                cleanup()
            except Exception:
                # This session is read-only; a dead connection must not erase
                # the result already computed by the validator.
                pass


if __name__ == '__main__':
    reference = {}
    result = attachment_validation_main(failure_reference=reference)
    payload = {'reason':result}
    if sys.argv[1:] == ['compare'] and result == 'attachment_size_and_hash_mismatch':
        payload['reference'] = reference
    print('attachment_validation='+json.dumps(payload).encode().hex())
"""
