"""Record/replay cassettes for LLM, search and fetch calls (plan.md §20).

``Cassette`` (``opp.cassettes.cassette``) is the JSON file; ``CassetteLLMProvider``,
``CassetteSearchProvider`` and ``CassetteFetcher`` (``opp.cassettes.providers``) wrap the
real providers to record, or stand in for them to replay offline.
"""
