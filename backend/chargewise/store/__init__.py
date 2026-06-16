"""Persistence layer: ORM models, engine/session, and repositories.

The repository functions are the only place the rest of the app touches the
database, so swapping SQLite for Postgres later is a change confined here.
"""
