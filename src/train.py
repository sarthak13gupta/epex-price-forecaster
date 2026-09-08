"""
Training entrypoint.

Kept as a thin shim so `python src/train.py` and the container's default command
both work; the pipeline itself lives in src/pipelines/train_pipeline.py.
"""

from src.pipelines.train_pipeline import main

if __name__ == "__main__":
    main()
