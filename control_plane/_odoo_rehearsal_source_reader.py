"""The existing host-held source identity, shared by capture and preflight."""

REHEARSAL_SOURCE_READER_LIBRARY = r"""
def rehearsal_source_reader(values):
    for name in ('ODOO_UPSTREAM_HOST','ODOO_UPSTREAM_USER','ODOO_UPSTREAM_DB_USER','ODOO_UPSTREAM_DB_NAME'):
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]*',values[name]):
            raise RuntimeError('Unsupported source coordinate')
    filestore=values['ODOO_UPSTREAM_FILESTORE_PATH']
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',filestore) or '..' in Path(filestore).parts:
        raise RuntimeError('Unsupported source filestore')
    directory=Path(values.get('DATA_WORKFLOW_SSH_DIR') or '/home/ubuntu/.ssh')
    key=values.get('DATA_WORKFLOW_SSH_KEY','')
    for prefix in ('/root/.ssh/','/home/ubuntu/.ssh/'):
        if key.startswith(prefix):
            key=str(directory/Path(key).name)
            break
    if not key:
        key=next((str(directory/name) for name in ('id_ed25519','id_ecdsa','id_rsa') if (directory/name).is_file()),'')
    if not key or not Path(key).is_file() or not (directory/'known_hosts').is_file():
        raise RuntimeError('Existing read-only source identity is unavailable')
    ssh=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes',
        '-o','UserKnownHostsFile='+str(directory/'known_hosts'),'-i',key]
    return ssh,values['ODOO_UPSTREAM_USER']+'@'+values['ODOO_UPSTREAM_HOST'],filestore
"""
