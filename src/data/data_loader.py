import io
import os

import boto3
import pandas as pd
from dotenv import load_dotenv

from src.utils.config_loader import load_config

load_dotenv()

VALID_DATASET_TYPES = ("train", "predict")


def load_raw_dataset(dataset_type: str = "train") -> pd.DataFrame:
    """
    Loads a raw CSV from local disk or S3 depending on the ENV toggle.

    `dataset_type` selects between the historical training file and the
    forward-looking prediction file, which carries only the known-ahead
    exogenous inputs.
    """
    if dataset_type not in VALID_DATASET_TYPES:
        raise ValueError(
            f"Unknown dataset_type {dataset_type!r}. "
            f"Expected one of {list(VALID_DATASET_TYPES)}."
        )

    config = load_config()
    env = os.getenv("ENV", "local").lower()

    if dataset_type == "predict":
        s3_file_key = os.getenv("S3_PRED_FILE_KEY", "epex_fr_temp_pred.csv")
        local_path = config["paths"]["raw_pred_csv"]
    else:
        s3_file_key = os.getenv("S3_TRAIN_FILE_KEY", "epex_fr_temp_train.csv")
        local_path = config["paths"]["raw_train_csv"]

    if env == "production":
        return _load_from_s3(s3_file_key)

    return _load_from_disk(local_path)


def _load_from_s3(s3_file_key: str) -> pd.DataFrame:
    """Streams a CSV out of S3 straight into a DataFrame."""
    bucket_name = os.getenv("S3_BUCKET_NAME")

    if not bucket_name:
        raise ValueError(
            "ENV=production requires S3_BUCKET_NAME to be set in the environment."
        )

    print(f"[PROD] Fetching s3://{bucket_name}/{s3_file_key} ...")

    try:
        # Credentials are deliberately NOT passed. boto3 resolves them through
        # its own chain, which makes the same code work in both environments:
        #
        #   1. environment variables      <- local dev, loaded from .env
        #   2. ~/.aws/credentials
        #   3. IAM instance role          <- EC2, injected and auto-rotating
        #
        # Passing them explicitly would force long-lived AKIA keys to exist in
        # a file that works from anywhere in the world if it leaks. Omitting
        # them is what allows the .env keys to have no production role at all.
        s3_client = boto3.client(
            "s3", region_name=os.getenv("AWS_DEFAULT_REGION")
        )
        response = s3_client.get_object(Bucket=bucket_name, Key=s3_file_key)
        df = pd.read_csv(io.BytesIO(response["Body"].read()))
    except Exception as exc:
        print(f"[PROD ERROR] Failed to fetch data from S3: {exc}")
        raise

    print(f"[PROD] Successfully fetched {len(df)} rows from S3.")
    return df


def _load_from_disk(local_path: str) -> pd.DataFrame:
    """Reads a CSV from the local filesystem."""
    print(f"[LOCAL] loading dataset from local path: {local_path}...")

    try:
        df = pd.read_csv(local_path)
    except Exception as exc:
        print(f"[LOCAL ERROR] Failed to load data from disk: {exc}")
        raise

    print(f"[LOCAL] Successfully loaded {len(df)} rows from local disk")
    return df


if __name__ == "__main__":
    df_train = load_raw_dataset(dataset_type="train")
    df_pred = load_raw_dataset(dataset_type="predict")

    print("\nTraining Head:")
    print(df_train.head(2))
    print("\nPrediction Head:")
    print(df_pred.head(2))
