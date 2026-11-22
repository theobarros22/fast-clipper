# S2 - Transcrição pelas legendas automáticas do YouTube

Agente: **Sonnet** | Estimativa: 1 a 2 dias | Depende de: nada | Branch: `agente/S2`

## Contexto

Ideia do NeonClipping (github.com/iamajmeena/neonclipping, MIT, arquivo `captions_yt.py`): vídeos do
YouTube já trazem legenda automática com tempo por palavra, no formato `json3`. Usá-la pula o Whisper:
sem GPU, sem RAM extra e em segundos. Reimplemente no nosso padrão, **não copie o código**, e cite a
origem na docstring. Twitch, Kick, arquivo local e gravação de tela continuam no Whisper.

## Arquivos permitidos

- `fastclipper/transcribe/youtube_captions.py` (novo)
- `fastclipper/ingest/vod_ingest.py` (guardar na fonte importada: URL de origem, URL da faixa `json3` e idioma)
- `fastclipper/api/app.py` (endpoint `/api/v1/studio/transcribe` e `TranscribeRequest`)
- `tests/test_youtube_captions.py` (novo)
- `tests/fixtures/youtube_json3_amostra.json` (novo, sintético, até 50 KB)
- `docs/agentes/relatorios/S2.md`

## Especificação

- `select_caption_track(info: dict, idioma: str | None) -> dict | None`
  Ordem de preferência em `info["automatic_captions"]`: `"<idioma>-orig"`, `"<idioma>"`, a única
  chave terminada em `-orig`, `"pt-orig"`, `"en-orig"`. Sem automática: a faixa manual de
  `info["subtitles"]` no idioma do vídeo. Só formato com `ext == "json3"`.
- `parse_json3(data: dict, offset_s: float = 0.0) -> list[WordTiming]`
  - início da palavra = `tStartMs + tOffsetMs`
  - fim = início da próxima palavra do mesmo evento, ou `tStartMs + dDurationMs` do evento
  - ignorar segmentos vazios e `"\n"`; descartar marcações entre colchetes (`[Música]`, `[Music]`)
  - `offset_s` é subtraído, para tempos relativos a um trecho; palavras com tempo negativo saem
- `fetch_youtube_words(url_faixa: str, timeout_s: float = 30, max_bytes: int = 40_000_000) -> list[WordTiming] | None`
  com `urllib` e `User-Agent`. Erro de rede ou de JSON devolve `None` com `logging.warning`. Nunca levanta.
- Endpoint de transcrição: novo campo `TranscribeRequest.engine: Literal["auto", "whisper", "youtube"] = "auto"`.
  - `auto`: fonte do YouTube com faixa disponível usa as legendas; senão, Whisper. Se o download da faixa falhar, recua para o Whisper.
  - `youtube` sem faixa: HTTP 422 com mensagem clara em português.
  - A resposta tem **o mesmo formato** da resposta do Whisper (as chaves que `studio.html` e o piloto usam) mais `"engine": "youtube"` ou `"whisper"`.

## Testes exigidos

**Nenhuma chamada de rede**: use a fixture e `monkeypatch` em `fetch_youtube_words`.

- seleção de faixa nos 5 casos de prioridade, mais o caso sem faixa
- parse: quantidade de palavras, tempos crescentes, sem `[Música]`, fim maior ou igual ao início
- offset de trecho
- `auto` com fonte do YouTube usa as legendas e **não** chama o Whisper (monkeypatch que falha se chamado)
- `auto` com arquivo local chama o Whisper
- falha ao baixar a faixa recua para o Whisper
- resposta com o mesmo conjunto de chaves da resposta do Whisper, mais `engine`

## Critérios de aceite

- [ ] `studio.html` funciona sem nenhuma mudança.
- [ ] Teste manual descrito no relatório: um vídeo público do YouTube com legenda automática,
      com o tempo de transcrição antes (Whisper) e depois (legendas).
