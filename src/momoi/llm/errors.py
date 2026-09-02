class ProviderError(RuntimeError):
    pass


class ProviderResponseError(ProviderError):
    """The endpoint returned a successful but unusable response."""
