"""Unit tests for the four CloudVault Lambdas, using moto to fake AWS.

Run:  pip install "moto[s3,dynamodb]" boto3 pytest && pytest -v
"""
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

os.environ.update({
    "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
    "TABLE_NAME": "files-test", "BUCKET_NAME": "cloudvault-files-test", "MAX_UPLOAD_MB": "1",
})
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import common  # noqa: E402
import generate_share_link  # noqa: E402
import list_files  # noqa: E402
import tag_file  # noqa: E402
import upload_file  # noqa: E402

ALICE, BOB = "alice-sub", "bob-sub"


class FakeComprehend:
    def __init__(self, pii):
        self.pii = pii

    def detect_dominant_language(self, Text):
        return {"Languages": [{"LanguageCode": "en", "Score": 0.99}]}

    def detect_key_phrases(self, Text, LanguageCode):
        return {"KeyPhrases": [{"Text": "Quarterly Budget", "Score": 0.99}, {"Text": "cloud costs", "Score": 0.9}]}

    def detect_entities(self, Text, LanguageCode):
        return {"Entities": [{"Text": "AWS", "Type": "ORGANIZATION", "Score": 0.98}]}

    def detect_pii_entities(self, Text, LanguageCode):
        return {"Entities": [{"Type": "EMAIL", "Score": 0.99}] if self.pii else []}


class FakeRekognition:
    def detect_labels(self, Image, MaxLabels, MinConfidence):
        return {"Labels": [{"Name": "Laptop", "Confidence": 97.0}, {"Name": "Electronics", "Confidence": 95.0}]}


@pytest.fixture
def aws():
    with mock_aws():
        common._clients.clear()
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="cloudvault-files-test")
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="files-test",
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"},
                       {"AttributeName": "fileId", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"},
                                  {"AttributeName": "fileId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield
        common._clients.clear()


def api_event(uid, body=None, path=None, query=None):
    return {"requestContext": {"authorizer": {"claims": {"sub": uid}}},
            "body": json.dumps(body) if body is not None else None,
            "pathParameters": path, "queryStringParameters": query}


def call(fn, event):
    res = fn(event, None)
    return res["statusCode"], json.loads(res["body"])


def upload(uid, name, ctype, data, pii=False):
    """Simulate the full flow: get upload URL -> put object -> S3 event -> tagFile."""
    status, body = call(upload_file.handler, api_event(uid, {"fileName": name, "contentType": ctype, "size": len(data)}))
    assert status == 201, body
    key = body["upload"]["fields"]["key"]
    common.s3().put_object(Bucket="cloudvault-files-test", Key=key, Body=data, ContentType=ctype)
    common._clients["comprehend"] = FakeComprehend(pii)
    common._clients["rekognition"] = FakeRekognition()
    tag_file.handler({"Records": [{"s3": {"object": {"key": key.replace(" ", "+")}}}]}, None)
    return body["fileId"]


def test_upload_returns_presigned_post_and_pending_record(aws):
    status, body = call(upload_file.handler, api_event(ALICE, {"fileName": "../notes.txt", "contentType": "text/plain", "size": 10}))
    assert status == 201
    assert body["upload"]["fields"]["key"] == f"users/{ALICE}/{body['fileId']}/notes.txt"  # path traversal stripped
    assert "policy" in body["upload"]["fields"]
    _, listing = call(list_files.handler, api_event(ALICE))
    assert listing["files"][0]["status"] == "PENDING_UPLOAD"


@pytest.mark.parametrize("payload,expected", [
    ({"fileName": "a.exe", "contentType": "application/x-msdownload", "size": 10}, 415),
    ({"fileName": "big.pdf", "contentType": "application/pdf", "size": 5 * 1024 * 1024}, 413),
    ({"contentType": "text/plain", "size": 10}, 400),
])
def test_upload_validation(aws, payload, expected):
    assert call(upload_file.handler, api_event(ALICE, payload))[0] == expected


def test_unauthenticated_request_rejected(aws):
    assert call(list_files.handler, {"body": None})[0] == 401


def test_image_is_tagged_by_rekognition(aws):
    fid = upload(ALICE, "desk photo.png", "image/png", b"\x89PNG fake")
    _, listing = call(list_files.handler, api_event(ALICE))
    f = next(x for x in listing["files"] if x["fileId"] == fid)
    assert f["status"] == "READY" and f["tags"] == ["image", "laptop", "electronics"] and f["sensitive"] is False


def test_text_with_pii_is_flagged_sensitive(aws):
    fid = upload(ALICE, "contacts.txt", "text/plain", b"Email me at jane@example.com", pii=True)
    _, listing = call(list_files.handler, api_event(ALICE, query={"tag": "sensitive"}))
    assert [f["fileId"] for f in listing["files"]] == [fid]
    assert listing["files"][0]["piiTypes"] == ["EMAIL"]


def test_search_by_ai_tag(aws):
    upload(ALICE, "budget.txt", "text/plain", b"Quarterly budget for cloud costs")
    upload(ALICE, "desk.png", "image/png", b"img")
    _, res = call(list_files.handler, api_event(ALICE, query={"tag": "budget"}))
    assert [f["fileName"] for f in res["files"]] == ["budget.txt"]


def test_share_link_and_ai_access_rule(aws):
    normal = upload(ALICE, "plan.txt", "text/plain", b"Project plan")
    secret = upload(ALICE, "ids.txt", "text/plain", b"jane@example.com", pii=True)

    status, body = call(generate_share_link.handler, api_event(ALICE, {"expiresIn": 3600}, {"fileId": normal}))
    assert status == 200 and "X-Amz-Signature" in body["url"] and body["expiresIn"] == 3600

    assert call(generate_share_link.handler, api_event(ALICE, {"expiresIn": 3600}, {"fileId": secret}))[0] == 403
    assert call(generate_share_link.handler, api_event(ALICE, {"expiresIn": 900}, {"fileId": secret}))[0] == 200
    assert call(generate_share_link.handler, api_event(ALICE, {"expiresIn": 999}, {"fileId": normal}))[0] == 400

    _, listing = call(list_files.handler, api_event(ALICE, query={"tag": "plan"}))
    assert listing["files"][0]["shareCount"] == 1


def test_users_are_isolated(aws):
    fid = upload(ALICE, "private.txt", "text/plain", b"Alice only")
    assert call(list_files.handler, api_event(BOB))[1]["files"] == []
    assert call(generate_share_link.handler, api_event(BOB, {"expiresIn": 300}, {"fileId": fid}))[0] == 404


def test_cannot_share_before_upload_completes(aws):
    _, body = call(upload_file.handler, api_event(ALICE, {"fileName": "x.pdf", "contentType": "application/pdf", "size": 10}))
    assert call(generate_share_link.handler, api_event(ALICE, {"expiresIn": 300}, {"fileId": body["fileId"]}))[0] == 409


def test_ai_failure_does_not_block_file(aws):
    class Broken:
        def detect_dominant_language(self, Text):
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "DetectDominantLanguage")
    _, body = call(upload_file.handler, api_event(ALICE, {"fileName": "n.txt", "contentType": "text/plain", "size": 4}))
    key = body["upload"]["fields"]["key"]
    common.s3().put_object(Bucket="cloudvault-files-test", Key=key, Body=b"text", ContentType="text/plain")
    common._clients["comprehend"] = Broken()
    tag_file.handler({"Records": [{"s3": {"object": {"key": key}}}]}, None)
    f = call(list_files.handler, api_event(ALICE))[1]["files"][0]
    assert f["status"] == "READY" and f["tags"] == ["text"]


def test_manual_retag_endpoint(aws):
    fid = upload(ALICE, "photo.png", "image/png", b"img")
    status, body = call(tag_file.handler, api_event(ALICE, path={"fileId": fid}))
    assert status == 200 and "laptop" in body["tags"]


def test_invalid_file_id_rejected(aws):
    assert call(generate_share_link.handler, api_event(ALICE, {"expiresIn": 300}, {"fileId": "../../etc"}))[0] == 400
