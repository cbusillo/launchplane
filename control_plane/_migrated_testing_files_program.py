MIGRATED_TESTING_FILES_PROGRAM = r"""
import hashlib,json,os,re,shutil,stat,tarfile,tempfile
from pathlib import Path,PurePosixPath
source=capture['source_observation']
root=Path('/volumes/data')/('.lp-native-'+identity)
if root.is_symlink() or not root.is_dir():
    raise RuntimeError('Migrated staging volume is unavailable')
artifacts={part:root/(part+'.artifact') for part in ('database','filestore')}
whole={}
for part,path in artifacts.items():
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError('Unsafe migrated artifact')
    if path.exists():
        with path.open('rb') as handle:
            digest=hashlib.file_digest(handle,'sha256').hexdigest()
        if path.stat().st_size!=capture[part]['size'] or digest!=capture[part]['sha256']:
            raise RuntimeError('Migrated artifact integrity differs')
        whole[part]=capture[part]
if mode=='inspect':
    print(json.dumps({'chunks':[],'artifacts':whole}));sys.exit(0)
if mode=='cleanup':
    for path in artifacts.values():
        path.unlink(missing_ok=True)
    sys.exit(0)
if mode!='prepare' or len(whole)!=2:
    raise RuntimeError('Migrated pair is incomplete')
database=spec['database_name']
if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}',database):
    raise RuntimeError('Invalid testing database')
filestore=Path('/volumes/data/filestore')/database
if filestore.resolve()!=filestore.absolute():
    raise RuntimeError('Unsafe testing filestore')
filestore.mkdir(parents=True,exist_ok=True)
if shutil.disk_usage(filestore).free < source['filestore_expanded_bytes'] + capture['database']['size']:
    raise RuntimeError('Insufficient space for complete migrated restore')
seen=set();total=0
with tarfile.open(artifacts['filestore'],'r|gz') as archive:
    for member in archive:
        relative=PurePosixPath(member.name)
        if relative.is_absolute() or '..' in relative.parts or member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
            raise RuntimeError('Unsafe migrated archive entry')
        if member.isdir():
            continue
        name=str(relative)
        if not re.fullmatch(r'(?:checklist/)?[a-f0-9]{2}/[a-f0-9]{40}',name) or name in seen:
            raise RuntimeError('Unsupported or duplicate migrated attachment')
        seen.add(name);total+=member.size
        if total>source['filestore_expanded_bytes'] or len(seen)>source['filestore_files']:
            raise RuntimeError('Migrated filestore exceeds its declared inventory')
        path=filestore/name
        if path.resolve()!=path.absolute() or path.is_symlink():
            raise RuntimeError('Unsafe migrated attachment destination')
        path.parent.mkdir(exist_ok=True)
        temporary=None
        try:
            digest=hashlib.sha256();size=0
            with archive.extractfile(member) as content, tempfile.NamedTemporaryFile(dir=path.parent,prefix='.lp-native-'+identity+'-',delete=False) as output:
                temporary=Path(output.name)
                while data:=content.read(1024*1024):
                    size+=len(data);digest.update(data);output.write(data)
                output.flush();os.fsync(output.fileno())
            if size!=member.size:
                raise RuntimeError('Migrated attachment content differs from its identity')
            if path.exists():
                if not path.is_file() or path.stat().st_size!=size:
                    raise RuntimeError('Existing migrated attachment differs')
                with path.open('rb') as handle:
                    if hashlib.file_digest(handle,'sha256').hexdigest()!=digest.hexdigest():
                        raise RuntimeError('Existing migrated attachment differs')
            else:
                os.link(temporary,path)
                directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
if (len(seen),total)!=(source['filestore_files'],source['filestore_expanded_bytes']):
    raise RuntimeError('Migrated filestore inventory is incomplete')
destination=Path(spec['dump_path'])
if destination.parent!=Path('/tmp') or not re.fullmatch(r'lp-testing-[a-f0-9]{32}-[a-f0-9]{32}-database.dump',destination.name) or destination.exists() or destination.is_symlink():
    raise RuntimeError('Unsafe testing dump destination')
with destination.open('xb') as output, artifacts['database'].open('rb') as content:
    shutil.copyfileobj(content,output,1024*1024);output.flush();os.fsync(output.fileno())
os.chmod(destination,0o644)
"""
