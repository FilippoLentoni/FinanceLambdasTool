"""AWS client construction (real-deploy lesson L4).

Every client is regional and signs with SigV4. A default ``boto3.client("s3")`` presigns SigV2 URLs
on the global ``s3.amazonaws.com`` host, which S3 rejects for KMS-encrypted objects and in
SigV4-only regions (the platform's first beta run failed with HTTP 403). This package has no S3
need today; :func:`s3_client` exists so that any future use goes through the safe helper, and a
guard test (``tests/unit/test_aws_clients.py``) fails on any bare ``boto3.client("s3"...)`` call in
the source tree.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["region", "session", "ssm_client", "s3_client", "credentials"]

DEFAULT_REGION = "us-east-2"


def region(explicit: str | None = None) -> str:
    return explicit or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def session(region_name: str | None = None) -> Any:
    import boto3

    return boto3.session.Session(region_name=region(region_name))


def _config(*, total_max_attempts: int | None = None, **extra: Any) -> Any:
    from botocore.config import Config

    retries = {"max_attempts": 3, "mode": "standard"} if total_max_attempts is None else {"total_max_attempts": total_max_attempts, "mode": "standard"}
    return Config(signature_version="v4", retries=retries, connect_timeout=3, read_timeout=10, **extra)


def ssm_client(region_name: str | None = None, *, boto_session: Any = None, total_max_attempts: int | None = None) -> Any:
    """Regional SSM client (SigV4); callers may bound standard retries for release publication."""
    r = region(region_name)
    return (boto_session or session(r)).client("ssm", region_name=r, config=_config(total_max_attempts=total_max_attempts))


def s3_client(region_name: str | None = None, *, boto_session: Any = None) -> Any:
    """An S3 client that signs/presigns SigV4 on the regional virtual-hosted endpoint."""
    from botocore.config import Config

    r = region(region_name)
    return (boto_session or session(r)).client(
        "s3",
        region_name=r,
        endpoint_url=f"https://s3.{r}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def credentials(boto_session: Any = None) -> Any:
    """Frozen credentials of the Lambda role (for SigV4 signing of producer API calls)."""
    creds = (boto_session or session()).get_credentials()
    if creds is None:
        raise RuntimeError("no AWS credentials available")
    return creds.get_frozen_credentials()
