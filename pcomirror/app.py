"""Application assembly — wire the components together into one service."""
from __future__ import annotations

from . import divergence, pseudonym
from .config import Settings
from .db import Database
from .diagnostics import NullRecorder, Recorder
from .ingest import Ingestor
from .pcoclient import PcoClient, UrllibTransport
from .ratelimit import RateLimiter
from .serving import Application
from .webhooks import WebhookProcessor
from .writer import Writer


class Mirror:
    """Holds every wired component; the single object the CLI/scheduler drive."""

    def __init__(self, settings: Settings, transport=None, db: Database | None = None):
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.db.init_schema()
        self.limiter = RateLimiter(settings.rate_target_rps, settings.rate_util)
        self.transport = transport or UrllibTransport(settings.pco_ca_bundle or None)
        keep = getattr(settings, "diagnostic_keep", 0)
        self.diagnostics = Recorder(self.db, keep) if keep else NullRecorder()
        self.client = PcoClient(settings, self.limiter, self.transport, self.diagnostics)
        self.writer = Writer(self.db, settings.api_version)
        self.ingestor = Ingestor(self.db, self.client, self.writer)
        self.webhooks = WebhookProcessor(self.db, self.writer, self.ingestor)
        self.wsgi = Application(self.db, self.writer, self.ingestor,
                                self.client, self.webhooks, settings, self.diagnostics)
        # The checker serves through the real WSGI app rather than a
        # reimplementation of it: what is under test is what callers get.
        self.pseudonymiser = pseudonym.Pseudonymiser(pseudonym.secret_for(settings))
        self.divergence = divergence.ShadowChecker(
            self.db, self.client, settings, self.pseudonymiser,
            serve=self.wsgi.serve_json, recorder=self.diagnostics)
        self.wsgi.divergence = self.divergence

    def close(self):
        self.db.close()
