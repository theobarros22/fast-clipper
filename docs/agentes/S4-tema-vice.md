# S4 - Tema visual "Vice" (cores inspiradas em GTA VI)

Agente: **Sonnet** | Estimativa: 1 dia | Depende de: nada | Branch: `agente/S4`

## Contexto

O Theo quer o Fast Clipper com as cores de GTA VI: gradiente de pôr do sol (laranja, rosa-choque,
violeta) sobre a noite de Vice City, com verde-água neon de apoio. Hoje o Studio é cinza-escuro com
acento azul (`--accent: #3B82F6`); a página de aprovação já usa um rosa (`#ff4f8b`).

**Regra de marca (inegociável, está em `docs/plano-equipes-gta-v.md`):** o projeto não tem vínculo com a
Rockstar Games nem com a Take-Two. Use **só cores**. Proibido: logo de GTA ou de VI, a fonte Pricedown,
palmeiras do logo, as palavras "GTA" ou "Grand Theft Auto" na interface, imagens do jogo.

## Arquivos permitidos

- `fastclipper/web/studio.html` (só CSS e, se preciso, classes no HTML; **nenhuma mudança de JavaScript**)
- `fastclipper/web/aprovacao.html` (mesma paleta)
- `docs/agentes/relatorios/S4.md`
- `docs/agentes/relatorios/S4-antes.png` e `S4-depois.png` (capturas de tela)

## Paleta (tokens CSS, contraste já conferido pelo auditor)

| Token | Valor | Uso | Contraste sobre `--bg` |
| --- | --- | --- | --- |
| `--bg` | `#0F0A1E` | fundo, noite violeta | - |
| `--card` | `#18112D` | cartões | - |
| `--raised` | `#22183D` | campos, áreas elevadas | - |
| `--line` | `#3A2A5E` | bordas | - |
| `--ink` | `#FBF4FF` | texto principal | 18,0 |
| `--sub` | `#D2C4EC` | texto secundário | 11,9 |
| `--muted` | `#A495C6` | texto discreto | 7,1 |
| `--accent` | `#FF4FA3` | rosa neon: botão principal, seleção, foco | 6,4 |
| `--accent-hover` | `#FF2E8C` | hover do botão principal | - |
| `--accent-soft` | `rgba(255, 79, 163, 0.16)` | fundo de item ativo | - |
| `--accent-ink` | `#1B0714` | **texto sobre rosa** (branco sobre rosa dá 3,0: proibido) | 6,4 sobre o rosa |
| `--sunset` | `#FF8A3D` | laranja: destaques, badge AO VIVO | 8,3 |
| `--violet` | `#9B5CFF` | **só em gradiente e decoração**, nunca em texto | 4,96 |
| `--violet-text` | `#B98CFF` | violeta quando for texto | 7,6 |
| `--teal` | `#2EE6D6` | verde-água neon: links, info, contorno de foco secundário | 12,4 |
| `--ok` | `#3DDC97` | sucesso | 11,0 |
| `--warn` | `#FFB547` | aviso | 11,0 |
| `--error` | `#FF5D6C` | erro | 6,5 |
| `--grad-vice` | `linear-gradient(90deg, #FF8A3D 0%, #FF4FA3 50%, #9B5CFF 100%)` | faixa do topo, barra de progresso, título | - |

## O que fazer

1. Tire `S4-antes.png` do Studio aberto (`http://127.0.0.1:8000/`) antes de mudar qualquer coisa.
2. Troque os valores das variáveis em `:root` pelos da tabela e crie as que faltam.
3. Procure cores escritas direto no CSS (hoje há cerca de 24 hex distintos em `studio.html`) e troque
   pelo token equivalente. Sobrar hex solto só com justificativa no relatório.
4. Use `--grad-vice` em no máximo 3 lugares: faixa fina no topo, barra de progresso e o título do app.
   O resto da interface fica sóbrio; o rosa marca a ação principal de cada tela.
5. Botão principal: fundo `--accent`, texto `--accent-ink`. Badge de live: `--sunset`.
6. Foco de teclado visível em todos os botões e campos (`outline` em `--teal`).
7. `aprovacao.html`: mesmos tokens.
8. Tire `S4-depois.png` na mesma tela e no mesmo tamanho de janela.

## Critérios de aceite

- [ ] Nenhum `innerHTML` ou `alert(` novo (os testes do projeto verificam isso).
- [ ] Nenhuma mudança de JavaScript, de rota ou de comportamento (o `git diff` mostra só CSS e classes).
- [ ] Nenhum texto com contraste abaixo de 4,5:1. O violeta puro não aparece em texto.
- [ ] Nada de logo, fonte, imagem ou nome da franquia.
- [ ] Suíte de testes da web/Studio passando.
- [ ] As duas capturas de tela no relatório.
