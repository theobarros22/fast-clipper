# Contribuindo com o Fast Clipper

## Ambiente de desenvolvimento

1. Crie e ative um ambiente virtual Python 3.11+:
   ```bash
   python -m venv .venv
   ```
2. Instale o pacote em modo editável com as dependências de desenvolvimento e transcrição:
   ```bash
   pip install -e ".[dev,transcribe]"
   ```
3. Garanta `ffmpeg` e `ffprobe` disponíveis no `PATH` (e `yt-dlp` para links ao vivo/VOD).

## Testes e qualidade

1. Gere os vídeos sintéticos de teste (não ficam no Git):
   ```bash
   python tests/fixtures/gerar_midia_sintetica.py
   ```
2. Rode a suíte completa (os testes usam `FASTCLIPPER_DATA_DIR` em pasta temporária e não alteram `data/`):
   ```bash
   python -m pytest -q
   ```
3. Antes de abrir PR, valide também o script de baseline:
   ```bash
   python tools/auditar_baseline.py
   ```

## Padrões

- Todo código novo deve permanecer em `fastclipper/` com encoding UTF-8.
- Não insira HTML dinâmico via `innerHTML` nem use `alert()` em `fastclipper/web/studio.html`.
- Toda chamada externa via `subprocess.run` deve definir `timeout`.
