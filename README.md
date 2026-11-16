# Fast Clipper

[Português](#português) | [English](#english)

## Português

O **Fast Clipper** é um projeto independente em desenvolvimento para ajudar criadores, editores e equipes a transformar transmissões ao vivo e vídeos longos em clipes curtos, verticais e prontos para revisão.

A ideia é reduzir o tempo entre um momento interessante e um clipe pronto para publicação, mantendo o controle editorial e a possibilidade de corrigir cada resultado. O projeto parte de uma base do **Viral Cutter Classic**, preservado como projeto separado.

### O que queremos construir

- **Lives e vídeos gravados:** acompanhar fontes autorizadas, marcar momentos e preservar o contexto antes e depois de cada evento.
- **Sugestões assistidas:** investigar sinais como fala, áudio, chat e mudanças visuais para ajudar a encontrar trechos interessantes. As sugestões dependerão de validação humana.
- **Enquadramento vertical:** criar clipes 9:16 que mantenham pessoas, facecam e gameplay visíveis, com layouts ajustáveis e tela dividida quando necessário.
- **Revisão e edição:** ajustar início e fim, legendas, títulos e enquadramento; reprocessar um clipe individualmente.
- **Trabalho em equipe:** organizar usuários, canais, turnos, tarefas e um mapa compartilhado de momentos, com isolamento entre organizações.
- **Comparação de reações:** associar gravações autorizadas de diferentes criadores ao mesmo acontecimento e montar sequências comparativas.
- **Distribuição:** planejar conexões com plataformas sociais, publicação com aprovação e acompanhamento de resultados quando as APIs permitirem.
- **Continuidade:** acompanhar o progresso e retomar trabalhos após interrupções, aproveitando etapas e arquivos válidos.

### Estado do projeto

**Projeto em fase inicial.** O repositório contém uma base derivada do Viral Cutter Classic e uma proposta de produto. Captura ao vivo, colaboração multiusuário, publicação e comparação entre criadores ainda precisam ser implementadas e validadas. Não é uma versão pronta para produção.

A primeira etapa prevista é auditar a base, estabelecer testes reproduzíveis e avaliar gravações autorizadas de lives de GTA V, incluindo enquadramento de gameplay e facecam. Esses testes não foram apresentados aqui como concluídos.

### Participe

Contribuições são bem-vindas em processamento de vídeo, FFmpeg, Python, análise de cenas, acessibilidade, interface, testes e documentação. Antes de iniciar uma mudança importante, abra uma issue para discutir escopo e compatibilidade.

Leia o [plano de produto e arquitetura](docs/plano-fast-clipper.md) e a [licença](LICENSE.md). **A licença atual é personalizada e não comercial; não é uma licença open source aprovada pela OSI.**

Use somente conteúdo próprio ou autorizado. Não envie vídeos privados, credenciais ou dados pessoais ao repositório. O Fast Clipper é independente e não é afiliado à Rockstar Games ou à Take-Two Interactive.

---

## English

**Fast Clipper** is an independent project in development, intended to help creators, editors, and teams turn live streams and long videos into short vertical clips ready for review.

The goal is to reduce the time between an interesting moment and a clip ready for publication, while keeping editorial control and allowing each result to be corrected. The project starts from a **Viral Cutter Classic** code base, which remains a separate project.

### What we plan to build

- **Live streams and recorded video:** monitor authorized sources, mark moments, and preserve context before and after each event.
- **Assisted suggestions:** investigate speech, audio, chat, and visual changes to help find interesting moments. Suggestions will require human validation.
- **Vertical framing:** create 9:16 clips that keep people, facecams, and gameplay visible, with adjustable layouts and split screen when needed.
- **Review and editing:** adjust clip boundaries, captions, titles, and framing; reprocess individual clips.
- **Team collaboration:** organize users, channels, shifts, assignments, and a shared moment map, with isolation between organizations.
- **Reaction comparisons:** associate authorized recordings from different creators with the same event and build comparison sequences.
- **Distribution:** plan social-platform connections, approval-based publishing, and performance tracking where APIs permit.
- **Continuity:** show progress and resume interrupted work using valid completed stages and files.

### Project status

**Early-stage project.** This repository contains a code base derived from Viral Cutter Classic and a product proposal. Live capture, multi-user collaboration, publishing, and creator comparisons still need to be implemented and validated. It is not production-ready.

The first planned milestone is to audit the code base, establish reproducible tests, and evaluate authorized recordings of GTA V streams, including gameplay and facecam framing. Those tests are not claimed as completed here.

### Contribute

Contributions are welcome in video processing, FFmpeg, Python, scene analysis, accessibility, interface design, testing, and documentation. Please open an issue before starting a significant change so its scope and compatibility can be discussed.

Read the [product and architecture plan](docs/plano-fast-clipper.md) and the [license](LICENSE.md). **The current license is a custom noncommercial license, not an OSI-approved open-source license.**

Use only content you own or are authorized to use. Do not commit private videos, credentials, or personal data. Fast Clipper is independent and is not affiliated with Rockstar Games or Take-Two Interactive.
