"""Read-only comparison with the retained original capture, without extraction."""

REHEARSAL_CAPTURE_COMPARISON_LIBRARY = r"""
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
import selectors


def valid_attachment_reference(value):
    return (isinstance(value,dict)
        and set(value)=={'name','checksum','size','copied_size','copied_sha1'}
        and isinstance(value['checksum'],str)
        and re.fullmatch('[a-f0-9]{40}',value['checksum'])
        and value['name']==value['checksum'][:2]+'/'+value['checksum']
        and isinstance(value['copied_sha1'],str)
        and re.fullmatch('[a-f0-9]{40}',value['copied_sha1'])
        and type(value['size']) is int and value['size']>=0
        and type(value['copied_size']) is int and value['copied_size']>=0
        and value['copied_size']!=value['size']
        and value['copied_sha1']!=value['checksum'])


def capture_comparison_budget(size,limit):
    if type(size) is not int or size<0 or type(limit) is not int or limit<=0:
        raise ValueError('Capture comparison budget is unverified')
    # Allow a cold sequential digest plus a worst-case full member read at
    # modest throughput. The service derives the cap from the native job limit.
    throughput=32*1024*1024
    return min(limit,max(600,60+(2*size+throughput-1)//throughput))


@contextmanager
def capture_comparison_file(path):
    descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(descriptor,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Capture comparison requires regular custody')
        yield stream,before
        after=os.fstat(stream.fileno())
        fields=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if any(getattr(before,key)!=getattr(after,key) for key in fields):
            raise ValueError('Capture comparison custody changed')


def captured_attachment_table_chunks(stream,containers,deadline,output_limit):
    # Decode selected table data to a pipe. No database connection, SQL
    # execution, output file, dump copy or application runtime is involved.
    remaining=max(1,int(deadline-time.monotonic()))
    arguments=['docker','exec','-i',containers['database'][0],
        'timeout','--signal=TERM','--kill-after=5',str(remaining),'pg_restore','--data-only',
        '--schema=public','--table=ir_attachment','--no-owner','--no-acl','--file=-']
    process=subprocess.Popen(arguments,stdin=stream,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            consumed=0
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not selector.select(remaining):
                    raise TimeoutError('Captured database observation expired')
                chunk=os.read(process.stdout.fileno(),65536)
                if not chunk:
                    break
                consumed+=len(chunk)
                if output_limit is not None and consumed>output_limit:
                    raise ValueError('Captured database output exceeds its bound')
                yield chunk
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise TimeoutError('Captured database observation expired')
        status=process.wait(timeout=remaining)
        if status==124:
            raise TimeoutError('Captured database observation expired')
        if status!=0:
            raise ValueError('Captured database decoding is unavailable')
    except subprocess.TimeoutExpired as error:
        raise TimeoutError('Captured database observation expired') from error
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()


def captured_attachment_rows(chunks: Iterable[bytes]) -> Iterator[dict[str, bytes]]:
    prefix=b'COPY public.ir_attachment ('
    suffix=b') FROM stdin;\n'
    columns=None
    complete=False
    metadata=b''
    index=0
    offset=0
    marker=b''
    selected={}
    values={}
    for chunk in chunks:
        for part in re.split(b'([\t\n])',chunk):
            if columns is None or complete:
                metadata+=part
                if len(metadata)>65536:
                    raise ValueError('Captured database metadata exceeds its bound')
                if part!=b'\n':
                    continue
                line,metadata=metadata,b''
                if not line.startswith(b'COPY '):
                    continue
                if columns is not None or not line.startswith(prefix) or not line.endswith(suffix):
                    raise ValueError('Captured attachment table is ambiguous')
                columns=line[len(prefix):-len(suffix)].split(b', ')
                keys=(b'store_fname',b'checksum',b'file_size')
                if len(columns)!=len(set(columns)) or not all(key in columns for key in keys):
                    raise ValueError('Captured attachment columns are unavailable')
                selected={columns.index(key):key.decode() for key in keys}
                values={key:b'' for key in selected.values()}
                continue
            if part not in (b'\t',b'\n'):
                if index==0:
                    marker=(marker+part)[:3]
                if index in selected:
                    key=selected[index]
                    values[key]+=part
                    if len(values[key])>64:
                        raise ValueError('Captured attachment field exceeds its bound')
                offset+=len(part)
                continue
            if part==b'\n' and index==0 and offset==2 and marker==b'\\.':
                complete=True
                continue
            if part==b'\t':
                index+=1
                if index>=len(columns):
                    raise ValueError('Captured attachment row has excess fields')
                offset=0
                continue
            if index+1!=len(columns):
                raise ValueError('Captured attachment row is incomplete')
            yield values
            values={key:b'' for key in selected.values()}
            index,offset,marker=0,0,b''
    if not complete or metadata:
        raise ValueError('Captured attachment table is incomplete')


def captured_attachment_reference_present(chunks,reference):
    expected={'store_fname':reference['name'].encode(),'checksum':reference['checksum'].encode(),
        'file_size':str(reference['size']).encode()}
    found=False
    for row in captured_attachment_rows(chunks):
        found=found or row==expected
    return found


def compare_captured_database_reference(spec,root,containers,reference):
    if not valid_attachment_reference(reference):
        raise RuntimeError('Attachment observation is unverified')
    try:
        raw=root/'raw-source'
        if root.resolve()!=root.absolute() or raw.resolve()!=raw.absolute() or not raw.is_dir():
            return 'unavailable'
        with capture_comparison_file(root/'capture.json') as (stream,_):
            encoded=stream.read(4097)
            if len(encoded)>4096:
                return 'unavailable'
            receipt=json.loads(encoded)
        if (not isinstance(receipt,dict)
                or set(receipt)!={'source_binding_sha256','database_sha256','filestore_sha256'}
                or receipt['source_binding_sha256']!=spec['record']['source_binding_sha256']
                or any(not isinstance(receipt[key],str) or not re.fullmatch('[a-f0-9]{64}',receipt[key])
                    for key in receipt)):
            return 'unavailable'
        deadline=time.monotonic()+600
        with capture_comparison_file(raw/'database.dump') as (stream,metadata):
            bound=spec['record']['storage']['database_bytes']
            if type(bound) is not int or bound<=0 or not 0<metadata.st_size<=bound:
                return 'unavailable'
            value=hashlib.sha256()
            consumed=0
            while True:
                if time.monotonic()>=deadline:
                    raise TimeoutError('Captured database observation expired')
                chunk=stream.read(1048576)
                if not chunk:
                    break
                consumed+=len(chunk)
                if consumed>metadata.st_size:
                    return 'unavailable'
                value.update(chunk)
            if consumed!=metadata.st_size or value.hexdigest()!=receipt['database_sha256']:
                return 'unavailable'
            stream.seek(0)
            lines=captured_attachment_table_chunks(stream,containers,deadline,bound)
            try:
                found=captured_attachment_reference_present(lines,reference)
            finally:
                close=getattr(lines,'close',None)
                if close is not None:
                    close()
        return 'reference_present' if found else 'reference_not_found'
    except TimeoutError:
        return 'incomplete'
    except (OSError,ValueError,TypeError,KeyError):
        return 'unavailable'


def compare_retained_attachment(spec,root,reference):
    if not valid_attachment_reference(reference):
        raise RuntimeError('Attachment observation is unverified')
    try:
        raw=root/'raw-source'
        if root.resolve()!=root.absolute() or raw.resolve()!=raw.absolute() or not raw.is_dir():
            return 'capture_comparison_unavailable'
        with capture_comparison_file(root/'capture.json') as (stream,_):
            encoded=stream.read(4097)
            if len(encoded)>4096:
                return 'capture_comparison_unavailable'
            receipt=json.loads(encoded)
        if (not isinstance(receipt,dict)
                or set(receipt)!={'source_binding_sha256','database_sha256','filestore_sha256'}
                or receipt['source_binding_sha256']!=spec['record']['source_binding_sha256']
                or any(not isinstance(receipt[key],str) or not re.fullmatch('[a-f0-9]{64}',receipt[key])
                    for key in receipt)):
            return 'capture_comparison_unavailable'
        target=None
        with capture_comparison_file(raw/'filestore.tar') as (stream,metadata):
            bound=spec['record']['storage']['filestore_bytes']
            if (type(bound) is not int or bound<=0 or metadata.st_size>bound
                    or metadata.st_size%tarfile.BLOCKSIZE):
                return 'capture_comparison_unavailable'
            deadline=time.monotonic()+capture_comparison_budget(
                metadata.st_size,spec['capture_comparison_timeout_seconds'])
            value=hashlib.sha256()
            consumed=0
            def read(size):
                nonlocal consumed
                if time.monotonic()>=deadline:
                    raise TimeoutError('Capture comparison budget expired')
                data=stream.read(size)
                consumed+=len(data)
                if consumed>metadata.st_size:
                    raise ValueError('Capture comparison custody changed')
                value.update(data)
                return data
            # Verify every byte before interpreting the archive, then use the
            # standard tar reader for bounded headers and the one member. No
            # extraction or raw bytes enter an application container.
            while read(1048576):
                pass
            if consumed!=metadata.st_size or value.hexdigest()!=receipt['filestore_sha256']:
                return 'capture_comparison_unavailable'
            stream.seek(0)
            class Reader:
                def read(self,size):
                    if time.monotonic()>=deadline:
                        raise TimeoutError('Capture comparison budget expired')
                    if not 0<=size<=1048576:
                        raise ValueError('Capture comparison metadata exceeds its bound')
                    return stream.read(size)
                def seek(self,offset,whence=0):
                    if time.monotonic()>=deadline:
                        raise TimeoutError('Capture comparison budget expired')
                    position=(offset if whence==os.SEEK_SET else
                        stream.tell()+offset if whence==os.SEEK_CUR else
                        metadata.st_size+offset if whence==os.SEEK_END else -1)
                    if not 0<=position<=metadata.st_size:
                        raise ValueError('Capture comparison archive exceeds its bound')
                    return stream.seek(position)
                def tell(self):
                    return stream.tell()
            reader=Reader()
            with tarfile.open(fileobj=reader,mode='r:') as archive:
                while True:
                    member=archive.next()
                    if member is None:
                        # A second concatenated archive is ambiguous evidence.
                        reader.seek(archive.offset)
                        tail_bytes=0
                        while True:
                            tail=reader.read(1048576)
                            if not tail:
                                break
                            if any(tail):
                                raise ValueError('Capture comparison archive has trailing data')
                            tail_bytes+=len(tail)
                        if tail_bytes<2*tarfile.BLOCKSIZE:
                            raise ValueError('Capture comparison archive is incomplete')
                        break
                    archive.members.clear()
                    if not (member.isfile() or member.isdir()) or member.size<0 or member.size>bound:
                        raise ValueError('Capture comparison archive has unsupported members')
                    parts=member.name.split('/')
                    while parts and parts[0]=='.':
                        parts.pop(0)
                    if parts and parts[-1]=='' and member.isdir():
                        parts.pop()
                    if (any(part in {'','..','.'} for part in parts) or '\\' in member.name
                            or not parts and not member.isdir() or member.isdir() and member.size):
                        raise ValueError('Capture comparison archive has unsafe paths')
                    if '/'.join(parts)!=reference['name']:
                        continue
                    if target is not None or not member.isfile():
                        raise ValueError('Capture comparison archive is ambiguous')
                    content=hashlib.sha1()
                    size=0
                    with archive.extractfile(member) as attachment:
                        while True:
                            if time.monotonic()>=deadline:
                                raise TimeoutError('Capture comparison budget expired')
                            chunk=attachment.read(1048576)
                            if not chunk:
                                break
                            size+=len(chunk)
                            content.update(chunk)
                    if size!=member.size:
                        raise ValueError('Capture comparison archive is incomplete')
                    target=(size,content.hexdigest())
        if target is None:
            return 'capture_attachment_not_present'
        size,checksum=target
        if checksum==reference['checksum']:
            return ('capture_attachment_matches_reference_copy_differs' if size==reference['size']
                else 'capture_attachment_checksum_matches_size_metadata_differs')
        if (size,checksum)==(reference['copied_size'],reference['copied_sha1']):
            return 'capture_attachment_matches_copy_reference_differs'
        return 'capture_attachment_differs_from_copy_and_reference'
    except TimeoutError:
        return 'capture_comparison_incomplete'
    except (OSError,ValueError,TypeError,KeyError,tarfile.TarError):
        # Exception text may contain a Client attachment path. Never publish it.
        return 'capture_comparison_unavailable'
"""
