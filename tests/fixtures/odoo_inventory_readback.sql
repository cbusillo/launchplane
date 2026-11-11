CREATE TABLE res_company (id integer PRIMARY KEY);
CREATE TABLE external_system (id integer PRIMARY KEY, code text);
CREATE TABLE external_id (system_id integer, resource text, res_model text, res_id integer, external_id text, active boolean);
CREATE TABLE product_template (id integer PRIMARY KEY, name jsonb, active boolean, default_code text, list_price numeric, is_storable boolean, tracking text, fishbowl_extra_details text, fishbowl_upc text, fishbowl_packaging text);
CREATE TABLE product_product (id integer PRIMARY KEY, product_tmpl_id integer, barcode text, default_code text, standard_price jsonb);
CREATE TABLE product_supplierinfo (id integer PRIMARY KEY, company_id integer, product_tmpl_id integer, partner_id integer, price numeric, currency_id integer, uom_id integer, product_code text);
CREATE TABLE stock_location (id integer PRIMARY KEY, usage text);
CREATE TABLE stock_quant (product_id integer, location_id integer, company_id integer, quantity numeric);
CREATE TABLE stock_move (product_id integer, state text, company_id integer);
CREATE TABLE cm_product_import_conflict (product_id integer, source text, company_id integer);
CREATE TABLE res_partner (id integer PRIMARY KEY, name text);
CREATE TABLE res_users (id integer PRIMARY KEY, active boolean, login text, partner_id integer, password text);
CREATE TABLE ir_model_data (module text, name text, model text, res_id integer);
CREATE TABLE ir_module_module (name text, state text);
CREATE TABLE res_groups_users_rel (uid integer, gid integer);
CREATE TABLE ir_config_parameter (key text, value text);
INSERT INTO res_company VALUES (7),(8);
INSERT INTO external_system VALUES (1,'fishbowl'),(2,'unrelated');
INSERT INTO external_id VALUES (1,'part','product.template',101,'51',true),(1,'part','product.template',102,'52',false),(2,'part','product.template',999,'51',true);
INSERT INTO product_template VALUES
(101,'{"en_US":"Fictional hinge"}',true,'PART-51',0,true,'none','details','UPC-51','box'),
(102,'{"en_US":"Fictional bolt"}',false,'PART-52',0,true,'lot',NULL,NULL,NULL),
(999,'{"en_US":"Unrelated"}',true,'OTHER',100,true,'serial',NULL,NULL,NULL);
INSERT INTO product_product VALUES (201,101,'PART-51','PART-51','{"7":9.45,"8":999}'),(202,102,'PART-52','PART-52','{"7":2.00}'),(299,999,'OTHER','OTHER','{"7":100}');
INSERT INTO res_partner VALUES (11,'Example Supplier'),(19,'Fictional Purchaser');
INSERT INTO product_supplierinfo SELECT n,7,101,11,8.25,1,1,'V-51' FROM generate_series(1,12) n;
INSERT INTO product_supplierinfo VALUES (13,8,101,11,999,1,1,'FOREIGN'),(14,7,999,11,100,1,1,'UNBOUND');
INSERT INTO external_id SELECT 1,'vendorparts','product.supplierinfo',n,n::text,true FROM generate_series(1,13) n;
INSERT INTO stock_location VALUES (1,'internal'),(2,'supplier');
INSERT INTO stock_quant VALUES (201,1,7,2.50),(202,1,7,2.75),(201,1,8,100),(201,2,7,100),(299,1,7,100);
INSERT INTO stock_move VALUES (201,'done',7),(202,'draft',7),(201,'done',8),(299,'done',7);
INSERT INTO cm_product_import_conflict VALUES (101,'fishbowl',7),(999,'fishbowl',7),(101,'fishbowl',8),(101,'unrelated',7);
INSERT INTO res_users VALUES (19,true,'buyer@example.invalid',19,'private-fixture-password');
INSERT INTO ir_model_data VALUES ('cm_custom','user_gunneet','res.users',19),('base','group_user','res.groups',1),('purchase','group_purchase_user','res.groups',2);
INSERT INTO ir_module_module VALUES ('cm_custom','installed');
INSERT INTO res_groups_users_rel VALUES (19,1),(19,2),(19,3),(99,999);
INSERT INTO ir_config_parameter VALUES ('database.uuid','00000000-0000-4000-8000-000000000007'),('private.key','private-fixture-secret');
