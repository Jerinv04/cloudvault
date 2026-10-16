"""generateShareLink - POST /files/{fileId}/share  body: {"expiresIn": 900}

Creates a temporary pre-signed GET URL. This is where the AI-powered access
control is enforced: files that Comprehend flagged as containing personal data
(PII) may only be shared with a short expiry.
"""
from datetime import datetime, timedelta, timezone

from common import (BUCKET_NAME, ApiError, api_handler, emit_metric, now_iso, parse_body,
                    path_param, s3, table, user_id)

EXPIRY_OPTIONS = (300, 900, 3600)   # 5 min, 15 min, 1 hour
SENSITIVE_MAX_EXPIRY = 900          # AI rule: PII files -> max 15 minutes


@api_handler
def handler(event, context):
    uid = user_id(event)
    file_id = path_param(event, "fileId")
    body = parse_body(event)
    try:
        expires_in = int(body.get("expiresIn", 900))
    except (TypeError, ValueError):
        raise ApiError(400, "expiresIn must be a number")
    if expires_in not in EXPIRY_OPTIONS:
        raise ApiError(400, f"expiresIn must be one of {list(EXPIRY_OPTIONS)} seconds")

    # Ownership check: the key includes the caller's user id, so another
    # user's fileId simply is not found.
    item = table().get_item(Key={"userId": uid, "fileId": file_id}).get("Item")
    if not item:
        raise ApiError(404, "File not found")
    if item.get("status") != "READY":
        raise ApiError(409, "File is still being uploaded or analysed")

    if item.get("sensitive") and expires_in > SENSITIVE_MAX_EXPIRY:
        emit_metric("ShareLinksBlocked")
        raise ApiError(403, "AI detected personal data in this file - share links are limited to 15 minutes")

    url = s3().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET_NAME,
            "Key": item["s3Key"],
            "ResponseContentDisposition": f'attachment; filename="{item["fileName"]}"',
        },
        ExpiresIn=expires_in,
    )

    table().update_item(
        Key={"userId": uid, "fileId": file_id},
        UpdateExpression="ADD shareCount :one SET lastSharedAt = :now",
        ExpressionAttributeValues={":one": 1, ":now": now_iso()},
    )
    emit_metric("ShareLinksCreated")

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return 200, {"url": url, "expiresIn": expires_in, "expiresAt": expires_at,
                 "sensitive": bool(item.get("sensitive"))}
