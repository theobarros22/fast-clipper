# H2 - Guarda de memória

Agente: **Haiku** | Estimativa: 2 h | Depende de: nada | Branch: `agente/H2`

## Contexto

Em 20/11 o PC ficou com 0,9 GB livres. O Whisper falhou com `mkl_malloc: failed to allocate memory`,
o FFmpeg com `Cannot allocate memory`, e o piloto esperou 300 s em silêncio. Precisamos de uma
checagem única e reutilizável antes das etapas pesadas. **Esta tarefa cria só o módulo.**
Quem o usa é a tarefa S1.

## Arquivos permitidos

- `fastclipper/process/memory_guard.py` (novo)
- `tests/test_memory_guard.py` (novo)
- `docs/agentes/relatorios/H2.md`

## Especificação

```python
MINIMOS_GB: dict[str, float] = {"transcricao": 2.5, "render": 2.0, "pacote_ia": 3.0}

class InsufficientMemoryError(RuntimeError):
    """Memória livre abaixo do mínimo para uma etapa."""
    def __init__(self, etapa: str, livre_gb: float, minimo_gb: float): ...
    # atributos públicos: etapa, livre_gb, minimo_gb

def free_memory_gb() -> float | None: ...
def ensure_free_memory(minimo_gb: float, etapa: str) -> float: ...
def is_out_of_memory_error(texto: str) -> bool: ...
```

- `free_memory_gb`: no Windows, `ctypes` + `GlobalMemoryStatusEx` (campo `ullAvailPhys`);
  no Linux, `MemAvailable` de `/proc/meminfo`. Qualquer erro devolve `None`. **Sem psutil.**
- `ensure_free_memory`:
  - `free_memory_gb()` devolveu `None`: não bloqueia, devolve `-1.0` e faz `logging.warning` uma única vez por processo.
  - livre menor que o mínimo: levanta `InsufficientMemoryError`.
  - senão: devolve o valor livre.
  - A variável de ambiente `FASTCLIPPER_MIN_FREE_GB`, se definida, substitui o mínimo de qualquer etapa (`0` desliga a guarda).
- `str(InsufficientMemoryError("render", 0.9, 2.0))` deve ser exatamente:
  `Memória livre 0,9 GB; a etapa 'render' precisa de 2,0 GB. Feche programas e tente de novo.`
  (vírgula decimal, 1 casa).
- `is_out_of_memory_error`: `True` se o texto contém, sem diferenciar maiúsculas, qualquer um de
  `cannot allocate memory`, `mkl_malloc`, `out of memory`, `std::bad_alloc`, `memoryerror`,
  `cudaerrormemoryallocation`.

## Testes exigidos

Sem depender da RAM real: use `monkeypatch` em `free_memory_gb`.

- livre acima do mínimo: devolve o valor
- livre abaixo: levanta, com a mensagem exata e os 3 atributos
- `free_memory_gb` devolvendo `None`: não levanta
- `FASTCLIPPER_MIN_FREE_GB=0`: nunca levanta
- `free_memory_gb()` real devolve `float > 0` ou `None`, sem exceção
- `is_out_of_memory_error`: os 6 textos positivos, mais 3 negativos (`"Invalid argument"`, `""`, `"No such file"`)

## Critérios de aceite

- [ ] Testes novos passando em menos de 2 s.
- [ ] Nenhum outro arquivo alterado.
- [ ] Nenhuma dependência nova.
