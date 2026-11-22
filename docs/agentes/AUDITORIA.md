# Protocolo de auditoria (Claude)

Toda entrega dos agentes passa por estes 7 pontos antes de qualquer merge. O relatório do agente
é lido, mas não é prova: cada critério é conferido de novo pelo auditor.

1. **Escopo.** `git diff --stat main...origin/agente/<ID>` só pode listar os arquivos permitidos na ficha.
2. **Testes.** O auditor roda os testes da ficha e a suíte inteira, na máquina do Theo e num clone limpo.
3. **Critérios de aceite.** Cada um conferido com evidência própria.
4. **Leitura do código.** Bugs, casos de borda, exceções engolidas, mudança de comportamento não pedida,
   custo de memória e de tempo.
5. **Testes enganosos.** Teste que passa sem testar: asserts fracos, mocks que escondem o comportamento,
   teste que nunca falharia.
6. **Segurança e licença.** Segredos, acesso de rede inesperado, código copiado de outro projeto
   (comparar com a origem quando a ficha citar uma) e atribuição.
7. **Consistência.** Português, padrão de nomes do projeto, sem código morto.

## Veredito

- **APROVADO**: o auditor propõe o merge; o merge só acontece com o OK do Theo.
- **CORRIGIR**: lista numerada de correções volta para o mesmo agente, na mesma branch.
- **REJEITADO**: motivo registrado; a tarefa volta ao painel como `a fazer`.

Registro em `docs/agentes/auditorias/<ID>.md`: veredito, o que foi conferido, comandos e saídas,
e a lista de correções quando houver.
