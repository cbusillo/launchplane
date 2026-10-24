"""Read-only validation with content-free evidence for the rejected attachment."""

ATTACHMENT_METADATA_LIMITS = {"name": 4096, "res_model": 255, "mimetype": 255}
ATTACHMENT_EVIDENCE_MAX_BYTES = 128 * 1024
ATTACHMENT_MISMATCH_RECEIPT_NAME = "execute.attachment-mismatch.json"

ATTACHMENT_MISMATCH_LIBRARY = (
    f"ATTACHMENT_METADATA_LIMITS = {ATTACHMENT_METADATA_LIMITS!r}\n"
    f"ATTACHMENT_EVIDENCE_MAX_BYTES = {ATTACHMENT_EVIDENCE_MAX_BYTES!r}\n"
    f"ATTACHMENT_MISMATCH_RECEIPT_NAME = {ATTACHMENT_MISMATCH_RECEIPT_NAME!r}\n"
    + r"""
def valid_attachment_mismatch(value):
    fields={'reason','store_fname','name','res_model','res_id','mimetype',
        'database_size','database_checksum','file_size','file_checksum','file_status','truncated_fields'}
    if not isinstance(value,dict) or set(value)!=fields:
        return False
    if any(value[key] is not None and (not isinstance(value[key],str) or len(value[key])>limit)
            for key,limit in ATTACHMENT_METADATA_LIMITS.items()):
        return False
    truncated=value['truncated_fields']
    if (not isinstance(truncated,list) or any(not isinstance(key,str) or key not in ATTACHMENT_METADATA_LIMITS for key in truncated)
            or len(truncated)!=len(set(truncated))):
        return False
    if value['res_id'] is not None and type(value['res_id']) is not int:
        return False
    if any(type(value[key]) is not int or value[key]<0 for key in ('database_size','file_size')):
        return False
    if not isinstance(value['database_checksum'],str) or not re.fullmatch('[a-f0-9]{40}',value['database_checksum']):
        return False
    measured=value['file_status']=='measured'
    if measured:
        if not isinstance(value['file_checksum'],str) or not re.fullmatch('[a-f0-9]{40}',value['file_checksum']):
            return False
    elif (value['file_status'] not in {'unreadable','changed'} or value['file_checksum'] is not None
            or value['reason']!='attachment_size_mismatch'):
        return False
    if value['store_fname']!=value['database_checksum'][:2]+'/'+value['database_checksum']:
        return False
    return ((value['reason']=='attachment_size_mismatch' and value['database_size']!=value['file_size'])
        or (value['reason']=='attachment_hash_mismatch' and measured and value['database_size']==value['file_size']
            and value['database_checksum']!=value['file_checksum']))
"""
)

ATTACHMENT_VALIDATION_LIBRARY = (
    ATTACHMENT_MISMATCH_LIBRARY
    + r"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time


def record_attachment_mismatch(row,metadata,mismatch,reason,*,file_checksum=None,file_status='unreadable'):
    if len(row)!=7:
        return False
    evidence={'reason':reason,'store_fname':row[0],'name':row[3],
        'res_model':row[4],'res_id':row[5],'mimetype':row[6],
        'database_size':row[2],'database_checksum':row[1],
        'file_size':metadata.st_size,'file_checksum':file_checksum,
        'file_status':file_status,'truncated_fields':[]}
    for key,limit in ATTACHMENT_METADATA_LIMITS.items():
        if isinstance(evidence[key],str) and len(evidence[key])>limit:
            evidence[key]=evidence[key][:limit]
            evidence['truncated_fields'].append(key)
    if not valid_attachment_mismatch(evidence):
        return False
    mismatch.update(evidence)
    return True


def validate_attachment_files(rows, root, deadline, *, check_content=True, classify_size=False,
        failure_reference=None, mismatch=None):
    if mismatch is not None:
        mismatch.clear()
    if root.resolve() != root.absolute() or not root.is_dir():
        return 'attachment_path_unsafe'
    for row in rows:
        name, checksum, size = row[:3]
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
            if size_mismatch and not classify_size and mismatch is None:
                return 'attachment_size_mismatch'
            if size_mismatch and mismatch is not None and not record_attachment_mismatch(
                    row,metadata,mismatch,'attachment_size_mismatch'):
                return 'attachment_metadata_invalid'
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
                    if mismatch:
                        mismatch['file_status']='changed'
                        return 'attachment_size_mismatch'
                    return 'attachment_unreadable'
            file_checksum=value.hexdigest()
            if mismatch is not None and (size_mismatch or file_checksum!=checksum):
                reason='attachment_size_mismatch' if size_mismatch else 'attachment_hash_mismatch'
                if not record_attachment_mismatch(row,metadata,mismatch,reason,
                        file_checksum=file_checksum,file_status='measured'):
                    return 'attachment_metadata_invalid'
                return reason
            if file_checksum != checksum:
                if size_mismatch and failure_reference is not None:
                    failure_reference.update({'name':name,'checksum':checksum,'size':size,
                        'copied_size':metadata.st_size,'copied_sha1':value.hexdigest()})
                return 'attachment_size_and_hash_mismatch' if size_mismatch else 'attachment_hash_mismatch'
            if size_mismatch:
                return 'attachment_content_matches_checksum_size_mismatch'
        except FileNotFoundError:
            return 'attachment_missing'
        except OSError:
            if mismatch:
                return 'attachment_size_mismatch'
            return 'attachment_unreadable'
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return 'verified'

"""
)

ATTACHMENT_VALIDATION_PROGRAM = (
    ATTACHMENT_VALIDATION_LIBRARY
    + r"""
def attachment_validation_main(*, failure_reference=None, mismatch=None):
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
                cursor.execute('SELECT store_fname,checksum,file_size,name,res_model,res_id,mimetype FROM ir_attachment WHERE store_fname IS NOT NULL')
                reason = validate_attachment_files(cursor,
                    Path('/volumes/data/filestore')/database, deadline,
                    check_content=check_content, classify_size=bounded,
                    failure_reference=failure_reference if sys.argv[1]=='compare' else None,
                    mismatch=mismatch if sys.argv[1]=='validate' else None)
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
    mismatch = {}
    result = attachment_validation_main(failure_reference=reference,mismatch=mismatch)
    payload = {'reason':result}
    if sys.argv[1:] == ['compare'] and result == 'attachment_size_and_hash_mismatch':
        payload['reference'] = reference
    if mismatch and result==mismatch['reason']:
        payload['mismatch'] = mismatch
    print('attachment_validation='+json.dumps(payload).encode().hex())
"""
)
