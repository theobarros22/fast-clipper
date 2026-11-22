import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Isola dados gerados pelos testes da pasta data/ real do repositório (N7)
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="fastclipper_test_data_"))
os.environ["FASTCLIPPER_DATA_DIR"] = str(_TEST_DATA_DIR)


@pytest.fixture(autouse=True)
def _desligar_guarda_memoria_para_testes(monkeypatch):
    """Desliga a guarda de memória física por padrão nos testes gerais.

    Evita que testes com renders ou transcrições reais dependam da RAM física
    livre da máquina hospedeira. Testes dedicados à guarda de memória removem
    a variável ou usam monkeypatch próprio.
    """
    monkeypatch.setenv("FASTCLIPPER_MIN_FREE_GB", "0")


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
