"""listFiles - GET /files[?tag=...]

Returns only the caller's files: the DynamoDB partition key is the Cognito
user id, so one user can never query another user's items.
"""
from boto3.dynamodb.conditions import Key

from common import api_handler, table, user_id

PUBLIC_FIELDS = ("fileId", "fileName", "contentType", "size", "status", "tags",
                 "sensitive", "piiTypes", "shareCount", "uploadedAt", "taggedAt", "lastSharedAt")
MAX_ITEMS = 500


@api_handler
def handler(event, context):
    uid = user_id(event)
    query = (event.get("queryStringParameters") or {}).get("tag", "").strip().lower()

    items, start_key = [], None
    while len(items) < MAX_ITEMS:
        kwargs = {"KeyConditionExpression": Key("userId").eq(uid)}
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        page = table().query(**kwargs)
        items.extend(page.get("Items", []))
        start_key = page.get("LastEvaluatedKey")
        if not start_key:
            break

    if query:  # match AI tags or the file name
        items = [i for i in items
                 if any(query in t for t in i.get("tags", [])) or query in i.get("fileName", "").lower()]

    items.sort(key=lambda i: i.get("uploadedAt", ""), reverse=True)
    files = [{k: i[k] for k in PUBLIC_FIELDS if k in i} for i in items[:MAX_ITEMS]]
    return 200, {"files": files, "count": len(files)}
