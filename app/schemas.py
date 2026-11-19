"""The create-call contract: the JSON body POST /calls accepts.

Field names match the assignment's example exactly (camelCase), so a body
copied from the brief validates as-is.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Inactivity(BaseModel):
    afterSeconds: float = Field(8, gt=0, description="Silence before the agent checks in.")
    message: str = "Are you still there?"
    hangUpAfterSeconds: float = Field(20, gt=0, description="Silence before the agent hangs up.")


class HttpSpec(BaseModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    timeoutMs: int = Field(4000, gt=0, le=30000)
    headers: dict[str, str] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []},
        description="JSON Schema for the tool's arguments.",
    )
    http: HttpSpec


class CallConfig(BaseModel):
    systemPrompt: str
    voice: str = "female-en-in-1"
    language: str = "en-IN"
    firstSpeaker: Literal["agent", "user"] = "agent"
    greeting: str | None = None
    maxDurationSeconds: int = Field(600, gt=0, le=3600)
    inactivity: Inactivity = Field(default_factory=Inactivity)
    tools: list[ToolSpec] = Field(default_factory=list)
    knowledgeBase: str | None = None
    direction: Literal["outbound", "inbound"] = "outbound"
    to: str | None = Field(None, description="E.164 number to dial (outbound only).")
    webhookUrl: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "CallConfig":
        if self.direction == "outbound" and not self.to:
            raise ValueError("'to' is required for an outbound call")
        if self.firstSpeaker == "agent" and not self.greeting:
            raise ValueError("'greeting' is required when firstSpeaker is 'agent'")
        names = [t.name for t in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("tool names must be unique")
        return self
