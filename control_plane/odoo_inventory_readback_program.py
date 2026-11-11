from control_plane.odoo_source_prerequisite_programs import HOST_INSPECT_LIBRARY

INVENTORY_LIBRARY = r"""
def rows(cursor, statement, parameters=()):
    cursor.execute(statement, parameters)
    return cursor.fetchall()

def scalar(cursor, statement, parameters=()):
    return rows(cursor, statement, parameters)[0][0]

def columns(cursor, table):
    return {row[0] for row in rows(cursor,
        'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s', (table,))}

def read_inventory(cursor, spec):
    company = spec['company_id']
    if scalar(cursor, 'SELECT count(*) FROM res_company WHERE id = %s', (company,)) != 1:
        raise ValueError()
    tables = {name: columns(cursor, name) for name in (
        'external_id','external_system','product_template','product_product','product_supplierinfo',
        'stock_quant','stock_location','stock_move','cm_product_import_conflict','res_users','res_partner',
        'ir_model_data','ir_module_module','res_groups_users_rel')}
    bound = {'system_id','resource','res_model','res_id','external_id'} <= tables['external_id'] and 'code' in tables['external_system']
    parts_available = bound and 'id' in tables['product_template']
    part_ids = []
    if parts_available:
        part_ids = [row[0] for row in rows(cursor,
            "SELECT DISTINCT t.id FROM product_template t JOIN external_id e ON e.res_id=t.id "
            "JOIN external_system s ON s.id=e.system_id WHERE s.code='fishbowl' "
            "AND e.resource='part' AND e.res_model='product.template'")]
    facts = {key: None for key in (
        'parts','supplier_cost_rows','listing_bindings','nonzero_sale_price_parts','stockable_parts',
        'lot_tracked_parts','serial_tracked_parts','current_conflicts','parts_with_extra_details',
        'parts_with_upc','parts_with_packaging','stock_move_rows','done_stock_move_rows','internal_quantity')}
    if parts_available:
        facts['parts'] = len(part_ids)
        for key, field, predicate in (
            ('nonzero_sale_price_parts','list_price','list_price <> 0'),
            ('stockable_parts','is_storable','is_storable'),
            ('lot_tracked_parts','tracking',"tracking = 'lot'"),
            ('serial_tracked_parts','tracking',"tracking = 'serial'"),
            ('parts_with_extra_details','fishbowl_extra_details',"coalesce(fishbowl_extra_details, '') <> ''"),
            ('parts_with_upc','fishbowl_upc',"coalesce(fishbowl_upc, '') <> ''"),
            ('parts_with_packaging','fishbowl_packaging',"coalesce(fishbowl_packaging, '') <> ''"),
        ):
            if field in tables['product_template']:
                facts[key] = scalar(cursor, 'SELECT count(*) FROM product_template WHERE id = ANY(%s) AND ' + predicate, (part_ids,))
    if bound:
        facts['listing_bindings'] = scalar(cursor,
            "SELECT count(*) FROM external_id e JOIN external_system s ON s.id=e.system_id "
            "WHERE s.code='fishbowl' AND e.resource='product' AND e.res_model IN ('product.template','product.product')")
    suppliers_available = bound and {'id','company_id','product_tmpl_id','partner_id','price','currency_id','uom_id','product_code'} <= tables['product_supplierinfo']
    if suppliers_available:
        facts['supplier_cost_rows'] = scalar(cursor,
            "SELECT count(DISTINCT p.id) FROM product_supplierinfo p JOIN external_id e ON e.res_id=p.id "
            "JOIN external_system s ON s.id=e.system_id WHERE s.code='fishbowl' AND e.resource='vendorparts' "
            "AND e.res_model='product.supplierinfo' AND p.company_id=%s", (company,))
    if {'product_id','source','company_id'} <= tables['cm_product_import_conflict'] and parts_available:
        facts['current_conflicts'] = scalar(cursor,
            "SELECT count(*) FROM cm_product_import_conflict WHERE source='fishbowl' AND company_id=%s AND product_id=ANY(%s)", (company,part_ids))
    stock_available = (parts_available and {'id','product_tmpl_id'} <= tables['product_product']
        and {'product_id','location_id','company_id','quantity'} <= tables['stock_quant']
        and {'id','usage'} <= tables['stock_location'])
    quantity_sql = ("SELECT coalesce(sum(q.quantity),0)::text FROM stock_quant q JOIN stock_location l ON l.id=q.location_id "
        "JOIN product_product p ON p.id=q.product_id WHERE l.usage='internal' AND q.company_id=%s AND p.product_tmpl_id=ANY(%s)")
    if stock_available:
        facts['internal_quantity'] = scalar(cursor, quantity_sql, (company,part_ids))
    if parts_available and {'product_id','state','company_id'} <= tables['stock_move'] and {'id','product_tmpl_id'} <= tables['product_product']:
        move_counts = rows(cursor, "SELECT count(*),count(*) FILTER (WHERE m.state='done') FROM stock_move m "
            "JOIN product_product p ON p.id=m.product_id WHERE m.company_id=%s AND p.product_tmpl_id=ANY(%s)", (company,part_ids))[0]
        facts['stock_move_rows'],facts['done_stock_move_rows'] = move_counts
    witnesses = []
    for part_id in spec['part_ids']:
        witness = {'part_id':part_id,'status':'unavailable'}
        if parts_available:
            matches = rows(cursor, "SELECT t.id,CASE jsonb_typeof(to_jsonb(t)->'name') WHEN 'object' THEN to_jsonb(t)->'name'->>'en_US' ELSE to_jsonb(t)->>'name' END, "
                "to_jsonb(t)->>'default_code',to_jsonb(t)->>'list_price',to_jsonb(t)->>'tracking',t.active "
                "FROM product_template t JOIN external_id e ON e.res_id=t.id JOIN external_system s ON s.id=e.system_id "
                "WHERE s.code='fishbowl' AND e.resource='part' AND e.res_model='product.template' AND e.external_id=%s LIMIT 2", (part_id,))
            if len(matches)>1:
                raise ValueError()
            witness['status'] = 'present' if matches else 'missing'
            if matches:
                template,name,number,price,tracking,active = matches[0]
                witness.update(template_id=template,name=name,part_number=number,sale_price=price,tracking=tracking,active=active)
                if {'id','product_tmpl_id'} <= tables['product_product']:
                    variants = rows(cursor, "SELECT to_jsonb(p)->>'barcode',to_jsonb(p)->'standard_price'->>%s,to_jsonb(p)->>'default_code' FROM product_product p WHERE product_tmpl_id=%s LIMIT 2", (str(company),template))
                    if len(variants)==1:
                        witness['barcode'],witness['cost'],witness['part_number'] = variants[0]
                if stock_available:
                    witness['internal_quantity'] = scalar(cursor,quantity_sql,(company,[template]))
                if suppliers_available and {'id','name'} <= tables['res_partner']:
                    suppliers = rows(cursor, "SELECT p.partner_id,r.name,p.product_code,p.price::text,p.currency_id,p.uom_id "
                        "FROM product_supplierinfo p JOIN res_partner r ON r.id=p.partner_id "
                        "WHERE p.product_tmpl_id=%s AND p.company_id=%s AND EXISTS (SELECT 1 FROM external_id e "
                        "JOIN external_system s ON s.id=e.system_id WHERE s.code='fishbowl' AND e.resource='vendorparts' "
                        "AND e.res_model='product.supplierinfo' AND e.res_id=p.id) ORDER BY p.id LIMIT 11", (template,company))
                    witness['supplier_costs'] = [dict(zip(('supplier_id','supplier_name','supplier_part_number','cost','currency_id','unit_id'), row)) for row in suppliers[:10]]
                    witness['suppliers_truncated'] = len(suppliers)>10
        witnesses.append(witness)
    purchaser = {'xmlid':spec['purchaser_xmlid'],'status':'unavailable'}
    if {'name','state'} <= tables['ir_module_module']:
        modules = rows(cursor, 'SELECT state FROM ir_module_module WHERE name=%s LIMIT 2', (spec['purchaser_xmlid'].split('.')[0],))
        if len(modules)>1:
            raise ValueError()
        purchaser['module_state'] = modules[0][0] if modules else None
    if {'module','name','model','res_id'} <= tables['ir_model_data'] and {'id','active','login','partner_id'} <= tables['res_users'] and {'id','name'} <= tables['res_partner']:
        module,name = spec['purchaser_xmlid'].split('.')
        users = rows(cursor, "SELECT u.id,u.active,u.login,p.name FROM ir_model_data d JOIN res_users u ON u.id=d.res_id "
            "JOIN res_partner p ON p.id=u.partner_id WHERE d.module=%s AND d.name=%s AND d.model='res.users' LIMIT 2", (module,name))
        if len(users)>1:
            raise ValueError()
        purchaser['status'] = 'present' if users else 'missing'
        if users:
            purchaser.update(zip(('user_id','active','login','name'), users[0]))
            if {'uid','gid'} <= tables['res_groups_users_rel']:
                groups = rows(cursor, "SELECT DISTINCT d.module||'.'||d.name FROM res_groups_users_rel g "
                    "JOIN ir_model_data d ON d.res_id=g.gid AND d.model='res.groups' WHERE g.uid=%s ORDER BY 1 LIMIT 101", (users[0][0],))
                if len(groups)>100:
                    raise ValueError()
                purchaser['group_xmlids'] = [row[0] for row in groups]
                purchaser['groups_without_xmlid'] = scalar(cursor, "SELECT count(DISTINCT g.gid) FROM res_groups_users_rel g WHERE g.uid=%s "
                    "AND NOT EXISTS (SELECT 1 FROM ir_model_data d WHERE d.res_id=g.gid AND d.model='res.groups')", (users[0][0],))
    return {'runtime':'verified','company_id':company,'facts':facts,'parts':witnesses,'purchaser':purchaser}
"""

CONTAINER_PROGRAM = (
    INVENTORY_LIBRARY
    + r"""
import configparser,json,os,sys
import psycopg2
spec=json.loads(sys.stdin.read(16385))
stage='runtime_inspection'
try:
    if os.environ.get('PLATFORM_INSTANCE')!='testing' or json.loads(os.environ[__IDENTITY_KEY__])!=spec['identity']:
        raise ValueError()
    stage='configuration'
    path=__CONFIG_PATH__
    with open(path,'rb') as stream:
        before=stream.read(262145)
    if len(before)>262144:
        raise ValueError()
    config=configparser.ConfigParser(interpolation=None)
    config.read_string(before.decode())
    options=config['options']
    database=os.environ.get('ODOO_DB_NAME')
    if not database or options.get('db_name')!=database or options.get('db_host')!='database':
        raise ValueError()
    stage='applied_parameters'
    if options.get('db_port','5432')!='5432':
        raise ValueError()
    connection=psycopg2.connect(host='database',hostaddr=spec['database_address'],port='5432',
        user=options['db_user'],password=options['db_password'],dbname=database,connect_timeout=3,
        options='-c default_transaction_read_only=on -c statement_timeout=3000')
    try:
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            result=read_inventory(cursor,spec)
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
    stage='drift'
    with open(path,'rb') as stream:
        if stream.read(262145)!=before:
            raise ValueError()
except BaseException:
    result={'runtime':'unknown','failure_stage':stage}
encoded=json.dumps(result)
print(encoded if len(encoded)<=65536 else json.dumps({'runtime':'unknown','failure_stage':'result_validation'}))
"""
)

HOST_PROGRAM = (
    HOST_INSPECT_LIBRARY
    + r"""
stage='host_inspection'
try:
    before=inspect()
    database=inspect_database_peer(before)
    stage='host_execution'
    child=subprocess.run(['docker','exec','-i',before['Id'],'timeout','--kill-after=2s','30s','python3','-I','-c',spec['container_program']],
        input=json.dumps({key:spec[key] for key in ('identity','company_id','part_ids','purchaser_xmlid')} | {'database_address':database['address']}),
        text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=35,check=False)
    stage='drift'
    after=inspect()
    if before!=after or database!=inspect_database_peer(after):
        raise ValueError()
    stage='result_validation'
    if child.returncode!=0 or len(child.stdout)>65536:
        raise ValueError()
    result=json.loads(child.stdout)
except Exception:
    result={'runtime':'unknown','failure_stage':stage}
print(prefix+json.dumps({'type':'complete','result':result}),flush=True)
"""
)
