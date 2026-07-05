"""Application assembly — wire the components together into one service."""
from __future__ import annotations

from .config import Settings
from .db import Database
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
        self.client = PcoClient(settings, self.limiter, self.transport)
        self.writer = Writer(self.db, settings.api_version)
        self.ingestor = Ingestor(self.db, self.client, self.writer)
        self.webhooks = WebhookProcessor(self.db, self.writer, self.ingestor)
        self.wsgi = Application(self.db, self.writer, self.ingestor,
                                self.client, self.webhooks, settings)

    def close(self):
        self.db.close()
