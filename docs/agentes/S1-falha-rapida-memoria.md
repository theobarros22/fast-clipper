# S1 - Falha rápida por falta de memória

Agente: **Sonnet** | Estimativa: 1 dia | Depende de: **H2 mesclada em `main`** | Branch: `agente/S1`

## Contexto

Em 20/11, com 0,9 GB livres: o Whisper falhou (`mkl_malloc`), o render falhou (`Cannot allocate memory`),
a fila recolocou o job (até `max_attempts=3`) e `AutopilotRunner._wait_jobs` consultou em silêncio
até o timeout de 300 s. Objetivo: falhar em segundos, com uma mensagem que diga o que fazer.
Use `fastclipper/process/memory_guard.py` (entregue pela H2).

## Arquivos permitidos

- `fastclipper/transcribe/whisper_service.py`
- `fastclipper/rendering/pipeline_runner.py`
- `fastclipper/queue/durable_queue.py`
- `fastclipper/autopilot/runner.py`
- `fastclipper/api/app.py` (só para traduzir o erro em HTTP 503)
- `tests/test_memory_fail_fast.py` (novo)
- `docs/agentes/relatorios/S1.md`

## O que fazer

1. `WhisperTranscriptionService.transcribe`: chamar `ensure_free_memory(MINIMOS_GB["transcricao"], "transcricao")`
   **antes** de carregar o modelo.
2. Render: chamar `ensure_free_memory(MINIMOS_GB["render"], "render")` antes de iniciar o FFmpeg
   (`render_clip` ou `process_job`, onde fizer mais sentido; explique a escolha no relatório).
3. `DurableTaskQueue.fail_job`: novo parâmetro `retryable: bool = True`. Com `False`, o job vai
   direto para `FALHOU`, sem nova tentativa. O comportamento padrão não muda.
4. `pipeline_runner.process_job`: se a exceção for `InsufficientMemoryError` ou
   `is_out_of_memory_error(mensagem)` for verdadeiro, chamar `fail_job(..., retryable=False)` com
   mensagem em português começando por `Sem memória:`.
5. `app.py`: `InsufficientMemoryError` vira HTTP 503 com `detail = str(erro)` nos endpoints de transcrição e render.
6. `AutopilotRunner._wait_jobs`: `logger.info` só quando o status ou o `step_description` do job
   mudar (não a cada consulta). Falha com `Sem memória` encerra a espera daquele job na hora.
7. Não mudar nenhum timeout padrão.

## Testes exigidos

Sem consumir RAM de verdade: `monkeypatch` em `memory_guard.free_memory_gb`.

- `transcribe` com 0,5 GB livres levanta `InsufficientMemoryError` e **não** chama `_get_model`
- job de render que lança `Cannot allocate memory` termina em `FALHOU` com `attempts == 1`
- falha comum (`Invalid argument`) continua sendo recolocada na fila (comportamento antigo)
- `POST /api/v1/studio/transcribe` com memória baixa devolve 503 com a mensagem
- cenário do piloto com memória baixa termina em menos de 10 s (medir com `time.monotonic`)

## Critérios de aceite

- [ ] Testes novos passando, junto com `test_autopilot.py`, `test_screen_capture.py` e os testes de API.
- [ ] Suíte inteira sem regressão (cole o resumo no relatório).
- [ ] Mensagem ao usuário em português, com o número de GB.
