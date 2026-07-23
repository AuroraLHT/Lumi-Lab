"""Request/response models for the HTTP auth and settings routes.

The live data path is the contract-driven `/ws` bridge and defines its own frame
formats; these Pydantic models cover only the small REST surface beside it.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

# ----------------------------------------------------------------- auth


class UserResponse(BaseModel):
    id: int
    username: str
    full_name: str = ""
    role: str = "viewer"
    is_admin: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds")
    user: UserResponse


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    full_name: str = Field(default="", max_length=128)
    role: Literal["viewer", "operator", "admin"] = "viewer"


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


# ------------------------------------------------------------- settings


class UserSettings(BaseModel):
    """
    Opaque per-user UI state. The frontend owns the schema; the backend stores
    and returns it verbatim.
    """

    settings: dict[str, Any] = Field(default_factory=dict)
