import boto3

from esg_watchdog.config import settings


class S3Storage:
    def __init__(self) -> None:
        self.s3_client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
        self.bucket = settings.aws_s3_bucket

    def upload_pdf(self, key: str, data: bytes) -> str:
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType="application/pdf",
        )

        return key;
