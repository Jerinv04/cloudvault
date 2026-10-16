"""tagFile - AI auto-tagging.

Triggered two ways:
  1. Automatically by S3 (ObjectCreated) as soon as an upload finishes.
  2. Manually via POST /files/{fileId}/tag to re-run the analysis.

Images  -> Amazon Rekognition DetectLabels   (e.g. "laptop", "person", "document")
Text    -> Amazon Comprehend key phrases + entities, and PII detection.
If PII (emails, phone numbers, names, ...) is found the file is marked
sensitive=True, which generateShareLink uses to restrict sharing.
"""
import logging
from urllib.parse import unquote_plus

from botocore.exceptions import ClientError

from common import (BUCKET_NAME, ApiError, api_handler, client, emit_metric, now_iso,
                    path_param, s3, table, user_id)

logger = logging.getLogger()

REKOGNITION_BYTES_LIMIT = 5 * 1024 * 1024
TEXT_SAMPLE_BYTES = 4500          # stay inside Comprehend's synchronous limits
PII_MIN_SCORE = 0.80
IMAGE_TYPES = {"image/jpeg", "image/png"}          # formats Rekognition accepts
TEXT_TYPES = {"text/plain", "text/markdown", "text/csv", "application/json"}
COMPREHEND_LANGS = {"en", "es", "fr", "de", "it", "pt", "ar", "hi", "ja", "ko", "zh", "zh-TW"}


def category_tag(content_type):
    if content_type.startswith("image/"):
        return "image"
    if content_type == "application/pdf":
        return "pdf"
    if content_type in TEXT_TYPES:
        return "text"
    if content_type == "application/zip":
        return "archive"
    return "document"


def _clean(tag):
    tag = " ".join(str(tag).lower().split())
    return tag[:40]


def analyse_image(key, size):
    if size <= REKOGNITION_BYTES_LIMIT:
        image = {"Bytes": s3().get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()}
    else:
        image = {"S3Object": {"Bucket": BUCKET_NAME, "Name": key}}
    result = client("rekognition").detect_labels(Image=image, MaxLabels=10, MinConfidence=80)
    return [_clean(label["Name"]) for label in result.get("Labels", [])], False, []


def analyse_text(key):
    raw = s3().get_object(Bucket=BUCKET_NAME, Key=key, Range=f"bytes=0-{TEXT_SAMPLE_BYTES * 2}")["Body"].read()
    text = raw.decode("utf-8", errors="ignore").encode("utf-8")[:TEXT_SAMPLE_BYTES].decode("utf-8", errors="ignore")
    if not text.strip():
        return [], False, []

    comprehend = client("comprehend")
    langs = comprehend.detect_dominant_language(Text=text).get("Languages", [])
    lang = langs[0]["LanguageCode"] if langs else "en"
    if lang not in COMPREHEND_LANGS:
        lang = "en"

    phrases = comprehend.detect_key_phrases(Text=text, LanguageCode=lang).get("KeyPhrases", [])
    phrases = sorted(phrases, key=lambda p: p["Score"], reverse=True)[:6]
    entities = comprehend.detect_entities(Text=text, LanguageCode=lang).get("Entities", [])
    entities = sorted(entities, key=lambda e: e["Score"], reverse=True)[:4]
    tags = [_clean(p["Text"]) for p in phrases] + [_clean(e["Text"]) for e in entities]

    pii_types = []
    if lang == "en":  # PII detection is English-only
        pii = comprehend.detect_pii_entities(Text=text, LanguageCode="en").get("Entities", [])
        pii_types = sorted({e["Type"] for e in pii if e["Score"] >= PII_MIN_SCORE})
    return tags, bool(pii_types), pii_types


def process(uid, file_id, key, from_upload):
    head = s3().head_object(Bucket=BUCKET_NAME, Key=key)
    content_type = head.get("ContentType", "application/octet-stream").lower()
    size = head.get("ContentLength", 0)

    tags, sensitive, pii_types, ai_error = [], False, [], None
    try:
        if content_type in IMAGE_TYPES:
            tags, sensitive, pii_types = analyse_image(key, size)
        elif content_type in TEXT_TYPES:
            tags, sensitive, pii_types = analyse_text(key)
    except ClientError as err:  # AI failure must never leave a file stuck
        ai_error = err.response.get("Error", {}).get("Code", "AIError")
        logger.warning("AI analysis failed for %s: %s", key, ai_error)

    all_tags = list(dict.fromkeys([category_tag(content_type)] + [t for t in tags if t]))
    if sensitive:
        all_tags.append("sensitive")

    # "status", "size" and "sensitive" are DynamoDB reserved words -> use #aliases
    update = ("SET #st = :ready, #tg = :tags, #se = :sens, #pi = :pii, "
              "#sz = :size, #ta = :now")
    values = {":ready": "READY", ":tags": all_tags, ":sens": sensitive, ":pii": pii_types,
              ":size": size, ":now": now_iso()}
    names = {"#st": "status", "#tg": "tags", "#se": "sensitive", "#pi": "piiTypes",
             "#sz": "size", "#ta": "taggedAt"}
    if ai_error:
        names["#ae"] = "aiError"
        update += ", #ae = :err"
        values[":err"] = ai_error
    try:
        table().update_item(
            Key={"userId": uid, "fileId": file_id},
            UpdateExpression=update,
            ConditionExpression="attribute_exists(fileId)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning("No metadata record for %s - ignoring orphan object", key)
            return None
        raise

    if from_upload:
        emit_metric("FilesUploaded")
    emit_metric("FilesTagged")
    if sensitive:
        emit_metric("SensitiveFilesDetected")
    logger.info("Tagged %s: %s (sensitive=%s)", key, all_tags, sensitive)
    return {"fileId": file_id, "tags": all_tags, "sensitive": sensitive, "piiTypes": pii_types}


def parse_key(key):
    """users/{userId}/{fileId}/{fileName} -> (userId, fileId)"""
    parts = key.split("/", 3)
    if len(parts) != 4 or parts[0] != "users":
        return None, None
    return parts[1], parts[2]


@api_handler
def _api(event, context):
    uid = user_id(event)
    file_id = path_param(event, "fileId")
    item = table().get_item(Key={"userId": uid, "fileId": file_id}).get("Item")
    if not item:
        raise ApiError(404, "File not found")
    if item.get("status") == "PENDING_UPLOAD":
        raise ApiError(409, "File has not finished uploading")
    return 200, process(uid, file_id, item["s3Key"], from_upload=False)


def handler(event, context):
    if "Records" not in event:          # API Gateway re-tag request
        return _api(event, context)

    results = []
    for record in event["Records"]:      # S3 upload event(s)
        key = unquote_plus(record["s3"]["object"]["key"])
        uid, file_id = parse_key(key)
        if not uid:
            logger.warning("Skipping unexpected key %s", key)
            continue
        results.append(process(uid, file_id, key, from_upload=True))
    return {"processed": len(results)}
