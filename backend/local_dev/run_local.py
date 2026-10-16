"""Local mock backend for CloudVault.

Runs a moto server that emulates S3 / DynamoDB / Rekognition / Comprehend /
Cognito, bootstraps the same resources template.yaml would create in AWS,
and exposes the four Lambda handlers over plain HTTP so the real frontend
(pointed at this process instead of API Gateway) can be driven end to end
without any AWS account. Local dev only - never used by the deployed stack.
"""
import json
import os
import re
import sys
import uuid

os.environ.update({
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "TABLE_NAME": "cloudvault-files-local",
    "BUCKET_NAME": "cloudvault-files-local",
    "MAX_UPLOAD_MB": "25",
    "ALLOWED_ORIGIN": "http://localhost:5173",
    "LOCAL_AWS_ENDPOINT": "http://localhost:5099",
})

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

import boto3  # noqa: E402
import jwt  # noqa: E402
from flask import Flask, request, jsonify  # noqa: E402
from flask_cors import CORS  # noqa: E402
from moto.server import ThreadedMotoServer  # noqa: E402

MOTO_PORT = 5099
API_PORT = 5088
ENDPOINT = f"http://localhost:{MOTO_PORT}"
BUCKET = os.environ["BUCKET_NAME"]
TABLE = os.environ["TABLE_NAME"]
FRONTEND_ORIGIN = "http://localhost:5173"
DEMO_EMAIL = "demo@cloudvault.local"
DEMO_PASSWORD = "DemoPass123"


EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(\+?\d[\d\-\s()]{7,}\d)")


class LocalRekognition:
    """moto has no real image-recognition model; a small canned response is
    enough to exercise the tagging pipeline end to end."""

    def detect_labels(self, Image, MaxLabels, MinConfidence):
        return {"Labels": [{"Name": "Photo", "Confidence": 95.0}, {"Name": "Document", "Confidence": 85.0}]}


class LocalComprehend:
    """moto has no NLP model either; this does real (if simple) regex-based
    PII detection so the sensitive-file / share-link-restriction demo works."""

    def detect_dominant_language(self, Text):
        return {"Languages": [{"LanguageCode": "en", "Score": 0.99}]}

    def detect_key_phrases(self, Text, LanguageCode):
        words = sorted({w.strip(".,!?;:\"'()") for w in Text.split() if len(w) > 5}, key=len, reverse=True)
        return {"KeyPhrases": [{"Text": w, "Score": 0.9} for w in words[:6]]}

    def detect_entities(self, Text, LanguageCode):
        return {"Entities": []}

    def detect_pii_entities(self, Text, LanguageCode):
        entities = []
        if EMAIL_RE.search(Text):
            entities.append({"Type": "EMAIL", "Score": 0.99})
        if PHONE_RE.search(Text):
            entities.append({"Type": "PHONE", "Score": 0.95})
        return {"Entities": entities}


def aws(service):
    return boto3.client(service, region_name="us-east-1", endpoint_url=ENDPOINT,
                         aws_access_key_id="test", aws_secret_access_key="test")


def bootstrap():
    s3 = aws("s3")
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_cors(Bucket=BUCKET, CORSConfiguration={
        "CORSRules": [{
            "AllowedOrigins": [FRONTEND_ORIGIN],
            "AllowedMethods": ["GET", "POST", "PUT"],
            "AllowedHeaders": ["*"],
            "ExposeHeaders": ["ETag"],
        }]
    })

    ddb = aws("dynamodb")
    ddb.create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"},
                   {"AttributeName": "fileId", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"},
                              {"AttributeName": "fileId", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )

    idp = aws("cognito-idp")
    pool = idp.create_user_pool(
        PoolName="cloudvault-local",
        UsernameAttributes=["email"],
        AutoVerifiedAttributes=["email"],
        Policies={"PasswordPolicy": {
            "MinimumLength": 8, "RequireLowercase": True,
            "RequireUppercase": True, "RequireNumbers": True, "RequireSymbols": False,
        }},
    )["UserPool"]["Id"]
    client_id = idp.create_user_pool_client(
        UserPoolId=pool, ClientName="cloudvault-local-web", GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]["ClientId"]

    idp.admin_create_user(
        UserPoolId=pool, Username=DEMO_EMAIL, MessageAction="SUPPRESS",
        UserAttributes=[{"Name": "email", "Value": DEMO_EMAIL}, {"Name": "email_verified", "Value": "true"}],
    )
    idp.admin_set_user_password(UserPoolId=pool, Username=DEMO_EMAIL, Password=DEMO_PASSWORD, Permanent=True)

    return pool, client_id


def build_event(claims_sub, body_raw, query, path_params):
    return {
        "requestContext": {"authorizer": {"claims": {"sub": claims_sub}}} if claims_sub else {},
        "body": body_raw,
        "queryStringParameters": query or None,
        "pathParameters": path_params or None,
    }


def sub_from_request():
    token = request.headers.get("Authorization")
    if not token:
        return None
    try:
        return jwt.decode(token, options={"verify_signature": False}).get("sub")
    except jwt.PyJWTError:
        return None


def to_flask(lambda_response):
    return lambda_response["body"], lambda_response["statusCode"], {"Content-Type": "application/json"}


def sync_pending_tags(uid, table_res, s3_client, tag_file):
    """Stand-in for the S3 ObjectCreated trigger: tag any file whose bytes have
    landed in S3 but which is still PENDING_UPLOAD."""
    from boto3.dynamodb.conditions import Key
    page = table_res.query(KeyConditionExpression=Key("userId").eq(uid))
    for item in page.get("Items", []):
        if item.get("status") != "PENDING_UPLOAD":
            continue
        try:
            s3_client.head_object(Bucket=BUCKET, Key=item["s3Key"])
        except Exception:
            continue
        try:
            tag_file.process(uid, item["fileId"], item["s3Key"], from_upload=True)
        except Exception as e:
            print(f"[local_dev] tag sync failed for {item['fileId']}: {e}")


def make_app(pool_id, client_id):
    import common
    import generate_share_link
    import list_files
    import tag_file
    import upload_file

    # moto doesn't implement Rekognition/Comprehend detection calls - swap in
    # local stand-ins the same way the pytest suite does.
    common._clients["rekognition"] = LocalRekognition()
    common._clients["comprehend"] = LocalComprehend()

    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": FRONTEND_ORIGIN}}, allow_headers=["Content-Type", "Authorization"])

    @app.route("/config", methods=["GET"])
    def config():
        # Lets the frontend ask for the CURRENT Cognito pool/client id at
        # runtime instead of baking a possibly-stale one in at build time -
        # this backend generates a new pool every restart.
        return jsonify({
            "userPoolId": pool_id,
            "userPoolClientId": client_id,
            "cognitoEndpoint": ENDPOINT,
        })

    @app.route("/files/upload-url", methods=["POST"])
    def upload_url():
        event = build_event(sub_from_request(), request.get_data(as_text=True), None, None)
        body, status, headers = to_flask(upload_file.handler(event, None))
        return app.response_class(body, status=status, headers=headers)

    @app.route("/files", methods=["GET"])
    def files():
        uid = sub_from_request()
        if uid:
            sync_pending_tags(uid, common.table(), common.s3(), tag_file)
        event = build_event(uid, None, dict(request.args), None)
        body, status, headers = to_flask(list_files.handler(event, None))
        return app.response_class(body, status=status, headers=headers)

    @app.route("/files/<file_id>/share", methods=["POST"])
    def share(file_id):
        event = build_event(sub_from_request(), request.get_data(as_text=True), None, {"fileId": file_id})
        body, status, headers = to_flask(generate_share_link.handler(event, None))
        return app.response_class(body, status=status, headers=headers)

    @app.route("/files/<file_id>/tag", methods=["POST"])
    def tag(file_id):
        event = build_event(sub_from_request(), None, None, {"fileId": file_id})
        body, status, headers = to_flask(tag_file.handler(event, None))
        return app.response_class(body, status=status, headers=headers)

    return app


if __name__ == "__main__":
    server = ThreadedMotoServer(port=MOTO_PORT, verbose=False)
    server.start()
    print(f"[local_dev] moto AWS mock running on {ENDPOINT}")

    pool_id, client_id = bootstrap()
    print(f"[local_dev] Cognito user pool: {pool_id}")
    print(f"[local_dev] Cognito app client: {client_id}")
    print(f"[local_dev] Demo login -> email: {DEMO_EMAIL}  password: {DEMO_PASSWORD}")

    local_env = {
        "VITE_API_URL": f"http://localhost:{API_PORT}",
        "VITE_USER_POOL_ID": pool_id,
        "VITE_USER_POOL_CLIENT_ID": client_id,
        "VITE_COGNITO_ENDPOINT": ENDPOINT,
        "DEMO_EMAIL": DEMO_EMAIL,
        "DEMO_PASSWORD": DEMO_PASSWORD,
    }
    with open(os.path.join(os.path.dirname(__file__), "local_env.json"), "w") as f:
        json.dump(local_env, f, indent=2)

    # Write frontend/.env directly so `npm run dev` always picks up this run's
    # fresh Cognito pool/client id - no manual copy-paste step needed.
    frontend_env = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", ".env")
    with open(frontend_env, "w") as f:
        for key in ("VITE_API_URL", "VITE_USER_POOL_ID", "VITE_USER_POOL_CLIENT_ID", "VITE_COGNITO_ENDPOINT"):
            f.write(f"{key}={local_env[key]}\n")
    print(f"[local_dev] wrote {os.path.abspath(frontend_env)} - (re)start the frontend after this")

    app = make_app(pool_id, client_id)
    print(f"[local_dev] API shim listening on http://localhost:{API_PORT}")
    app.run(host="0.0.0.0", port=API_PORT)
