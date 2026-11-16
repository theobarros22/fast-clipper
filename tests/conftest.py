import os
import shutil
import tempfile
from pathlib import Path

# Isola dados gerados pelos testes da pasta data/ real do repositório (N7)
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="fastclipper_test_data_"))
os.environ["FASTCLIPPER_DATA_DIR"] = str(_TEST_DATA_DIR)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
