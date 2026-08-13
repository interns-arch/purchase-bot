"""File Inbox: every file the system has received, from every channel, with
the ability to DOWNLOAD the exact bytes a sender uploaded -- most importantly
a FAILED file, so the admin can open it and see what the vendor actually
sent without asking them to re-share it.

Files live on the app server's disk (`uploads/`); on Render's free tier that
disk is EPHEMERAL, so files received before the last deploy/restart may be
gone -- the listing marks each row's `file_available` honestly and the
download endpoint explains rather than 500s."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.documents import service as documents_service
from backend.app.documents.models import IncomingDocument, IncomingDocumentStatus

router = APIRouter(
    prefix="/api/documents",
    tags=["documents"],
    dependencies=[Depends(get_current_user)],
)


class DocumentRow(BaseModel):
    id: int
    filename: str
    source: str
    document_type: str
    status: str
    sender: str | None
    error_message: str | None
    received_at: str
    file_available: bool


def _to_row(document: IncomingDocument) -> DocumentRow:
    return DocumentRow(
        id=document.id,
        filename=document.filename,
        source=document.source.value,
        document_type=document.document_type.value,
        status=document.status.value,
        sender=document.sender,
        error_message=document.error_message,
        received_at=document.received_at.isoformat() if document.received_at else "",
        file_available=documents_service.resolve_stored_file(document) is not None,
    )


@router.get("", response_model=list[DocumentRow])
def list_documents(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[DocumentRow]:
    parsed_status: IncomingDocumentStatus | None = None
    if status_filter:
        try:
            parsed_status = IncomingDocumentStatus(status_filter)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown status {status_filter!r}.",
            ) from exc
    documents = documents_service.list_recent(db, status=parsed_status, limit=limit)
    return [_to_row(document) for document in documents]


@router.get("/{document_id}/download")
def download_document(document_id: int, db: Session = Depends(get_db)) -> FileResponse:
    document = db.get(IncomingDocument, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found.")
    file_path = documents_service.resolve_stored_file(document)
    if file_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "This file is no longer stored on the server (server restarts clear "
                "stored files). Ask the sender to re-send it, or check WhatsApp/Gmail "
                "for the original."
            ),
        )
    return FileResponse(path=file_path, filename=document.filename)
