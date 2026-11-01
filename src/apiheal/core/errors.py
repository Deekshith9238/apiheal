"""Errors with stable CLI exit codes."""


class ApihealError(Exception):
    exit_code = 5


class InputError(ApihealError):
    exit_code = 2


class PolicyError(ApihealError):
    exit_code = 3


class VerificationError(ApihealError):
    exit_code = 4


class PublicationError(ApihealError):
    """A verified local patch could not be safely published."""
