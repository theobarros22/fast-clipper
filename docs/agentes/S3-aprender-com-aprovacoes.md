# S3 - Aprender com as aprovações do piloto

Agente: **Sonnet** | Estimativa: 2 dias | Depende de: nada | Branch: `agente/S3`

## Contexto

O piloto (`fastclipper/autopilot`) registra cada clipe sugerido e a decisão do Theo (`aprovado`,
`agendado`, `rejeitado`). Hoje esse dado não volta para o sugeridor. Ideia do NeonClipping
(github.com/iamajmeena/neonclipping, MIT, arquivo `learn.py`): ajustar os pesos dos sinais pelas
avaliações do usuário, com regressão ridge puxada para os pesos padrão. Reimplemente no nosso padrão,
**não copie o código**, e cite a origem na docstring.

## Arquivos permitidos

- `fastclipper/signals/learning.py` (novo)
- `fastclipper/autopilot/state.py` (guardar os sinais de cada candidato)
- `fastclipper/autopilot/runner.py` (passar os sinais ao salvar o candidato)
- `fastclipper/autopilot/routes.py` (endpoint de leitura dos pesos)
- `tests/test_learning.py` (novo)
- `docs/agentes/relatorios/S3.md`

## Especificação

1. Ao criar um candidato, salvar `features` (JSON) com as intensidades por sinal que o
   `/api/v1/studio/suggest-moments` já devolve: áudio, chat, transcrição e visual (veja os detalhes
   do `CandidateSuggestion` e do `MultimodalSignalAggregator`).
   Migração sem perder dados: `ALTER TABLE candidates ADD COLUMN features TEXT` só se a coluna não existir.
2. Rótulo: `aprovado` ou `agendado` = 1; `rejeitado` = 0; `pendente` e `falhou` ficam de fora.
3. `fit_weights(amostras, padrao: SignalWeights) -> dict`:
   - menos de 12 amostras, ou menos de 3 de algum rótulo: devolve o padrão
   - regressão ridge cujo termo de regularização puxa para o padrão (não para zero)
   - pesos não negativos, normalizados para somar 1
   - mistura com o padrão: `alpha = min(0.8, n / 60)`
   - só `numpy`
4. Gravar em `data/autopilot/weights.json` (respeitando `FASTCLIPPER_DATA_DIR`) com `n`, `alpha`,
   data e pesos. Arquivo ausente ou corrompido: usa o padrão, sem exceção.
5. `GET /api/v1/autopilot/weights` devolve `padrao`, `aprendido`, `n` e `alpha`.
6. **Não aplicar** os pesos no sugeridor ainda. Isso é do Lote 2, depois da auditoria.
   Esta tarefa só aprende e expõe.

## Testes exigidos

- abaixo do mínimo de amostras: devolve o padrão
- dados sintéticos em que só o áudio separa aprovado de rejeitado: o peso do áudio sobe em relação ao padrão
- pesos não negativos e soma 1 (tolerância 1e-6)
- `alpha` cresce com `n` e para em 0,8
- banco antigo sem a coluna `features` abre, ganha a coluna e não perde linhas
- `weights.json` corrompido: devolve o padrão, sem exceção
- o endpoint devolve as 4 chaves

## Critérios de aceite

- [ ] `tests/test_autopilot.py` continua verde.
- [ ] Nenhuma dependência nova.
- [ ] O sugeridor não muda de comportamento nesta tarefa.
