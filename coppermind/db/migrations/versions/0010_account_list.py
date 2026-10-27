"""Notes carry a list of accounts, and declared date fields accept a date-and-time."""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_notes_context_account", table_name="notes")
    op.execute(
        "ALTER TABLE notes ALTER COLUMN account TYPE text[] "
        "USING CASE WHEN account IS NULL OR account = '' THEN ARRAY[]::text[] "
        "ELSE ARRAY[account] END"
    )
    op.execute("ALTER TABLE notes ALTER COLUMN account SET DEFAULT '{}'")
    op.execute("ALTER TABLE notes ALTER COLUMN account SET NOT NULL")
    op.create_index("ix_notes_account", "notes", ["account"], postgresql_using="gin")
    op.execute(r"""CREATE OR REPLACE FUNCTION
    coppermind_index_fields(gen text, identity text, fm jsonb)
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
        IF k = 'list' AND typ = 'string' THEN
          v := jsonb_build_array(v); typ := 'array';
        END IF;
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
            IF (v #>> '{}') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' THEN
              d := (v #>> '{}')::date;
            ELSIF (v #>> '{}') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?$'
              THEN
              d := (v #>> '{}')::timestamp::date;
            ELSE invalid := 'expected YYYY-MM-DD date or YYYY-MM-DDTHH:MM:SS';
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


def downgrade() -> None:
    op.drop_index("ix_notes_account", table_name="notes")
    op.execute("ALTER TABLE notes ALTER COLUMN account DROP NOT NULL")
    op.execute("ALTER TABLE notes ALTER COLUMN account DROP DEFAULT")
    op.execute("ALTER TABLE notes ALTER COLUMN account TYPE text USING account[1]")
    op.create_index("ix_notes_context_account", "notes", ["context", "account"])
    # The wider index function stays: it reads every note the old one did.
