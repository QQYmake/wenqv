"""Public surface of the framework-independent agent package."""

from .context import ContextConfig, ContextManager, ContextPreparation, estimate_tokens
from .core import AgentConfig, AgentCore, build_default_registry
from .llm import (
    LLMConfig,
    LLMClientFactory,
    OpenAICompatClient,
    configure_clients,
    get_client,
)
from .memory import InMemoryConversationStore
from .models import (
    AgentEvent,
    ChatMessage,
    ImageAttachment,
    LLMResponse,
    LLMStreamChunk,
    ToolCall,
    ToolCallDelta,
)
from .ports import ConversationStore, LLMClient, LLMClientProvider
from .registry import (
    Tool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolOutput,
    ToolRegistry,
)
from .skills import (
    SkillDefinition,
    SkillError,
    SkillInjectionResult,
    SkillManager,
    SkillNotFoundError,
)

__all__ = [
    "AgentConfig",
    "AgentCore",
    "AgentEvent",
    "ChatMessage",
    "ContextConfig",
    "ContextManager",
    "ContextPreparation",
    "ConversationStore",
    "InMemoryConversationStore",
    "ImageAttachment",
    "LLMClient",
    "LLMClientFactory",
    "LLMClientProvider",
    "LLMConfig",
    "LLMResponse",
    "LLMStreamChunk",
    "OpenAICompatClient",
    "SkillDefinition",
    "SkillError",
    "SkillInjectionResult",
    "SkillManager",
    "SkillNotFoundError",
    "Tool",
    "ToolCall",
    "ToolCallDelta",
    "ToolExecutionContext",
    "ToolExecutionResult",
    "ToolOutput",
    "ToolRegistry",
    "build_default_registry",
    "configure_clients",
    "estimate_tokens",
    "get_client",
]
