-- =====================================================================
-- pcomirror — canonical-writer semantics regression test
--
-- Verifies the correctness properties that the adversarial design review
-- flagged (DESIGN.md §3, §10). Load schema.sql first, then run this file.
-- Each check RAISEs EXCEPTION on failure, so a clean run == all pass.
--
--   createdb pcomirror
--   psql -d pcomirror -f docs/schema.sql
--   psql -d pcomirror -v ON_ERROR_STOP=1 -f docs/schema_test.sql
-- =====================================================================
\set ON_ERROR_STOP on

CREATE OR REPLACE FUNCTION _mkperson(id text, fn text, ln text, uat text,
                                     created text DEFAULT '2020-01-01T00:00:00Z')
RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object('id',id,'type','Person',
    'attributes', jsonb_build_object('first_name',fn,'last_name',ln,'status','active',
                    'created_at',created,'updated_at',uat),
    'relationships', jsonb_build_object('primary_campus',
        jsonb_build_object('data', jsonb_build_object('type','PrimaryCampus','id','3'))))
$$;

CREATE OR REPLACE FUNCTION _mkfd(id text, ctype text, cid text, defid text, val text, uat text)
RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object('id',id,'type','FieldDatum',
    'attributes', jsonb_build_object('value',val,'created_at','2020-01-01T00:00:00Z','updated_at',uat),
    'relationships', jsonb_build_object(
        'customizable', jsonb_build_object('data', jsonb_build_object('type',ctype,'id',cid)),
        'field_definition', jsonb_build_object('data', jsonb_build_object('type','FieldDefinition','id',defid))))
$$;

CREATE OR REPLACE FUNCTION _mkhm(id text, person text, hh text)
RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object('id',id,'type','HouseholdMembership',
    'attributes', jsonb_build_object('household_role','adult'),
    'relationships', jsonb_build_object(
        'person',   jsonb_build_object('data', jsonb_build_object('type','Person','id',person)),
        'household', jsonb_build_object('data', jsonb_build_object('type','Household','id',hh))))
$$;

CREATE OR REPLACE FUNCTION _assert(cond boolean, label text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  IF cond THEN RAISE NOTICE 'PASS: %', label;
  ELSE RAISE EXCEPTION 'FAIL: %', label; END IF;
END $$;

DO $$
DECLARE r person%ROWTYPE; hm household_membership%ROWTYPE; syncd boolean;
BEGIN
  -- T1: insert + generated projections
  PERFORM mirror_upsert('person',0,'100', _mkperson('100','Ada','Lovelace','2026-01-01T00:00:00Z'),'backfill','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='100';
  PERFORM _assert(r.first_name='Ada' AND r.last_name='Lovelace' AND r.primary_campus_id='3'
                  AND r.pco_updated_at='2026-01-01T00:00:00Z', 'T1 generated projections');

  -- T2: older update is a data no-op, but last_synced_at still advances
  UPDATE person SET last_synced_at='2000-01-01' WHERE pco_id='100';
  PERFORM mirror_upsert('person',0,'100', _mkperson('100','STALE','STALE','2025-01-01T00:00:00Z'),'webhook','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='100';
  PERFORM _assert(r.last_name='Lovelace' AND r.last_synced_at > now()-interval '1 min',
                  'T2 older write: data no-op, last_synced_at advances');

  -- T3: newer update wins
  PERFORM mirror_upsert('person',0,'100', _mkperson('100','Ada','Byron','2026-02-01T00:00:00Z'),'webhook','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='100';
  PERFORM _assert(r.last_name='Byron', 'T3 newer write wins');

  -- T4: same-second correction (>= semantics)
  PERFORM mirror_upsert('person',0,'200', _mkperson('200','Grace','WRONG','2026-03-01T12:00:00Z'),'webhook','2026-06-04');
  PERFORM mirror_upsert('person',0,'200', _mkperson('200','Grace','Hopper','2026-03-01T12:00:00Z'),'webhook','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='200';
  PERFORM _assert(r.last_name='Hopper', 'T4 same-second correction overwrites');

  -- T5: sticky tombstone — reordered OLDER update must not resurrect
  PERFORM mirror_tombstone('person',0,'200','2026-03-02T00:00:00Z','destroyed');
  PERFORM mirror_upsert('person',0,'200', _mkperson('200','Grace','Reorder','2026-03-01T12:00:00Z'),'webhook','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='200';
  PERFORM _assert(r.deleted_at IS NOT NULL, 'T5 sticky tombstone survives reordered older update');

  -- T6: newer-than-tombstone update DOES resurrect a non-merge tombstone
  PERFORM mirror_upsert('person',0,'200', _mkperson('200','Grace','Revived','2026-03-03T00:00:00Z'),'webhook','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='200';
  PERFORM _assert(r.deleted_at IS NULL AND r.last_name='Revived', 'T6 newer update resurrects non-merge tombstone');

  -- T7: merge tombstone is terminal (even a newer update cannot revive it)
  PERFORM mirror_tombstone('person',0,'200','2026-03-04T00:00:00Z','merged','999');
  PERFORM mirror_upsert('person',0,'200', _mkperson('200','Grace','ShouldNotRevive','2026-03-05T00:00:00Z'),'webhook','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='200';
  PERFORM _assert(r.deleted_at IS NOT NULL AND r.merged_into_pco_id='999', 'T7 merge tombstone terminal under update');

  -- T8: authoritative confirm_live overrides a merge tombstone
  PERFORM mirror_confirm_live('person',0,'200', _mkperson('200','Grace','Authoritative','2026-03-06T00:00:00Z'),'reconcile','2026-06-04');
  SELECT * INTO r FROM person WHERE pco_id='200';
  PERFORM _assert(r.deleted_at IS NULL AND r.merged_into_pco_id IS NULL, 'T8 confirm_live overrides merge');

  -- T9: polymorphic field_datum owner
  PERFORM mirror_upsert('field_datum',0,'fd1', _mkfd('fd1','Person','100','def1','baptized','2026-01-01T00:00:00Z'),'backfill','2026-06-04');
  PERFORM mirror_upsert('field_datum',0,'fd2', _mkfd('fd2','Organization','1','def2','orgwide','2026-01-01T00:00:00Z'),'backfill','2026-06-04');
  PERFORM _assert((SELECT person_pco_id='100' FROM field_datum WHERE pco_id='fd1')
              AND (SELECT person_pco_id IS NULL FROM field_datum WHERE pco_id='fd2'),
                  'T9 polymorphic field_datum owner (Person vs Organization)');

  -- T10: person_custom_fields view
  INSERT INTO field_definition(org_id,pco_id,raw,source,api_version)
  VALUES (0,'def1', jsonb_build_object('id','def1','type','FieldDefinition',
      'attributes', jsonb_build_object('name','Baptized','slug','baptized','data_type','string')),'backfill','2026-06-04');
  UPDATE field_datum SET value_text='baptized' WHERE pco_id='fd1';
  PERFORM _assert((SELECT fields->>'baptized' = 'baptized' FROM person_custom_fields WHERE person_pco_id='100'),
                  'T10 person_custom_fields view resolves slug');

  -- T11: timestamp-less resource — destroyed is terminal under redelivered create
  PERFORM mirror_upsert_untimed('household_membership',0,'hm1', _mkhm('hm1','100','h1'),'webhook','2026-06-04');
  PERFORM mirror_tombstone('household_membership',0,'hm1',NULL,'destroyed');
  PERFORM mirror_upsert_untimed('household_membership',0,'hm1', _mkhm('hm1','100','h1'),'webhook','2026-06-04');
  SELECT * INTO hm FROM household_membership WHERE pco_id='hm1';
  PERFORM _assert(hm.deleted_at IS NOT NULL, 'T11 untimed tombstone terminal under at-least-once redelivery');

  RAISE NOTICE '--- all canonical-writer semantics verified ---';
END $$;

DROP FUNCTION _mkperson(text,text,text,text,text);
DROP FUNCTION _mkfd(text,text,text,text,text,text);
DROP FUNCTION _mkhm(text,text,text);
DROP FUNCTION _assert(boolean,text);
