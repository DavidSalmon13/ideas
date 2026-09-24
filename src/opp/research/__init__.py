"""SearchProvider, SSRF-guarded Fetcher, extractor, cache.

Agents depend on the ``SearchProvider`` protocol (``opp.research.search``) and receive a
concrete provider by injection; ``build_search_provider`` picks it from settings.
"""
