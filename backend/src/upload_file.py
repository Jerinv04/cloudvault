"""uploadFile - POST /files/upload-url

Registers a new file in DynamoDB and returns a pre-signed S3 POST. The browser
uploads straight to S3, so file bytes never pass through Lambda or API Gateway.
The POST policy pins the exact object key, content type and maximum size.
"""
import uuid

from common import (BUCKET_NAME, MAX_UPLOAD_BYTES, ApiError, api_handler, emit_metric,
                    now_iso, parse_body, s3, safe_file_name, table, user_id)

UPLOAD_URL_TTL = 300  # seconds the upload form stays valid

ALLOWED_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "application/pdf", "text/plain", "text/markdown", "text/csv", "application/json",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@api_handler
def handler(event, context):
    uid = user_id(event)
    body = parse_body(event)

    name = safe_file_name(body.get("fileName"))
    content_type = str(body.get("contentType") or "").lower()
    try:
        size = int(body.get("size", 0))
    except (TypeError, ValueError):
        raise ApiError(400, "size must be a number")

    if content_type not in ALLOWED_TYPES:
        raise ApiError(415, f"File type '{content_type or 'unknown'}' is not allowed")
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        raise ApiError(413, f"File must be between 1 byte and {MAX_UPLOAD_BYTES // (1024 * 1024)} MB")

    file_id = uuid.uuid4().hex
    key = f"users/{uid}/{file_id}/{name}"

    table().put_item(
        Item={
            "userId": uid,
            "fileId": file_id,
            "fileName": name,
            "s3Key": key,
            "contentType": content_type,
            "size": size,
            "status": "PENDING_UPLOAD",
            "tags": [],
            "sensitive": False,
            "shareCount": 0,
            "uploadedAt": now_iso(),
        },
        ConditionExpression="attribute_not_exists(fileId)",
    )

    upload = s3().generate_presigned_post(
        Bucket=BUCKET_NAME,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, MAX_UPLOAD_BYTES],
        ],
        ExpiresIn=UPLOAD_URL_TTL,
    )
    emit_metric("UploadUrlsIssued")
    return 201, {"fileId": file_id, "upload": upload, "expiresIn": UPLOAD_URL_TTL}
