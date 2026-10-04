"""Ingestion adapters: map external data sources into ChargeWise's domain models.

Parsing is kept pure (string/JSON in, models out) so it can be unit-tested
without network access. Network fetch helpers are thin wrappers around httpx.
"""
