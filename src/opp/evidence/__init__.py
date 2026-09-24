"""Evidence Store: claims/sources, dedup, provenance, validation.

- ``opp.evidence.dedup`` — canonical source URLs and lead/opportunity fingerprints (pure).
- ``opp.evidence.validator`` — the anti-hallucination gate and assumption load (pure).
- ``opp.evidence.store`` — ``EvidenceStore``, which validates before anything is saved.

Import from the submodules: this package stays empty so the DB layer can use
``opp.evidence.dedup`` without importing the store (which depends on the DB layer).
"""
