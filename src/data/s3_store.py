"""
Writing to S3: forecasts, processed data and run artifacts.

`data_loader.py` reads *from* S3. This module writes *to* it, which is the half
the project was missing. The motivating case is the forecast archive: a forecast
that is not stored the day it is made cannot be reconstructed later, so the
accuracy record can only start once this exists.

Every method is a no-op returning None when S3 is not configured, so calling
code needs no `if ENV == "production"` branches.
"""

from __future__ import annotations

import io
import os
from typing import Any, Iterator

import boto3
import pandas as pd
from botocore.exceptions import ClientError

from src.utils.config_loader import load_config


def _prefix(config: dict[str, Any], name: str) -> str:
    """Reads a configured S3 prefix, guaranteeing a single trailing slash."""
    prefixes = config.get("paths", {}).get("s3_prefixes", {})
    value = prefixes.get(name, f"{name}/")
    return value.rstrip("/") + "/"


def forecast_key(config: dict[str, Any], run_date: pd.Timestamp | str) -> str:
    """
    Key for one day's forecast: `forecasts/dt=YYYY-MM-DD/forecast.parquet`.

    Date-partitioned rather than a flat `forecast_<date>.parquet` so the archive
    stays append-only and Athena or Glue can read `dt` as a partition column
    without any extra configuration.
    """
    date = pd.Timestamp(run_date).date()
    return f"{_prefix(config, 'forecasts')}dt={date}/forecast.parquet"


def artifact_key(config: dict[str, Any], run_id: str, filename: str) -> str:
    """Key for a training-run artifact, namespaced by MLflow run id."""
    return f"{_prefix(config, 'artifacts')}runs/{run_id}/{filename}"


class S3Store:
    """
    A thin, fail-soft wrapper over the handful of S3 calls this project needs.

    Fail-soft is deliberate for writes: losing a forecast archive entry is bad,
    but it must not take down a training run or a served request. Failures are
    reported and swallowed. Reads that the caller depends on raise instead.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_config()
        self.bucket = os.getenv("S3_BUCKET_NAME")
        self.env = os.getenv("ENV", "local").lower()
        self._client = None

    # ------------------------------------------------------------------ state

    @property
    def enabled(self) -> bool:
        """
        True only when S3 writes should actually happen.

        Gated on both `ENV` and a bucket being set, so a stray credential in the
        environment cannot cause a local run to start writing to the cloud.
        """
        return self.env == "production" and bool(self.bucket)

    @property
    def client(self) -> Any:
        """
        Lazily built S3 client.

        Credentials are deliberately NOT passed explicitly. boto3 resolves them
        from the environment locally and from the **IAM instance role** on EC2,
        which is what allows the static keys to be deleted in deployment.
        """
        if self._client is None:
            self._client = boto3.client(
                "s3", region_name=os.getenv("AWS_DEFAULT_REGION")
            )
        return self._client

    def _uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    # ----------------------------------------------------------------- writes

    def put_bytes(self, data: bytes, key: str) -> str | None:
        """Uploads raw bytes. Returns the s3:// URI, or None if disabled/failed."""
        if not self.enabled:
            return None
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        except (ClientError, Exception) as exc:
            print(f"[S3][WARN] put_object failed for {key}: {exc}")
            return None
        print(f"[S3] wrote {len(data):,} B -> {self._uri(key)}")
        return self._uri(key)

    def put_file(self, local_path: str, key: str) -> str | None:
        """Uploads a local file. Uses upload_file, which handles multipart."""
        if not self.enabled:
            return None
        try:
            self.client.upload_file(local_path, self.bucket, key)
        except (ClientError, Exception) as exc:
            print(f"[S3][WARN] upload_file failed for {key}: {exc}")
            return None
        print(f"[S3] wrote {os.path.basename(local_path)} -> {self._uri(key)}")
        return self._uri(key)

    def put_dataframe(
        self, df: pd.DataFrame, key: str, fmt: str = "parquet"
    ) -> str | None:
        """
        Uploads a DataFrame without touching the local filesystem.

        Parquet by default: it preserves the datetime index and dtypes, and
        compresses, which matters because S3 charges per request and per GB.
        CSV is available for anything a human needs to open directly.
        """
        if not self.enabled:
            return None

        buffer = io.BytesIO()
        if fmt == "parquet":
            df.to_parquet(buffer, engine="pyarrow", index=True)
        elif fmt == "csv":
            buffer.write(df.to_csv(index=True).encode())
        else:
            raise ValueError(f"Unsupported format {fmt!r}. Use 'parquet' or 'csv'.")

        return self.put_bytes(buffer.getvalue(), key)

    def save_forecast(
        self, forecast_df: pd.DataFrame, run_date: pd.Timestamp | str | None = None
    ) -> str | None:
        """
        Archives one forecast to its date partition.

        `run_date` is the date the forecast was *made*, not the period it covers,
        so a forecast can later be scored against actuals with its vintage known.
        """
        run_date = run_date or pd.Timestamp.utcnow()
        return self.put_dataframe(forecast_df, forecast_key(self.config, run_date))

    # ------------------------------------------------------------------ reads

    def get_bytes(self, key: str) -> bytes:
        """Reads an object. Raises — callers depend on the result."""
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()

    def read_dataframe(self, key: str, fmt: str = "parquet") -> pd.DataFrame:
        """Reads an object straight into a DataFrame."""
        buffer = io.BytesIO(self.get_bytes(key))
        return pd.read_parquet(buffer) if fmt == "parquet" else pd.read_csv(buffer)

    def exists(self, key: str) -> bool:
        """True if the object is present. False on 404, raises on other errors."""
        if not self.enabled:
            return False
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return False
            raise

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """
        Yields every key under a prefix.

        Uses a paginator rather than a bare list_objects_v2, which silently caps
        at 1,000 keys — the most common way to process only part of a bucket
        without noticing.
        """
        if not self.enabled:
            return
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def load_forecast_archive(self) -> pd.DataFrame:
        """
        Concatenates every archived forecast into one frame.

        This is the input to realised-accuracy tracking: join it to settled
        prices and you have a live scorecard rather than a backtest number.
        """
        keys = [
            k for k in self.list_prefix(_prefix(self.config, "forecasts"))
            if k.endswith(".parquet")
        ]
        if not keys:
            return pd.DataFrame()

        frames = []
        for key in sorted(keys):
            frame = self.read_dataframe(key)
            # dt=YYYY-MM-DD is the forecast's vintage, recoverable from the key
            frame["forecast_vintage"] = key.split("dt=")[1].split("/")[0]
            frames.append(frame)
        return pd.concat(frames)
