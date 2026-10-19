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


def validate_attachment_files(rows, root, deadline):
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
            if metadata.st_size != size:
                return 'attachment_size_mismatch'
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
            if value.hexdigest() != checksum:
                return 'attachment_hash_mismatch'
        except FileNotFoundError:
            return 'attachment_missing'
        except OSError:
            return 'attachment_unreadable'
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return 'verified'


def attachment_validation_main():
    try:
        import psycopg2
    except ImportError:
        return 'dependency_unavailable'
    try:
        database = os.environ['ODOO_DB_NAME']
        user = os.environ['ODOO_DB_USER']
        password = os.environ['ODOO_DB_PASSWORD']
        bounded = sys.argv[1] == 'observe'
    except (KeyError, IndexError):
        return 'environment_unavailable'
    try:
        connection = psycopg2.connect(host='database', user=user, password=password,
            dbname=database, connect_timeout=5)
    except Exception:
        return 'database_unavailable'
    try:
        connection.set_session(readonly=True)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
        deadline = time.monotonic()+20 if bounded else None
        # A server cursor keeps a large attachment table out of client memory.
        with connection.cursor(name='launchplane_rehearsal_attachments') as cursor:
            cursor.itersize = 256
            cursor.execute('SELECT store_fname,checksum,file_size FROM ir_attachment WHERE store_fname IS NOT NULL')
            return validate_attachment_files(cursor,
                Path('/volumes/data/filestore')/database, deadline)
    except Exception:
        return 'attachment_query_unavailable'
    finally:
        connection.rollback()
        connection.close()


if __name__ == '__main__':
    result = attachment_validation_main()
    print('attachment_validation='+json.dumps({'reason':result}).encode().hex())
"""
