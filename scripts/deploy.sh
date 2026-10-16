#!/usr/bin/env bash
# One-command deploy: backend (SAM) -> writes frontend/.env -> builds React -> uploads to S3 -> CloudFront.
# Usage:  ./scripts/deploy.sh                 (defaults: stack "cloudvault", region us-east-1)
#         STACK=cloudvault AWS_REGION=ap-south-1 ./scripts/deploy.sh
#         ALLOWED_ORIGIN=https://dxxxx.cloudfront.net ./scripts/deploy.sh   (lock CORS after first deploy)
set -euo pipefail
STACK=${STACK:-cloudvault}
REGION=${AWS_REGION:-us-east-1}
cd "$(dirname "$0")/.."

echo "==> 1/4 Building and deploying backend stack '$STACK' in $REGION"
(
  cd backend
  sam build
  sam deploy --stack-name "$STACK" --region "$REGION" --capabilities CAPABILITY_IAM \
    --resolve-s3 --no-confirm-changeset --no-fail-on-empty-changeset \
    ${ALLOWED_ORIGIN:+--parameter-overrides AllowedOrigin=$ALLOWED_ORIGIN}
)

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

echo "==> 2/4 Writing frontend/.env from stack outputs"
cat > frontend/.env <<ENV
VITE_API_URL=$(out ApiUrl)
VITE_USER_POOL_ID=$(out UserPoolId)
VITE_USER_POOL_CLIENT_ID=$(out UserPoolClientId)
ENV

echo "==> 3/4 Building React frontend"
(cd frontend && npm ci && npm run build)

echo "==> 4/4 Uploading to S3 and invalidating CloudFront"
aws s3 sync frontend/dist "s3://$(out FrontendBucketName)" --delete --region "$REGION"
aws cloudfront create-invalidation --distribution-id "$(out DistributionId)" --paths "/*" >/dev/null

echo
echo "CloudVault is live:  $(out CloudFrontUrl)   (CloudFront can take a few minutes on first deploy)"
echo "CloudWatch dashboard: $(out DashboardUrl)"
