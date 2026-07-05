"""pcomirror — a rate-safe local mirror of the Planning Center People API.

See DESIGN.md. The service stores every PCO resource verbatim as JSON with
generated projections, keeps it fresh via webhooks + background reconciliation,
respects the PCO rate limit, and serves a JSON:API drop-in with write-through.
"""
__version__ = "0.1.0"
