from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from company_brain.api.dependencies import Principal, get_tenant_scope, require_writer
from company_brain.db.session import get_session
from company_brain.domain.models import Canvas
from company_brain.domain.repositories import TenantScope

router = APIRouter(prefix="/api/v1/canvases", tags=["canvases"])
SessionDependency = Annotated[Session, Depends(get_session)]
ScopeDependency = Annotated[TenantScope, Depends(get_tenant_scope)]
WriterDependency = Annotated[Principal, Depends(require_writer)]

CanvasSide = Literal["top", "right", "bottom", "left"]
CanvasEnd = Literal["none", "arrow"]
CanvasType = Literal["text", "file", "link", "group"]
CanvasBackgroundStyle = Literal["cover", "ratio", "repeat"]
CanvasColor = Annotated[
    str,
    Field(pattern=r"^(?:[1-6]|#[0-9A-Fa-f]{3}|#[0-9A-Fa-f]{6}|#[0-9A-Fa-f]{8})$"),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CanvasNode(StrictModel):
    id: str = Field(min_length=1)
    type: CanvasType
    x: int
    y: int
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    color: CanvasColor | None = None
    text: str | None = None
    file: str | None = None
    subpath: str | None = None
    url: str | None = None
    label: str | None = None
    background: str | None = None
    backgroundStyle: CanvasBackgroundStyle | None = None

    @model_validator(mode="after")
    def validate_type_fields(self) -> "CanvasNode":
        required = {
            "text": self.text,
            "file": self.file,
            "link": self.url,
            "group": True,
        }
        if required[self.type] is None:
            raise ValueError(f"{self.type} node is missing its required field")
        allowed = {
            "text": {"text"},
            "file": {"file", "subpath"},
            "link": {"url"},
            "group": {"label", "background", "backgroundStyle"},
        }[self.type]
        optional_fields = {
            "text": self.text,
            "file": self.file,
            "subpath": self.subpath,
            "url": self.url,
            "label": self.label,
            "background": self.background,
            "backgroundStyle": self.backgroundStyle,
        }
        invalid = [
            name
            for name, value in optional_fields.items()
            if value is not None and name not in allowed
        ]
        if invalid:
            raise ValueError(f"Fields not valid for {self.type} node: {', '.join(invalid)}")
        if self.subpath is not None and not self.subpath.startswith("#"):
            raise ValueError("subpath must start with #")
        return self


class CanvasEdge(StrictModel):
    id: str = Field(min_length=1)
    fromNode: str = Field(min_length=1)
    fromSide: CanvasSide | None = None
    fromEnd: CanvasEnd | None = None
    toNode: str = Field(min_length=1)
    toSide: CanvasSide | None = None
    toEnd: CanvasEnd | None = None
    color: CanvasColor | None = None
    label: str | None = None


class JsonCanvas(StrictModel):
    nodes: list[CanvasNode] = Field(default_factory=list)
    edges: list[CanvasEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "JsonCanvas":
        node_ids = [node.id for node in self.nodes]
        edge_ids = [edge.id for edge in self.edges]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Canvas node IDs must be unique")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("Canvas edge IDs must be unique")
        known_nodes = set(node_ids)
        if any(
            edge.fromNode not in known_nodes or edge.toNode not in known_nodes
            for edge in self.edges
        ):
            raise ValueError("Canvas edges must reference existing nodes")
        return self


class CanvasImport(StrictModel):
    path: str = Field(min_length=1, max_length=2048)
    title: str = Field(min_length=1, max_length=500)
    canvas: JsonCanvas


class CanvasRead(StrictModel):
    id: UUID
    path: str
    title: str


def get_canvas_or_404(canvas_id: UUID, scope: TenantScope, session: Session) -> Canvas:
    canvas = session.scalar(
        select(Canvas).where(
            Canvas.id == canvas_id,
            Canvas.organization_id == scope.organization_id,
            Canvas.workspace_id == scope.workspace_id,
        )
    )
    if canvas is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canvas not found")
    return canvas


@router.post("/import", response_model=CanvasRead, status_code=status.HTTP_201_CREATED)
def import_canvas(
    payload: CanvasImport,
    session: SessionDependency,
    principal: WriterDependency,
) -> Canvas:
    canvas = Canvas(
        organization_id=principal.scope.organization_id,
        workspace_id=principal.scope.workspace_id,
        title=payload.title,
        path=payload.path,
        content=payload.canvas.model_dump(exclude_none=True),
    )
    session.add(canvas)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Canvas path already exists",
        ) from error
    session.refresh(canvas)
    return canvas


@router.get(
    "/{canvas_id}/export",
    response_model=JsonCanvas,
    response_model_exclude_none=True,
)
def export_canvas(
    canvas_id: UUID,
    session: SessionDependency,
    scope: ScopeDependency,
) -> JsonCanvas:
    canvas = get_canvas_or_404(canvas_id, scope, session)
    return JsonCanvas.model_validate(canvas.content)
