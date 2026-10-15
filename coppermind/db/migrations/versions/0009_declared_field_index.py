"""Index declared metadata and maintain it transactionally with the mirror."""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE field_index_generations (
        id text PRIMARY KEY, schema_revision integer NOT NULL,
        definitions jsonb NOT NULL, active boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL)""")
    op.execute(
        "CREATE UNIQUE INDEX ix_field_generation_active "
        "ON field_index_generations(active) WHERE active"
    )
    op.execute("""CREATE TABLE note_field_values (
        generation text NOT NULL REFERENCES field_index_generations(id) ON DELETE CASCADE,
        note_id text NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
        field text NOT NULL, position integer NOT NULL,
        value jsonb, date_value date, invalid_reason text,
        PRIMARY KEY (generation, note_id, field, position))""")
    op.execute(
        "CREATE INDEX ix_note_fields_lookup ON note_field_values(generation, field, note_id)"
    )
    op.execute(
        "CREATE INDEX ix_note_fields_date ON note_field_values(generation, field, date_value)"
    )
    op.execute(
        "CREATE INDEX ix_note_fields_value ON note_field_values USING gin(value jsonb_path_ops)"
    )
    op.execute(r"""CREATE FUNCTION coppermind_index_fields(gen text, identity text, fm jsonb)
    RETURNS void LANGUAGE plpgsql AS $$
    DECLARE defs jsonb; def jsonb; v jsonb; k text; name text; typ text;
            invalid text; d date; member jsonb; pos integer;
    BEGIN
      SELECT definitions INTO defs FROM field_index_generations WHERE id = gen;
      DELETE FROM note_field_values WHERE generation = gen AND note_id = identity;
      FOR def IN SELECT jsonb_array_elements(defs) LOOP
        name := def->>'name'; k := def->>'kind'; v := fm->name;
        IF v IS NULL OR v = 'null'::jsonb THEN CONTINUE; END IF;
        typ := jsonb_typeof(v); invalid := NULL; d := NULL;
        IF k IN ('string', 'enum', 'date') AND typ <> 'string' THEN invalid := 'expected text';
        ELSIF k = 'bool' AND typ <> 'boolean' THEN invalid := 'expected boolean';
        ELSIF k = 'int' AND (typ <> 'number' OR v::text !~ '^-?[0-9]+$') THEN
          invalid := 'expected integer';
        ELSIF k = 'list' AND typ <> 'array' THEN invalid := 'expected list';
        END IF;
        IF invalid IS NULL AND k = 'enum' AND jsonb_array_length(def->'vocabulary') > 0
           AND NOT ((def->'vocabulary') @> jsonb_build_array(v)) THEN
          invalid := 'outside allowed vocabulary';
        END IF;
        IF invalid IS NULL AND k = 'date' THEN
          BEGIN
            IF (v #>> '{}') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
              invalid := 'expected YYYY-MM-DD date';
            ELSE d := (v #>> '{}')::date;
            END IF;
          EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN
            invalid := 'invalid date';
          END;
        END IF;
        IF invalid IS NOT NULL THEN
          INSERT INTO note_field_values VALUES(gen, identity, name, 0, NULL, NULL, invalid);
        ELSIF k = 'list' THEN
          pos := 0;
          FOR member IN SELECT jsonb_array_elements(v) LOOP
            INSERT INTO note_field_values VALUES(gen, identity, name, pos, member, NULL, NULL);
            pos := pos + 1;
          END LOOP;
        ELSE
          INSERT INTO note_field_values VALUES(gen, identity, name, 0, v, d, NULL);
        END IF;
      END LOOP;
    END $$""")
    op.execute("""CREATE FUNCTION coppermind_refresh_note_fields() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE gen text;
    BEGIN
      FOR gen IN SELECT id FROM field_index_generations WHERE active LOOP
        PERFORM coppermind_index_fields(gen, NEW.id, NEW.frontmatter);
      END LOOP;
      RETURN NEW;
    END $$""")
    op.execute("""CREATE TRIGGER coppermind_note_fields AFTER INSERT OR UPDATE OF frontmatter
        ON notes FOR EACH ROW EXECUTE FUNCTION coppermind_refresh_note_fields()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER coppermind_note_fields ON notes")
    op.execute("DROP FUNCTION coppermind_refresh_note_fields()")
    op.execute("DROP FUNCTION coppermind_index_fields(text, text, jsonb)")
    op.execute("DROP TABLE note_field_values")
    op.execute("DROP TABLE field_index_generations")
