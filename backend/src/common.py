"""Shared helpers for the CloudVault Lambda functions."""
import json
import logging
import os
import re
import time
from decimal import Decimal
from functools import wraps

import boto3
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_NAME = os.environ.get("TABLE_NAME", "cloudvault-files")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "cloudvault-files")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024

# Set only by local_dev/run_local.py, to redirect every AWS client at a local
# moto server instead of real AWS. Never set in the deployed Lambda environment.
LOCAL_AWS_ENDPOINT = os.environ.get("LOCAL_AWS_ENDPOINT")

_clients = {}


def _local_creds_kwargs():
    """Explicit dummy credentials for local dev, so the mock backend never
    accidentally picks up a real AWS credential from this machine's
    environment, SSO cache or ~/.aws files via boto3's default chain."""
    if not LOCAL_AWS_ENDPOINT:
        return {}
    return {"aws_access_key_id": "test", "aws_secret_access_key": "test"}


def s3():
    """S3 client pinned to the regional endpoint with SigV4.

    SigV4 is mandatory for pre-signed URLs on SSE-KMS objects, and the regional
    endpoint avoids redirect/CORS problems with freshly created buckets.
    """
    if "s3" not in _clients:
        if LOCAL_AWS_ENDPOINT:
            _clients["s3"] = boto3.client(
                "s3",
                region_name=REGION,
                endpoint_url=LOCAL_AWS_ENDPOINT,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
                **_local_creds_kwargs(),
            )
        else:
            _clients["s3"] = boto3.client(
                "s3",
                region_name=REGION,
                endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            )
    return _clients["s3"]


def client(name):
    if name not in _clients:
        kwargs = {"region_name": REGION, **_local_creds_kwargs()}
        if LOCAL_AWS_ENDPOINT:
            kwargs["endpoint_url"] = LOCAL_AWS_ENDPOINT
        _clients[name] = boto3.client(name, **kwargs)
    return _clients[name]


def table():
    if "table" not in _clients:
        kwargs = {"region_name": REGION, **_local_creds_kwargs()}
        if LOCAL_AWS_ENDPOINT:
            kwargs["endpoint_url"] = LOCAL_AWS_ENDPOINT
        _clients["table"] = boto3.resource("dynamodb", **kwargs).Table(TABLE_NAME)
    return _clients["table"]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _plain(value):
    """Convert DynamoDB Decimals/sets into JSON-friendly values."""
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Not serialisable: {type(value)}")


def response(status, body):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(body, default=_plain),
    }


def user_id(event):
    """The Cognito user id (sub). API Gateway has already verified the JWT."""
    try:
        return event["requestContext"]["authorizer"]["claims"]["sub"]
    except (KeyError, TypeError):
        raise ApiError(401, "Not authenticated")


def parse_body(event):
    raw = event.get("body") or "{}"
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "Body must be valid JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "Body must be a JSON object")
    return body


def path_param(event, name):
    value = (event.get("pathParameters") or {}).get(name)
    if not value or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ApiError(400, f"Invalid {name}")
    return value


_SAFE = re.compile(r"[^A-Za-z0-9._ -]")


def safe_file_name(name):
    name = os.path.basename(str(name or "")).strip()
    name = _SAFE.sub("_", name)[:200]
    if not name or name in {".", ".."}:
        raise ApiError(400, "fileName is required")
    return name


def emit_metric(name, value=1):
    """Publish a custom CloudWatch metric using the Embedded Metric Format.

    Printing this JSON to the Lambda log is enough - CloudWatch extracts the
    metric automatically, so no extra API call or IAM permission is needed.
    """
    print(json.dumps({
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": "CloudVault",
                "Dimensions": [[]],
                "Metrics": [{"Name": name, "Unit": "Count"}],
            }],
        },
        name: value,
    }))


def api_handler(fn):
    """Wrap an API Gateway handler with uniform error handling."""
    @wraps(fn)
    def wrapper(event, context):
        try:
            status, body = fn(event, context)
            return response(status, body)
        except ApiError as err:
            logger.info("Client error %s: %s", err.status, err.message)
            return response(err.status, {"message": err.message})
        except Exception:  # never leak stack traces to the client
            logger.exception("Unhandled error")
            return response(500, {"message": "Internal server error"})
    return wrapper


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
