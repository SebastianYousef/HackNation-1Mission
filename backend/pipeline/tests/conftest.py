"""Point the pipeline at a throwaway data dir before atlas_pipeline is imported (config reads it once)."""
import os
import shutil
import tempfile

import pytest

os.environ["ATLAS_DATA_DIR"] = tempfile.mkdtemp(prefix="atlas-test-")
os.environ["ATLAS_LLM"] = "off"  # never call OpenAI from tests, even if .env has a key


@pytest.fixture()
def interim():
    from atlas_pipeline.config import INTERIM
    shutil.rmtree(INTERIM, ignore_errors=True)
    INTERIM.mkdir(parents=True)
    yield INTERIM
    shutil.rmtree(INTERIM, ignore_errors=True)
