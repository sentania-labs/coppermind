#!/bin/bash
echo "Running make setup..."
make setup || exit 1
echo "Running make lint..."
make lint || exit 1
echo "Running make typecheck..."
make typecheck || exit 1
echo "Running uv run pytest -q..."
uv run pytest -q || exit 1
echo "Running make scan-deps..."
make scan-deps || exit 1
echo "Running make prose-check..."
make prose-check || exit 1
echo "Running make scan-secrets..."
make scan-secrets || exit 1
echo "Running uv run pytest -q services/admin/tests/test_fields_page.py services/store/tests/test_tag_aliases.py services/api/tests/test_schema_api.py..."
uv run pytest -q services/admin/tests/test_fields_page.py services/store/tests/test_tag_aliases.py services/api/tests/test_schema_api.py || exit 1
echo "ALL CHECKS PASSED!"
