from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from company_brain.domain.models import EntityType


class EntityCreate(BaseModel):
    entity_type: EntityType
    name: str = Field(min_length=1, max_length=500)
    aliases: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)


class EntityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=500)
    aliases: list[str] | None = None
    metadata: dict[str, object] | None = None

    @field_validator("name", "aliases", "metadata", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field cannot be null")
        return value


class EntityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    entity_type: EntityType
    name: str
    normalized_name: str
    aliases: list[str]
    lifecycle_status: str


class RelationshipCreate(BaseModel):
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str = Field(min_length=1, max_length=100)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class RelationshipUpdate(BaseModel):
    relationship_type: str | None = Field(default=None, min_length=1, max_length=100)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("relationship_type", "confidence", mode="before")
    @classmethod
    def reject_explicit_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("Field cannot be null")
        return value


class RelationshipRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: str
    confidence: float


class EvidenceCreate(BaseModel):
    source_type: str = Field(min_length=1, max_length=100)
    uri: str = Field(min_length=1, max_length=2048)
    evidence_type: str = Field(min_length=1, max_length=100)
    pointer: dict[str, object] = Field(default_factory=dict)
    quote: str | None = None


class EvidenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    workspace_id: UUID
    source_id: UUID
    evidence_type: str
    pointer: dict[str, object]
    quote: str | None
