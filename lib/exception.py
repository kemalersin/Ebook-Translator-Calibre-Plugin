class UnexpectedResult(Exception):
    pass


class RequestRejected(UnexpectedResult):
    """The service rejected the request itself, so retrying cannot help."""


class ConversionFailed(Exception):
    pass


class ConversionAbort(Exception):
    pass


class TranslationFailed(Exception):
    pass


class TranslationCanceled(Exception):
    pass


class BadApiKeyFormat(TranslationCanceled):
    pass


class NoAvailableApiKey(TranslationCanceled):
    pass


class UnsupportedModel(Exception):
    pass
