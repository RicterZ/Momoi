from ..integrations.errors import IntegrationError


class ProviderError(IntegrationError):
    pass


class ProviderResponseError(ProviderError):
    """The endpoint returned a successful but unusable response."""
