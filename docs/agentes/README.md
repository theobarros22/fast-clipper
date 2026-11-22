# Painel de tarefas dos agentes - Lote 1

Origem: plano "Fast Clipper - Plano de Melhorias e Implementação" (21/11/2026) e as ideias do
NeonClipping (github.com/iamajmeena/neonclipping, licença MIT). Base: `main` no commit `9903c01`.
Regras comuns: `AGENTS.md` na raiz. Auditoria: `docs/agentes/AUDITORIA.md`.

## Divisão

- **Haiku**: tarefas pequenas, com um módulo novo e especificação fechada.
- **Sonnet**: tarefas que mexem em vários módulos ou exigem decisão de integração.
- **Claude**: escreve as fichas, revisa, audita e propõe o merge. O Theo aprova.

| ID | Tarefa | Agente | Depende de | Status |
| --- | --- | --- | --- | --- |
| H1 | Remover `mediapipe` sem uso | Haiku | - | a fazer |
| H2 | Guarda de memória | Haiku | - | a fazer |
| H3 | Léxico de exclamações PT-BR e EN | Haiku | - | a fazer |
| S1 | Falha rápida por falta de memória | Sonnet | H2 mesclada | bloqueada |
| S2 | Transcrição pelas legendas do YouTube | Sonnet | - | a fazer |
| S3 | Aprender com as aprovações do piloto | Sonnet | - | a fazer |
| S4 | Tema visual "Vice" (cores inspiradas em GTA VI) | Sonnet | - | a fazer |

## Ordem

- Podem rodar em paralelo: H1, H2, H3 (Haiku) e S2, S3, S4 (Sonnet).
- S1 só começa depois que H2 for aprovada e mesclada em `main`.
- Ordem de merge em `main`: H1, H2, S1, H3, S2, S3, S4. Antes de cada merge a branch faz rebase na `main`.

## Status

`a fazer` -> `em andamento` -> `entregue` (o agente muda até aqui, no próprio relatório)
-> `em auditoria` -> `aprovado` / `corrigir` / `rejeitado` -> `mesclado` (o auditor muda daqui em diante).

## Lote 2 (fichas escritas só depois da auditoria do Lote 1)

- Leitor de chat da live (Twitch IRC anônimo e YouTube via pytchat) alimentando o `ChatSpikeDetector`,
  com o clipe recuado alguns segundos antes do pico.
- Baixar só o áudio primeiro e depois só os trechos escolhidos (yt-dlp `download_ranges`).
- Integrar o léxico (H3) e os pesos aprendidos (S3) no `/suggest-moments`.
- Legendas com pysubs2, exportando SRT e WebVTT.
- Reenquadramento 9:16 com keyframes.
- Pacote de IA com Demucs: **bloqueado** até resolver a licença dos pesos.
