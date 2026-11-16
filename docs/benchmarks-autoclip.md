# Benchmarking & Engenharia Reversa: AutoClip (`zhouxiaoka/autoclip`)

**Data:** 2026-11-16  
**Repositório Analisado:** `https://github.com/zhouxiaoka/autoclip.git`  
**Objetivo:** Extrair padrões de arquitetura de alta confiabilidade, algoritmos determinísticos e casos de teste validados no ecossistema do AutoClip para elevar a robustez do **Fast Clipper**.

---

## 1. Visão Geral do AutoClip

O **AutoClip** é uma ferramenta de ponta para geração automatizada de cortes verticais e coletâneas a partir de vídeos longos (podcasts, entrevistas, gameplays e palestras). Possui arquitetura baseada em **Tauri (Rust) + React/TypeScript** no desktop e **FastAPI/Celery/Python** no backend de processamento de mídia.

Sua filosofia central de engenharia é descrita em `backend/pipeline/quality.py`:
> *"Engenharia de qualidade de corte: retirar do LLM tudo o que o código pode calcular deterministicamente."*

---

## 2. Inovações & Melhorias Incorporadas ao Fast Clipper

Analisamos o código-fonte, pipeline e a suíte de testes do AutoClip, extraindo e adaptando cinco componentes críticos para o Fast Clipper:

### 2.1 Media Pre-check Guard (`fastclipper/ingest/precheck.py`)
* **Problema Resolvido no AutoClip (RC156 Win QA #10, #11):** Vídeos de 10 segundos ou sem faixa de áudio executavam chamadas completas de Whisper, GPU e LLM por mais de 100 segundos até falharem com `IndexError` ou `timeline_empty`.
* **Solução Implementada:** Sonda estrita preliminar via FFprobe antes de qualquer processamento pesado. Rejeita imediatamente arquivos sem áudio (`source_no_audio`), menores que a janela mínima (`source_too_short`) ou corrompidos (`source_corrupted`).
* **Integração no CLI:** `python -m fastclipper precheck --video <caminho>`

### 2.2 Supervisor de Encoders H.264 & Fallback (`fastclipper/rendering/encoder.py`)
* **Problema Resolvido:** Encoders de hardware (`h264_nvenc`, `h264_qsv`, `h264_amf`, `h264_videotoolbox`) podem falhar aleatoriamente por exaustão de VRAM, limite de sessões ou drivers desatualizados.
* **Solução Implementada:**
  1. Teste de sonda em tempo de execução com frame sintético de 0.3s antes de aceitar o encoder de hardware.
  2. Cálculo dinâmico de bitrate baseado em densidade de pixels: $\text{bitrate} = \max(1.5\text{ Mbps}, \text{largura} \times \text{altura} \times \text{fps} \times 0.13)$.
  3. `mark_broken(encoder)`: Caso um clipe real falhe durante a renderização, o encoder de hardware é marcado como quebrado para o ciclo de vida do worker e o sistema recorre imediatamente a `libx264` (software) sem derrubar a fila.

### 2.3 Deduplicação Temporal & Perfis Adaptativos (`fastclipper/signals/clustering.py`)
* **Problema Resolvido:** Picos combinados de áudio e chat geravam múltiplos candidatos sobrepostos com 80%+ de interseção temporal para o mesmo momento da live.
* **Solução Implementada:**
  1. `DurationProfile`: Parâmetros adaptativos conforme a duração da live/vídeo (`short` <15m, `medium` 15-45m, `long` >45m).
  2. Cálculo de razão de sobreposição (IoU temporal): Se dois candidatos compartilham $\ge 50\%$ da janela do menor, o de menor pontuação é absorvido pelo de maior pontuação.
  3. `select_clips`: Garantia determinística de preenchimento mínimo (`min_keep`), impedindo que o operador receba zero clipes para uma transmissão longa.

### 2.4 Alinhamento em Limites de Sentenças (`fastclipper/signals/boundaries.py`)
* **Problema Resolvido:** Linhas de legendas ASR frequentemente cortam frases pela metade (ex: *"Eu acho que a gente"*, *"deve acelerar tudo agora. E então"*). O corte em timestamps soltos deixava frases incompletas ou carregava perguntas soltas do tópico seguinte.
* **Solução Implementada:**
  1. O início recua até o início da frase/pensamento anterior (`max_lead_s = 15s`).
  2. O fim avança até a conclusão da frase atual com pontuação terminal (`.`, `!`, `?`, `...`).
  3. Detector de perguntas finais (`QUESTION_ENDING`): Desconecta perguntas do próximo tópico no fim de uma resposta.
  4. Detecção por pausa vocal (`PAUSE_SEC = 0.6s`) quando o ASR é desprovido de pontuação.

### 2.5 Contenção de Processos Zumbis no Windows (`fastclipper/process/child_guard.py`)
* **Problema Resolvido:** No Windows, quando o processo principal do worker/CLI é finalizado (crash, `SIGTERM` ou `Ctrl+C`), processos filhos do FFmpeg continuavam rodando em segundo plano, travando arquivos MP4 em disco e consumindo GPU/CPU.
* **Solução Implementada:** Criação de um **Windows Job Object** via `ctypes` com a flag `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x00002000)`. Qualquer processo associado ao job é sumariamente eliminado pelo kernel do Windows assim que o handle do processo pai é fechado.

---

## 3. Matriz de Casos de Teste Importados e Validados

Foram criados 16 novos testes automatizados em [tests/test_autoclip_enhancements.py](file:///l:/Fast-Clipper-Publico/tests/test_autoclip_enhancements.py) e [tests/test_cli.py](file:///l:/Fast-Clipper-Publico/tests/test_cli.py):

| Caso de Teste | Origem no AutoClip | Validação no Fast Clipper | Status |
| :--- | :--- | :--- | :--- |
| `test_precheck_valid_media` | `test_media_precheck.py` | Fixture válida passa com resolução e fps corretos | Aprovado |
| `test_precheck_file_not_found` | `test_media_precheck.py` | Arquivo inexistente retorna `file_not_found` | Aprovado |
| `test_precheck_no_audio_detected` | `test_media_precheck.py` | Fixture sem áudio rejeitada com `source_no_audio` | Aprovado |
| `test_precheck_source_too_short` | `test_media_precheck.py` | Mídia menor que limite mínimo gera `source_too_short` | Aprovado |
| `test_encoder_selection_and_bitrate` | `test_video_encoder.py` | Detecta encoder de hardware e escala bitrate com fps | Aprovado |
| `test_forcing_software_encoder_via_env` | `test_video_encoder.py` | `FASTCLIPPER_VIDEO_ENCODER=x264` força `libx264` | Aprovado |
| `test_encoder_fallback_on_broken` | `test_video_encoder.py` | Falha em runtime do NVENC faz fallback para CPU | Aprovado |
| `test_duration_profiles_tiers` | `quality.py (DurationProfile)` | Parâmetros de corte adaptam-se para 5m, 30m e 2h | Aprovado |
| `test_temporal_overlap_deduplication`| `quality.py (refine_timeline)` | Sobreposição > 50% funde candidatos duplicados | Aprovado |
| `test_select_clips_supplement` | `quality.py (select_clips)` | Suplementa candidatos para garantir `min_keep` | Aprovado |
| `test_sentence_boundary_snap` | `test_boundaries.py` | Recua ao início da frase e descarta pergunta final | Aprovado |
| `test_sentence_boundary_pause` | `test_boundaries.py` | Usa pausas de 0.6s quando não há pontuação | Aprovado |
| `test_child_process_guard_reg` | `child_guard.py` | Registra e associa processo ao Windows Job | Aprovado |
| `test_child_process_guard_kill_all` | `child_guard.py` | `kill_all_guarded()` mata todos os subprocessos | Aprovado |
| `test_cli_precheck_valid` | AutoClip CLI | Validação do comando `precheck` no CLI unificado | Aprovado |
| `test_cli_precheck_invalid` | AutoClip CLI | Código de saída 1 para mídia sem áudio no CLI | Aprovado |

---

## 4. Evolução da Cobertura de Testes

* **Antes da análise do AutoClip:** 51 testes passando (Marcos M0 a M6).
* **Após incorporar melhorias do AutoClip:** **67 testes passando (100% de sucesso)** em 9.36s.
