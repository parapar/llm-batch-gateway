"""OpenAI-compatible /v1/files: upload batch input JSONL, read metadata,
download content (used for both the input file a student uploads and the
output/error files batch_ops writes once a batch finishes).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from batchsvc.batch_ops import get_owned_file_or_404, new_file_id
from batchsvc.blobs import read_blob, write_blob
from batchsvc.config import Settings
from batchsvc.deps import get_current_user, get_db, get_settings
from batchsvc.errors import InvalidRequestError
from batchsvc.models import FileObject, FilePurpose, User
from batchsvc.schemas import FileOut

router = APIRouter(tags=["files"])


def _file_out(f: FileObject) -> FileOut:
    return FileOut(
        id=f.id,
        bytes=f.bytes,
        created_at=int(f.created_at.timestamp()),
        filename=f.filename,
        purpose=f.purpose,
    )


@router.post("/v1/files", response_model=FileOut, status_code=201)
async def create_file(
    file: UploadFile = File(...),
    purpose: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileOut:
    if purpose != "batch":
        raise InvalidRequestError(
            "Only purpose='batch' uploads are accepted on this server.", param="purpose"
        )
    data = await file.read()
    if not data:
        raise InvalidRequestError("Uploaded file is empty.", param="file")

    file_id = new_file_id()
    path, sha256 = write_blob(settings.blob_dir, file_id, data)
    file_obj = FileObject(
        id=file_id,
        user_id=user.id,
        purpose=FilePurpose.BATCH_INPUT,
        filename=file.filename or "upload.jsonl",
        path=str(path),
        bytes=len(data),
        sha256=sha256,
    )
    db.add(file_obj)
    db.commit()
    db.refresh(file_obj)
    return _file_out(file_obj)


@router.get("/v1/files/{file_id}", response_model=FileOut)
def get_file(file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> FileOut:
    f = get_owned_file_or_404(db, user, file_id)
    return _file_out(f)


@router.get("/v1/files/{file_id}/content")
def get_file_content(
    file_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> Response:
    f = get_owned_file_or_404(db, user, file_id)
    data = read_blob(f.path)
    return Response(content=data, media_type="application/jsonl")
