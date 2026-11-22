# H3 - Léxico de exclamações (PT-BR e EN)

Agente: **Haiku** | Estimativa: 3 h | Depende de: nada | Branch: `agente/H3`

## Contexto

Ideia do NeonClipping (github.com/iamajmeena/neonclipping, MIT, arquivo `excite.py`): pontuar o que o
streamer **grita** ("meu Deus", "corre", "a polícia") como sinal de momento bom. Reimplemente a ideia
no nosso padrão. **Não copie o código nem a lista dele.** Cite a origem na docstring do módulo.
Esta tarefa cria só o módulo; integrar no sugeridor é do Lote 2.

## Arquivos permitidos

- `fastclipper/signals/exclamations.py` (novo)
- `tests/test_exclamations.py` (novo)
- `docs/agentes/relatorios/H3.md`

## Especificação

```python
from fastclipper.subtitles.models import WordTiming

def word_weight(palavra: str) -> float: ...
def phrase_hits(palavras: list[WordTiming]) -> list[tuple[float, float, str, float]]: ...
    # cada item: (start_s, end_s, trecho_casado, peso)
def window_score(palavras: list[WordTiming], inicio_s: float, fim_s: float) -> float: ...  # 0..1
def per_second_signal(palavras: list[WordTiming], duracao_s: float) -> list[float]: ...
```

Léxico de regex com peso, por categoria:

| Categoria | Peso | Exemplos (mínimo; pode ampliar) |
| --- | --- | --- |
| Espanto | 3 | meu deus, caraca, caralho, puta que pariu, não acredito, oh my god, no way, what the |
| Hype | 2 | vamo, bora, que isso, absurdo, insano, clipa, clip that, let's go |
| Perigo e perseguição | 2 | corre, foge, polícia, cuidado, atrás de você, atira, morreu, explodiu, bateu, run, police, watch out |
| Dor e susto | 2 | socorro, ai, aaa+, nãooo+ |
| Interjeição fraca | 0,7 | nossa, eita, opa, vish, uau, wow, oh, mano, véi |

Regras:

- Comparar em minúsculas e sem acento (`unicodedata`, NFD). "Polícia" e "policia" valem igual.
- Expressões de várias palavras casam em sequência de tokens (janela de até 4 palavras).
- Token terminado em `!` soma +1.
- Repetição ("corre corre corre") soma, com teto de 3 vezes o peso da expressão.
- `window_score` = soma dos pesos dentro da janela / (duração da janela em s x 0,6), limitado a 1,0.
- `per_second_signal` devolve uma lista com `ceil(duracao_s)` posições.
- Palavrões ficam no léxico porque são sinal real em live. Eles nunca aparecem em texto mostrado ao usuário.

## Testes exigidos

- peso de 6 palavras isoladas (com e sem acento, com maiúscula)
- frase de 2 e de 3 palavras detectada, com `start_s` e `end_s` corretos
- "corre corre corre" pontua mais que "corre"
- janela vazia devolve 0; janela cheia de gritos devolve no máximo 1
- `per_second_signal`: tamanho certo e valor maior que 0 no segundo do grito
- texto neutro de 30 palavras ("hoje vamos falar sobre o mapa novo...") com `window_score` menor que 0,1

## Critérios de aceite

- [ ] Só biblioteca padrão.
- [ ] Docstring cita o NeonClipping como origem da ideia.
- [ ] Testes rodando em menos de 1 s.
