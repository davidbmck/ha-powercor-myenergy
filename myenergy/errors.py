"""Safe client failures; messages never include upstream payloads or IDs."""


class MyEnergyError(Exception):
    """Base for actionable client failures."""


class AuthenticationError(MyEnergyError):
    """Credentials, API context or a submitted OTP were rejected."""


class AuthenticationExpired(AuthenticationError):
    """The portal requires a new login."""


class ChallengeRequired(AuthenticationError):
    """User interaction is required to finish authentication."""


class TransportError(MyEnergyError):
    """Connection, timeout, rate limit or server failure."""


class UnexpectedResponse(MyEnergyError):
    """The upstream response no longer matches the observed contract."""


class AccessDenied(MyEnergyError):
    """A protected action was denied; this alone does not prove expiry."""


class DataUnavailable(MyEnergyError):
    """The requested period has no available interval data."""


class MailboxError(MyEnergyError):
    """An email challenge could not be retrieved safely."""
