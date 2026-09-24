"""LLMGateway: tiers, structured output, retries, metering.

Agents call ``LLMGateway.structured`` (``opp.llm.gateway``) with a tier and a Pydantic
output type. Providers (``opp.llm.anthropic_provider``, ``opp.llm.fake``) implement the
neutral ``LLMProvider`` protocol in ``opp.llm.types``; ``build_gateway`` wires them up.
"""
