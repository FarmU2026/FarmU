"""S3 adapter for the overlay writer. boto3 is imported lazily so the core never needs it.

    writer = S3Writer(os.environ["FW_BUCKET"])   # needs s3:PutObject on analysis/*
"""
from __future__ import annotations


class S3Writer:
    def __init__(self, bucket: str, client=None, region: str | None = None):
        self.bucket = bucket
        self._client = client
        self._region = region

    @property
    def client(self):
        if self._client is None:
            import boto3  # lazy: only Lambdas that write overlays need it

            self._client = boto3.client("s3", region_name=self._region)
        return self._client

    def put(self, key: str, data: bytes, content_type: str = "image/png") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
