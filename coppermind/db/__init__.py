"""Database access for the derived and mirrored state.

Nothing in PostgreSQL is the only copy of anything. Notes, sources, settings,
schema, filing rules and key hashes all live on the `/data` volume; the tables
here are a mirror kept for listing, filtering, search and event delivery, and
one job rebuilds every row of them from `/data`. That is why a PostgreSQL
outage is a clean 503 on the endpoints that need it rather than a data loss.
"""
