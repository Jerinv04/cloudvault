# CloudVault — Exam & Viva Guide

Everything you need to present, demo and defend the project. Read sections 1–5 until you can explain them without notes. Then practise the demo (section 8) and the Q&A (section 9) aloud with Bernardo.

---

## 1. The 60-second pitch (memorise this)

> "Sharing files usually means choosing between tools that aren't secure enough for sensitive data and enterprise systems that are too heavy for a small team. **CloudVault** is a fully serverless file vault on AWS.
>
> Users log in through **Cognito**. The browser uploads directly to **S3** using a **pre-signed URL**, and every object is encrypted with a **KMS** key. The upload automatically triggers a **Lambda** that uses **Rekognition** and **Comprehend** to tag the file. If Comprehend finds personal data such as emails or phone numbers, the file is marked sensitive and its share links are limited to 15 minutes. That is our AI-powered access control.
>
> Files are shared only through **time-limited signed links**, never publicly. Metadata lives in **DynamoDB**. The frontend is served through **CloudFront**, and **CloudWatch** monitors everything. There are no servers to manage, it scales automatically, and the whole stack deploys from one template."

---

## 2. Architecture in one breath

**Browser (React on S3 + CloudFront) → Cognito (login, JWT) → API Gateway (validates JWT) → Lambda (4 functions) → S3 (files, KMS-encrypted) + DynamoDB (metadata) → Rekognition / Comprehend (AI tags) → CloudWatch (logs, metrics, alarms).**

### The four Lambda functions

| Function | Trigger | What it does |
|---|---|---|
| **uploadFile** | `POST /files/upload-url` | Validates type and size, writes a PENDING record in DynamoDB, returns a pre-signed POST (5 min, fixed key, fixed type, max 25 MB) |
| **listFiles** | `GET /files?tag=` | Queries DynamoDB by the caller's user id, optional tag search |
| **generateShareLink** | `POST /files/{id}/share` | Checks ownership, READY status and the AI rule, then returns a pre-signed GET URL (5/15/60 min) |
| **tagFile** | **S3 ObjectCreated event** (and `POST /files/{id}/tag`) | Rekognition labels images; Comprehend extracts key phrases and entities and detects PII; sets tags, `sensitive` and `READY` |

### Two flows to draw on the board

**Upload flow:**
```
Browser --(1) POST /files/upload-url + JWT--> API GW --> uploadFile --(2) put PENDING--> DynamoDB
        <--(3) pre-signed POST--------------------------
Browser --(4) file bytes--> S3 (encrypts with KMS) --(5) event--> tagFile --> Rekognition/Comprehend
                                                                  tagFile --(6) tags, READY--> DynamoDB
```

**Share flow:**
```
Browser --POST /files/{id}/share {expiresIn}--> API GW --> generateShareLink
   generateShareLink: GetItem(userId, fileId) -> owner?  READY?  sensitive && >15 min -> 403
   -> S3 pre-signed GET URL (expires) -> recipient downloads directly from S3
```

---

## 3. Every AWS service: what, why, and the alternative

Examiners love "why did you choose X instead of Y?" Know the right-hand column.

| Service | Role in CloudVault | Why this choice | Alternative & why not |
|---|---|---|---|
| **S3** | Stores the files | Very high durability (designed for 11 nines), unlimited capacity, native pre-signed URLs, versioning, events | EBS/EFS need servers attached; a DB is wrong for large binary files |
| **DynamoDB** | File metadata and tags | Serverless, millisecond latency, pay-per-request, key design gives per-user isolation | RDS needs a running instance and connection pooling from Lambda |
| **Lambda** | Backend logic | No servers to run, scales per request, pay per millisecond, native S3 triggers | EC2 needs patching and capacity planning and runs 24/7 |
| **API Gateway (REST)** | HTTPS front door | Built-in Cognito authorizer, throttling, CORS, metrics | Lambda Function URLs have no Cognito authorizer; the HTTP API needs a JWT authorizer set up (also valid) |
| **Cognito** | Sign-up, login, JWTs | Managed user directory, email verification, password policy, SRP, token refresh | Building our own auth is risky (password storage, token signing) |
| **CloudFront** | Serves the React app | HTTPS, global edge caching, keeps the bucket private through OAC | Public S3 website hosting is HTTP-only and needs a public bucket |
| **KMS** | Encryption keys | Managed keys held in HSMs, automatic rotation, every key use recorded in CloudTrail | SSE-S3 is simpler but gives less control and no key-level access policy |
| **Rekognition** | Image labels | Pre-trained deep-learning vision, one API call, no model training | Hosting our own CNN on SageMaker is costly and slow to build |
| **Comprehend** | Key phrases, entities, PII | Pre-trained NLP including PII detection | spaCy or a custom model in Lambda means big packages and cold starts |
| **CloudWatch** | Logs, metrics, dashboard, alarms | Automatic for Lambda and API Gateway; custom metrics via Embedded Metric Format | Third-party monitoring means extra cost and setup |

---

## 4. Security: "defence in depth"

Say the phrase **defence in depth**: many independent layers, so one failure is not a breach.

1. **Authentication.** Cognito verifies the email and enforces the password policy. Login uses **SRP (Secure Remote Password)**, so the password itself is never sent over the network.
2. **Token validation.** API Gateway's Cognito authorizer checks the JWT's signature (against the user pool's public keys), expiry, issuer and audience *before* any Lambda runs. An invalid token gets **401** and costs no compute.
3. **Authorization / no IDOR.** The user id (`sub`) comes from the **verified token**, never from the request body. It is the DynamoDB partition key and part of the S3 key (`users/{sub}/…`). User B asking for user A's fileId gets **404**. *IDOR = Insecure Direct Object Reference.*
4. **No public access.** S3 Block Public Access (all four settings) is on, ACLs are disabled (BucketOwnerEnforced), and a bucket policy denies any non-HTTPS request.
5. **Encryption at rest.** SSE-KMS uses a customer-managed key with rotation. **Envelope encryption:** KMS generates a data key, S3 encrypts the object with it and stores only the *encrypted* data key; the master key never leaves KMS. The **S3 Bucket Key** cuts the number of KMS calls, and therefore the cost. DynamoDB is encrypted too.
6. **Encryption in transit.** Everything uses HTTPS/TLS: CloudFront redirects HTTP, and the bucket policy denies plain HTTP.
7. **Controlled uploads.** The pre-signed POST *policy* is signed by the server and fixes the exact key, the content type and a **content-length range**. The browser cannot change any of these. There is also a type allow-list and file-name sanitisation, which blocks path traversal like `../`.
8. **Controlled sharing.** Only time-limited pre-signed GET URLs are issued; there are no public links. The AI rule applies here.
9. **Least-privilege IAM.** Each Lambda has its own role. For example, `listFiles` can only `dynamodb:Query`; it cannot touch S3 or KMS.
10. **Resilience.** S3 versioning recovers overwritten or deleted files, and DynamoDB point-in-time recovery does the same for metadata.
11. **Monitoring.** CloudWatch logs, custom metrics (uploads, tags, shares, sensitive files, blocked shares), a dashboard, and alarms on API 5XX errors and tagging errors.
12. **Abuse protection.** API throttling (20 req/s, burst 50) and a 5-minute upload window.

---

## 5. The AI part: explain it clearly (this is your USP)

**Auto-tagging** runs automatically, *event-driven*: an S3 `ObjectCreated` event triggers `tagFile`.

- **Images (JPEG/PNG)** go to `Rekognition.DetectLabels` (maximum 10 labels, confidence ≥ 80%). Example labels: `laptop`, `person`, `document`.
- **Text (txt/md/csv/json)** goes to Comprehend:
  - `DetectDominantLanguage` to pick the language,
  - `DetectKeyPhrases` and `DetectEntities` for the tags,
  - `DetectPiiEntities` (English) to find personal data.
- **Other types** (PDF, Office, zip) get a category tag only. This is a known limitation; see future work.

**AI-powered access control.** If PII (EMAIL, PHONE, ADDRESS, NAME, …) is detected with score ≥ 0.8, the file gets:
- `sensitive = true`,
- a red "sensitive" tag,
- a list of the PII types found.

`generateShareLink` then **refuses any link longer than 15 minutes (HTTP 403)** for that file and records a `ShareLinksBlocked` metric. So the AI decides the access policy automatically, not a human.

**Why this matters:** people often don't realise a file contains personal data. CloudVault catches it on upload and applies stricter sharing by default. This is the idea behind data-loss prevention (DLP).

**Resilience:** if the AI call fails (throttling, or the service is unavailable in the region), the file is still marked READY with a category tag and an `aiError` note. The file is never stuck, and you can re-run with the *Re-tag* button.

**Honest limitation to mention:** AI detection is probabilistic. It can miss PII (a false negative) or over-flag (a false positive). That's why the rule is *restrict*, not *delete*, and why re-tagging exists.

---

## 6. Design decisions examiners ask about

- **Why does the browser upload directly to S3 instead of through Lambda?**
  - API Gateway limits payloads to **10 MB** and synchronous Lambda invocations to **6 MB**.
  - Pushing bytes through Lambda costs compute time.
  - Direct upload is faster, cheaper, scales with S3, and file bytes never touch our code.
- **Why pre-signed *POST* rather than pre-signed PUT?**
  - A POST policy can enforce **content-length-range** (a size limit) server-side.
  - A PUT URL can't limit size.
- **Why is the share-link maximum 1 hour?**
  - A pre-signed URL is only valid while the credentials that signed it are valid.
  - Lambda signs with temporary role credentials, so very long expiries would silently break early.
  - One hour is reliable and matches "time-limited".
- **Why the DynamoDB key `userId` + `fileId`?**
  - Listing is a single `Query` on the partition, with no table scan.
  - Per-user isolation is built into the data model.
- **Why is `status` tracked (PENDING_UPLOAD → READY)?**
  - The record is created *before* the upload.
  - Sharing a half-uploaded or un-analysed file is blocked (409).
- **Why Embedded Metric Format?**
  - The Lambda prints structured JSON and CloudWatch turns it into metrics.
  - No extra API calls, no latency, no extra IAM permission.
- **Why Infrastructure as Code (SAM/CloudFormation)?**
  - It is reproducible: one command builds the entire stack and one deletes it.
  - It is version-controlled and reviewable, with no manual console drift.
- **Why is the frontend bucket private with OAC instead of S3 static-website hosting?**
  - Website endpoints require a public bucket and are HTTP-only.
  - With OAC only CloudFront can read the bucket, over HTTPS.
  - It is an improvement on the original plan; mention it proudly.

---

## 7. Scalability, availability, cost

- **Scalability.**
  - Lambda runs a new instance per concurrent request, up to the account's concurrency limit.
  - DynamoDB on-demand absorbs traffic spikes.
  - S3 and CloudFront are effectively unlimited.
  - There is nothing to resize.
- **Availability.**
  - Every service is managed and spans multiple Availability Zones within the region, so no single server can fail.
- **Cost model.** Pay-per-use.
  - When nobody uses the system, cost is close to zero; there are no idle EC2 instances.
  - The demo mostly falls within the Free Tier.
  - Rekognition, Comprehend and the KMS key bill per use or per month.
- **Operational overhead.** No OS patching, no capacity planning.

---

## 8. Testing and results (put this in your report)

**Automated: 14 unit tests** (`pytest`, AWS mocked with `moto`), all passing. They cover:
- a valid upload returns the POST policy and a PENDING record; `../` in the file name is stripped;
- validation: a disallowed type gives 415, an oversized file 413, a missing name 400;
- an unauthenticated request gets 401;
- an image is tagged by Rekognition; a text file with PII is flagged sensitive with its PII types;
- search by AI tag works;
- a share link is created (the signed URL contains `X-Amz-Signature`); the AI rule gives **403 for 1 h and 200 for 15 min** on sensitive files; an invalid expiry gives 400; the share count increments;
- **user isolation**: user B cannot list or share user A's file (404);
- a file cannot be shared before its upload completes (409);
- an AI-service failure still leaves the file READY;
- manual re-tag works; an invalid fileId (path injection) gives 400.

The template also passes `cfn-lint` with no errors.

**Manual end-to-end test plan** (run it after deploy and take screenshots for the report):

| # | Test | Expected |
|---|---|---|
| 1 | Sign up with a weak password | Rejected by the Cognito policy |
| 2 | Sign up, verify email, sign in | Dashboard loads |
| 3 | Upload `samples/project-notes.txt` | Tags such as "project notes", "serverless backend"; green "Encrypted · Ready" |
| 4 | Upload a photo (JPEG/PNG) | Rekognition labels appear |
| 5 | Upload `samples/team-contacts.txt` | Red "Sensitive · EMAIL, PHONE…"; the 1-hour option is blocked |
| 6 | Search a tag | Only matching files shown |
| 7 | Create a 5-min link, open it in incognito | Download works; after 5 min → `AccessDenied` / "Request has expired" |
| 8 | Open the raw S3 object URL without a signature | `AccessDenied` (no public access) |
| 9 | In S3 console → object → Properties | Encryption shows "SSE-KMS" |
| 10 | Call the API without a token (curl) | 401 Unauthorized |
| 11 | CloudWatch dashboard | Graphs of uploads, tags, shares, invocations and errors |

---

## 9. Live demo script (about 7 minutes, split between you two)

| Time | Who | Action & line |
|---|---|---|
| 0:00 | Bernardo | Pitch (section 1) + architecture slide |
| 1:30 | Jerin | Open the CloudFront URL → sign up → email code → sign in. *"Cognito handles identity; the password never leaves the browser thanks to SRP."* |
| 2:30 | Jerin | Upload a photo + `project-notes.txt`. *"The browser uploads straight to S3 with a pre-signed POST; the upload event triggers tagFile."* Show the tags. |
| 3:30 | Bernardo | Upload `team-contacts.txt` → the red sensitive badge. Try to share for 1 h → blocked; share for 5 min → works. *"This is our AI-powered access control."* |
| 4:30 | Bernardo | Open the link in incognito → downloads. Open the plain S3 URL → AccessDenied. |
| 5:15 | Jerin | S3 console: object encryption = SSE-KMS, versioning on, Block Public Access on. |
| 6:00 | Jerin | CloudWatch dashboard + the `tagFile` log showing the detected tags. |
| 6:30 | Both | Limitations & future work → questions. |

**Backup plan:** if Wi-Fi or AWS fails, show the screenshots in `docs/` and run `pytest -v` live. It proves the logic works offline.

---

## 10. Viva questions with model answers

**Basics**

1. **What is serverless?** You deploy code and configuration; the cloud provider runs, scales and patches the servers. You pay per use and there's no idle cost. Lambda, DynamoDB on-demand, S3, API Gateway and Cognito are all serverless.
2. **What is a pre-signed URL?** A URL containing a signature made with AWS credentials. Anyone holding it can perform *that one operation* on *that one object* until it expires, without having their own AWS credentials.
3. **Difference between authentication and authorization?** Authentication is *who you are* (Cognito login, JWT). Authorization is *what you may do* (API Gateway checks the token; Lambda allows access only to your own files; IAM limits what each Lambda can do).
4. **What is a JWT?** A JSON Web Token: header.payload.signature, Base64URL-encoded and signed by Cognito. It contains claims such as `sub`, `email` and `exp`. API Gateway verifies the signature with the user pool's public keys (JWKS).
5. **ID token vs access token?** The ID token carries user identity claims (email, sub) and is what a Cognito authorizer checks by default. The access token carries OAuth scopes. Tokens last 1 hour by default; the refresh token renews them silently.

**Architecture & AWS**

6. **What happens when I click Upload?** Walk through the upload flow in section 2.
7. **Why not store files in DynamoDB?** The item limit is 400 KB and storage is expensive; S3 is built for objects. DynamoDB stores the *metadata* and a pointer (`s3Key`).
8. **What is an S3 event notification?** S3 can invoke Lambda, SQS, SNS or EventBridge on events such as ObjectCreated. We use it to trigger `tagFile`, which makes the system event-driven and asynchronous.
9. **What is a cold start?** The first invocation after idle time needs a new execution environment, which adds latency. We keep functions small, use Python and avoid heavy dependencies.
10. **How does CloudFront OAC work?** CloudFront signs its requests to S3 with SigV4. The bucket policy allows only `cloudfront.amazonaws.com` with our distribution's ARN, so the bucket stays private.
11. **What is CORS and why do we need it?** The browser blocks cross-origin requests unless the server allows them. Our frontend (CloudFront domain) calls API Gateway and S3 (different domains), so both return CORS headers. After deploy we can restrict the allowed origin to our CloudFront URL.
12. **REST API vs HTTP API?** An HTTP API is cheaper and simpler. A REST API has more features: native Cognito authorizer, usage plans, per-method throttling and request validation. We chose REST for the built-in Cognito authorizer and throttling.
13. **What is IaC and what does `template.yaml` do?** Infrastructure as Code. The SAM template declares all resources (buckets, table, functions, roles, API, user pool, KMS key, CloudFront, dashboard, alarms). CloudFormation creates them in dependency order and can roll back on failure.

**Security**

14. **How is data encrypted?** At rest: S3 SSE-KMS with our customer-managed key (envelope encryption), and DynamoDB encrypted. In transit: TLS everywhere.
15. **What is envelope encryption?** Data is encrypted with a data key; the data key is encrypted with the KMS master key and stored alongside. The master key never leaves KMS.
16. **SSE-S3 vs SSE-KMS?** SSE-S3 uses AWS-owned keys with no control. SSE-KMS uses a key *you* control: key policy, rotation, CloudTrail audit of every use, and it can be disabled to cut off access.
17. **Can user B access user A's files?** No. The user id comes from the verified JWT, DynamoDB is queried by that partition key, and S3 keys are prefixed by it. A foreign fileId returns 404. The unit test `test_users_are_isolated` proves it.
18. **What stops someone uploading a 5 GB file or an .exe?** The server checks the type allow-list and size before signing. The signed POST policy enforces the exact content type and a content-length-range of 25 MB, and S3 itself rejects anything that violates the policy.
19. **What if a share link leaks?** It works only until it expires (at most 1 h, or 15 min for sensitive files) and only for that single file. Future work: link revocation.
20. **What is least privilege? Give an example.** Grant only the permissions needed. `listFiles` has only `dynamodb:Query` on one table; `uploadFile` can `PutObject` only under `users/*`, plus `kms:GenerateDataKey`.
21. **Why does uploadFile need `kms:GenerateDataKey`?** The pre-signed POST is signed with the Lambda role's credentials. S3 acts with those permissions, and writing an SSE-KMS object requires generating a data key. Likewise, the share function needs `kms:Decrypt` for downloads.
22. **Why versioning?** It recovers from accidental overwrite or deletion, and ransomware-style overwrites. Old versions expire after 30 days to control cost.

**AI**

23. **How does auto-tagging work?** See section 5. Rekognition handles images; Comprehend provides key phrases, entities and PII for text.
24. **What is PII? Give examples.** Personally Identifiable Information: email, phone, address, name, bank or card numbers, Aadhaar-style IDs.
25. **What if the AI is wrong?** Detection is probabilistic, with a confidence threshold (0.8 for PII, 80% for labels). A false positive only *restricts* sharing time, which is a safe failure. Users can re-tag. Future work: a manual override with an audit log.
26. **Why managed AI instead of your own model?** No training data or GPU is needed; the models are pre-trained and scale automatically; there's no model hosting. The trade-off is less customisation and per-request cost.
27. **What is the confidence score?** The model's probability estimate for a prediction. We filter out low-confidence results to reduce noise.

**Operations**

28. **How do you monitor the system?** CloudWatch Logs for every Lambda; custom metrics (UploadUrlsIssued, FilesUploaded, FilesTagged, ShareLinksCreated, SensitiveFilesDetected, ShareLinksBlocked); a dashboard; alarms on API 5XX and tagFile errors.
29. **How does it scale to 10,000 users?** Nothing changes: Lambda adds concurrent instances, DynamoDB on-demand scales, S3 and CloudFront scale automatically. We would raise the throttle limits and the Lambda concurrency quota if needed.
30. **What does it cost?** Pay-per-request; near zero when idle. The demo is mostly Free Tier; Rekognition and Comprehend are charged per call; the KMS key has a small monthly fee.
31. **How do you deploy and delete it?** `./scripts/deploy.sh` (SAM build and deploy, then build and upload the frontend) and `./scripts/teardown.sh`.
32. **How did you test it?** 14 automated unit tests with mocked AWS, `cfn-lint` on the template, and the manual end-to-end test plan (section 8).

**Critical thinking**

33. **Biggest limitation?**
    - AI tagging covers images and plain text only; PDF and Office files need Amazon Textract.
    - Share links can't be revoked before they expire.
    - There is no per-user storage quota.
34. **What would you add next?**
    - Textract for PDFs.
    - Link revocation (a share table and a proxy Lambda, or CloudFront signed URLs).
    - Sharing with other registered users.
    - File deletion from the UI.
    - Virus scanning on upload.
    - AWS WAF on API Gateway and CloudFront.
    - Multi-factor authentication in Cognito.
    - A CI/CD pipeline.
35. **What was the hardest part?**
    - Getting pre-signed URLs to work with SSE-KMS (they need SigV4 and the right KMS permissions on the signing role).
    - Avoiding a CloudFormation circular dependency between the S3 bucket's event trigger and the Lambda's IAM policy. We solved it by building the bucket ARN with `!Sub` instead of `!Ref`.

---

## 11. If you need a written report

Suggested chapters:
1. **Introduction:** problem statement, objectives (from slide 2).
2. **Literature / background:** cloud service models, serverless, AWS services used.
3. **System design:** architecture diagram (README), data flow (slide 6), DynamoDB schema, API table.
4. **Implementation:** each Lambda (short code snippets), the IaC template, the frontend.
5. **Security design:** the table in section 4.
6. **AI integration:** section 5.
7. **Testing & results:** the unit test list, manual test table, and screenshots (dashboard, sensitive file, S3 encryption setting, CloudWatch).
8. **Cost & scalability:** section 7.
9. **Limitations & future scope:** questions 33 and 34.
10. **Conclusion** and **references:** AWS documentation for each service.

---

## 12. Final checklist before the exam

- [ ] Deployed at least once and tested every row of the manual test plan
- [ ] Screenshots saved (login, dashboard with tags, sensitive block, S3 encryption properties, CloudWatch dashboard)
- [ ] Both of you can draw the upload flow and the share flow from memory
- [ ] You can open `template.yaml` and point to: the KMS key, bucket encryption, Block Public Access, the Cognito authorizer, one IAM policy
- [ ] You can open `tag_file.py` and show where `sensitive` is set; open `generate_share_link.py` and show the 403 rule
- [ ] `pytest -v` runs on your laptop (the offline backup demo)
- [ ] Created a fresh test account and uploaded the sample files before presenting
- [ ] After evaluation: `./scripts/teardown.sh`
