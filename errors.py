"""Domain errors; preserve lower-level causes with raise ... from exc."""

class AgentError(Exception):
    """Expected errors that the application boundary can explain to users."""
    code = 'AGENT_ERROR'


class LLMError(AgentError):
    code = 'MODEL_ERROR'


class StorageError(AgentError):
    code = 'STORAGE_ERROR'


class ToolError(AgentError):
    code = 'TOOL_ERROR'


class RunCancelled(AgentError):
    code = 'CANCELLED'
