from ..integrations.errors import IntegrationError


class ProviderError(IntegrationError):
    pass


class ProviderResponseError(ProviderError):
    """The endpoint returned a successful but unusable response."""


class ProviderContentFiltered(ProviderError):
    """The provider's moderation blocked the request; retrying the same input cannot succeed."""
