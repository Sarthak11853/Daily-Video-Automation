"""Exception hierarchy. Each stage raises a specific error so the pipeline can
decide between retrying, falling back to another provider, or emitting a
production package."""


class DVAError(Exception):
    """Base error."""


class ConfigError(DVAError):
    """Missing / invalid configuration or credentials."""


class QuotaExceeded(DVAError):
    """A self-imposed or provider quota is exhausted for today."""


class ProviderUnavailable(DVAError):
    """A provider (LLM, TTS, renderer, platform) is not installed / reachable."""


class TransientError(DVAError):
    """Network hiccup / 5xx / 429 — safe to retry."""


class VerificationFailed(DVAError):
    """Facts could not be verified against trusted sources."""


class ScriptInvalid(DVAError):
    """Generated script violates the schema or uses unverified facts."""


class RenderFailed(DVAError):
    """3D/2D rendering failed."""


class QCFailed(DVAError):
    """Quality check failed."""

    def __init__(self, message: str, report: dict | None = None):
        super().__init__(message)
        self.report = report or {}


class PublishError(DVAError):
    """Publishing failed."""


class DuplicateUpload(PublishError):
    """The video was already published (or an upload is in an unknown state)."""
