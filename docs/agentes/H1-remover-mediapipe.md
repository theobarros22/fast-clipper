# H1 - Remover `mediapipe` sem uso

Agente: **Haiku** | Estimativa: 30 min | Depende de: nada | Branch: `agente/H1`

## Contexto

`mediapipe` está declarado no extra `vision` do `pyproject.toml`, mas em 21/11 não havia nenhuma
referência a ele em `fastclipper/` nem em `tests/`. Dependência morta deixa a instalação mais lenta.

## Arquivos permitidos

- `pyproject.toml`
- `requirements.txt` (só se citar mediapipe)
- `README.md` (só se citar mediapipe como requisito)
- `docs/agentes/relatorios/H1.md`

## O que fazer

1. Confirme a busca e cole a saída no relatório:
   `Get-ChildItem fastclipper,tests -Recurse -Include *.py | Select-String -Pattern mediapipe`
   Se aparecer um `import mediapipe` real, **pare** e relate. Não remova nada.
2. Remova a linha do `mediapipe` do extra `vision`. Mantenha `numpy` e `opencv-python`.
3. Não desinstale pacotes da `.venv`.
4. O `fastclipper.spec` embute `face_detector.tflite`, que pode ser um resto do mediapipe.
   **Não mexa no `.spec`**: só registre no relatório se o arquivo é usado em algum `.py`
   (`Select-String -Pattern face_detector.tflite`).

## Critérios de aceite

- [ ] Nenhuma menção a `mediapipe` em `pyproject.toml`.
- [ ] `.venv\Scripts\python.exe -c "import fastclipper.api.app"` termina sem erro.
- [ ] Testes de layout e facecam passando (os arquivos `tests\test_*layout*.py` e `tests\test_*face*.py` que existirem).
- [ ] Relatório com a saída das duas buscas.
