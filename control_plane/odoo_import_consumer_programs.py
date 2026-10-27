from control_plane.odoo_source_prerequisite_programs import (
    CONTAINER_LIBRARY,
    CONTAINER_RUN,
    HOST_LIBRARY,
    HOST_RUN,
)

CONSUMER_VALIDATOR = r"""
def validate_result(value,names):
    if (not isinstance(value,dict) or set(value)-{'runtime','sources','failure_stage'}
        or value.get('runtime') not in {'verified','unknown'}
        or value.get('failure_stage') not in (None,*failure_stages)
        or not isinstance(value.get('sources'),list) or len(value['sources'])!=len(names)):
        raise ValueError()
    found=[]
    for source in value['sources']:
        if (not isinstance(source,dict) or set(source)-{'source','producer','failure_stage','grants','parameters'}
            or source.get('source') not in names or source.get('producer') not in (None,'verified')
            or source.get('failure_stage') not in (None,*failure_stages)
            or source.get('grants','unknown') not in ('unknown','read_only','unsafe')):
            raise ValueError()
        found.append(source['source'])
        parameters=source.get('parameters',{})
        if (not isinstance(parameters,dict) or set(parameters)-{'host','user','db','port','ssl_verify'}
            or parameters and set(parameters)!={'host','user','db','port','ssl_verify'}
            or parameters and source['source']!='fishbowl'
            or any(not isinstance(item,str) or not item or len(item)>16384 for item in parameters.values())):
            raise ValueError()
    if set(found)!=set(names) or len(found)!=len(set(found)):
        raise ValueError()
"""

CONSUMER_LIBRARY = r"""
import subprocess
consumer_mode=__CONSUMER_MODE__

def effective_settings(name,module,payload_producer,payload,applied):
    prefix=spec['prefixes'][name]+'.'
    metadata=spec['metadata'][name]
    if metadata['binding']!='selected' or metadata['secret_reference']!='match':
        raise ValueError()
    expected=[]
    for key in module.PARAMETER_KEYS:
        if key==prefix+'password':
            continue
        fact=spec['authority'][name].get(key)
        if fact is None or not fact['known']:
            raise ValueError()
        if fact['present']:
            expected.append({'key':key,'value':{'source':'literal','value':fact['value']}})
    password_key=prefix+'password'
    secret_key=spec['password_keys'][name]
    authority=payload_producer.resolve_config_parameters({'config_parameters':expected},{},keys=module.PARAMETER_KEYS)
    delivered=payload_producer.resolve_config_parameters(payload,os.environ,keys=module.PARAMETER_KEYS)
    if not isinstance(delivered,dict) or any(not isinstance(value,str) or len(value)>16384 for value in delivered.values()):
        raise ValueError()
    delivered_public={key:value for key,value in delivered.items() if key!=password_key}
    if delivered_public!=authority or not delivered.get(password_key) or not hmac.compare_digest(delivered[password_key].encode(),os.environ.get(secret_key,'').encode()):
        raise ValueError()
    current={key:applied[key] for key in module.PARAMETER_KEYS if key in applied}
    effective={**current,**delivered}
    resolved=full_settings(module,current)
    selected=full_settings(module,effective)
    if resolved is None or selected is None:
        raise ValueError()
    options=options_projection(module,current)
    delivered_options=options_projection(module,effective)
    if projection(resolved)!=options or projection(selected)!=delivered_options or tls(options)!='enabled_with_verification':
        raise ValueError()
    fields=(*__GRANT_FIELDS__,'use_ssl','password')
    if consumer_mode=='preview':
        fields=(*fields,'database')
    for field in fields:
        left=getattr(resolved,field)
        right=getattr(selected,field)
        if type(left) is not type(right) or (not hmac.compare_digest(left.encode(),right.encode()) if isinstance(left,str) else left!=right):
            raise ValueError()
    return resolved

def source_result(name,module,payload_producer,payload,applied):
    with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
        settings=effective_settings(name,module,payload_producer,payload,applied)
    if consumer_mode=='preview':
        if name!='fishbowl':
            return {'source':name}
        return {'source':name,'parameters':{'host':settings.host,'user':settings.user,'db':settings.database,
                'port':str(settings.port),'ssl_verify':'True' if settings.ssl_verify else 'False'}}
    child=subprocess.run([sys.executable,'-I','-c',__PROBE__],
        input=json.dumps({'host':settings.host,'user':settings.user,'password':settings.password,'port':str(settings.port)}),
        text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
        env={key:os.environ[key] for key in ('SSL_CERT_FILE','SSL_CERT_DIR') if os.environ.get(key)},timeout=10,check=False)
    result={'source':name}
    if child.returncode==0 and len(child.stdout)<128:
        payload=json.loads(child.stdout)
        if payload in ({'grants':'read_only'},{'grants':'unsafe'}):
            result.update(payload)
    return result
"""

CONTAINER_PROGRAM = CONTAINER_LIBRARY + CONSUMER_LIBRARY + CONSUMER_VALIDATOR + CONTAINER_RUN
HOST_PROGRAM = (
    HOST_LIBRARY
    + CONSUMER_VALIDATOR
    + HOST_RUN.replace(
        "'payload_addon','payload_root'", "'payload_addon','payload_root','password_keys'"
    )
    .replace("'20s'", "'50s'")
    .replace("timeout=25", "timeout=55")
)
