# Regras para agentes (Antigravity: Sonnet e Haiku)

Você está trabalhando no Fast Clipper. Leia este arquivo inteiro antes de começar.
Depois leia **só** a ficha da sua tarefa em `docs/agentes/` (ex.: `docs/agentes/H2-guarda-de-memoria.md`).
O painel com todas as tarefas está em `docs/agentes/README.md`.

Seu trabalho será revisado e auditado pelo Claude. A palavra final é do Theo.

## Ambiente

- Windows 11, PowerShell. Repositório em `L:\Fast-Clipper-Publico` (disco USB, mais lento).
- Python: sempre `.venv\Scripts\python.exe`. Não use `py`, nem `python` global, nem crie outra venv.
- Testes da sua tarefa: `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\<arquivo>.py`
- Suíte inteira (no fim): `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`
- Se faltar mídia de teste: `.venv\Scripts\python.exe tests\fixtures\gerar_midia_sintetica.py`
- A máquina tem 16 GB de RAM e costuma ficar com pouca memória livre. Se um teste de render falhar
  com `Cannot allocate memory` ou `mkl_malloc`, isso não é bug seu: registre no relatório e siga.

## Regras de escopo (inegociáveis)

1. Trabalhe só na branch da sua tarefa: `git switch main; git pull; git switch -c agente/<ID>`.
2. Altere apenas os arquivos listados em "Arquivos permitidos" na sua ficha.
   Precisou de outro arquivo? Pare e registre em "Dúvidas e riscos" no relatório.
3. Não adicione dependência nova (`pyproject.toml`, `requirements.txt`) a menos que a ficha mande.
4. Não toque em: `dist/`, `build/`, `data/`, `.venv/`, `fastclipper.spec`, arquivos `.bat`,
   `LICENSE.md`, nem em outros arquivos de `docs/` além do seu relatório.
5. Não apague nem enfraqueça testes existentes. Se um teste quebrar por sua mudança, corrija o código.
   Se achar que o teste está errado, explique no relatório e não mexa nele.
6. Proibido: `git push --force`, commit em `main`, merge, rebase de branches de outros agentes.
7. Sem segredos, tokens, chaves de API ou caminhos pessoais no código.
8. Código, comentários, mensagens de log e mensagens ao usuário em português, como o resto do projeto.
9. Sem `print` de depuração, sem código comentado, sem `TODO` solto.
10. Ideia vinda de outro projeto (a ficha avisa): reimplemente no nosso padrão, não copie o código,
    e cite a origem na docstring do módulo.

## Ao terminar

1. Escreva `docs/agentes/relatorios/<ID>.md` seguindo o modelo abaixo.
2. Commit na sua branch: `<tipo>(<area>): <resumo>` (ex.: `feat(transcribe): legendas do YouTube`),
   com a última linha `Agente: <modelo> via Antigravity - tarefa <ID>`.
3. `git push -u origin agente/<ID>` (somente a sua branch).
4. Pare. Revisão, correções pedidas e merge acontecem depois da auditoria.

## Modelo do relatório

```markdown
# Relatório <ID> - <título da tarefa>
Agente: <modelo>  |  Branch: agente/<ID>  |  Commit: <hash curto>

## O que foi feito
- ...

## Arquivos alterados
(cole a saída de `git diff --stat main...HEAD`)

## Critérios de aceite
- [x] critério 1 - evidência (nome do teste, comando ou saída)
- [ ] critério 2 - por que não foi cumprido

## Testes
(comando usado e as últimas linhas da saída: "N passed, M failed")

## Decisões que tomei
- ...

## Dúvidas e riscos
- ...
```
