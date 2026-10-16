#!/usr/bin/env bash
# Deletes everything so you stop paying. Buckets must be emptied first
# (the files bucket is versioned, so every version is removed).
set -euo pipefail
STACK=${STACK:-cloudvault}
REGION=${AWS_REGION:-us-east-1}

out() {
  aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

for BUCKET in "$(out FilesBucketName)" "$(out FrontendBucketName)"; do
  echo "Emptying s3://$BUCKET (all versions)"
  python3 -c "import boto3,sys; boto3.resource('s3', region_name='$REGION').Bucket(sys.argv[1]).object_versions.delete()" "$BUCKET"
done

sam delete --stack-name "$STACK" --region "$REGION" --no-prompts
echo "Stack deleted. The KMS key is scheduled for deletion by AWS (default 30-day waiting period)."
