"""Hello module API schemas."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class HelloWorldResponse(BaseModel):
    """Frontend-compatible hello world response."""

    model_config = ConfigDict(extra="forbid")

    message: Annotated[str, Field(min_length=1, examples=["Hello, world!"])]
