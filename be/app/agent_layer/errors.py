class AgentLayerError(Exception):
    """Base error raised by the Topline agent layer."""


class NotFoundError(AgentLayerError):
    pass


class UnsafeActionError(AgentLayerError):
    pass


class ApprovalRequiredError(UnsafeActionError):
    pass


class AmbiguousOwnerCommandError(UnsafeActionError):
    pass


class InvalidModelOutputError(AgentLayerError):
    pass
