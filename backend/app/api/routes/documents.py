"""Document Inbox endpoint -- lists every file the system has ever
received (manual or WhatsApp), regardless of whether it was successfully
processed. Thin wrapper over `backend.app.documents.service`."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from backend.app.auth.dependencies import get_current_user
from backend.app.database.session import get_db
from backend.app.documents import service as documents_service
from backend.app.documents.models import DocumentSource, IncomingDocumentStatus, IncomingDocumentType
from backend.app.schemas.documents import IncomingDocumentOut

router = APIRouter(
    prefix="/api/documents", tags=["documents"], dependencies=[Depends(get_current_user)]
)


@router.get("", response_model=list[IncomingDocumentOut])
def list_documents(
    source: DocumentSource | None = None,
    document_type: IncomingDocumentType | None = None,
    status: IncomingDocumentStatus | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[IncomingDocumentOut]:
    documents = documents_service.list_incoming_documents(
        db, source=source, document_type=document_type, status=status, limit=limit
    )
    return [
        IncomingDocumentOut(
            id=document.id,
            source=document.source.value,
            document_type=document.document_type.value,
            filename=document.filename,
            sender=document.sender,
            status=document.status.value,
            received_at=document.received_at,
            processed_at=document.processed_at,
            error_message=document.error_message,
        )
        for document in documents
    ]
