"""Read-only comparison with the retained original capture, without extraction."""

REHEARSAL_CAPTURE_COMPARISON_LIBRARY = r"""
from contextlib import contextmanager


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
        deadline=time.monotonic()+600
        target=None
        with capture_comparison_file(raw/'filestore.tar') as (stream,metadata):
            bound=spec['record']['storage']['filestore_bytes']
            if (type(bound) is not int or bound<=0 or metadata.st_size>bound
                    or metadata.st_size%tarfile.BLOCKSIZE):
                return 'capture_comparison_unavailable'
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
        return 'incomplete'
    except (OSError,ValueError,TypeError,KeyError,tarfile.TarError):
        # Exception text may contain a Client attachment path. Never publish it.
        return 'capture_comparison_unavailable'
"""
