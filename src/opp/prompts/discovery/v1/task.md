Signal source: $signal_name
What to look for: $evidence_pattern

Scope of this run: $scope
Operator preferences (soft): $preferences

$memory

Search query used: $query

Snippets:
$snippets

Extract at most $max_leads leads that this evidence supports. For each lead give:
- title: short and specific
- business_shape: one of saas, service, marketplace, data, licensing, infoproduct,
  ecommerce, physical, rollup, arbitrage, infra, other
- named_customer, customer_evidence: the specific segment and the snippet numbers
- pain, pain_evidence: the specific pain and the snippet numbers
- why_now, why_now_evidence: the specific recent change and the snippet numbers, or an
  empty string and no numbers if the snippets don't show one
- wedge: a narrow first product a solo founder could start with
- confidence (high, medium, low) and confidence_reason: how well the snippets support it

Also give a one-line note on what this search showed, for your own later reference.
