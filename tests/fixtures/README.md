# Conjunto de Avaliação e Fixtures de Teste

Este diretório contém as ferramentas e especificações para geração de **mídia sintética determinística** e para a gestão do **manifesto local de gravações autorizadas**, em estrita conformidade com o **M0 (Baseline e Conjunto de Avaliação)** do Fast Clipper.

---

## 1. Princípios de Segurança e Privacidade

1. **Mídias reais nunca são enviadas ao Git:** Arquivos `.mp4`, `.mov`, `.mkv`, `.wav` e derivados estão explicitamente bloqueados no `.gitignore`.
2. **Autorização explícita:** Gravações de transmissões (como trechos de GTA V) devem possuir consentimento comprovado e termo de uso registrado no manifesto local.
3. **Mídia Sintética como Primeira Linha:** Todos os testes unitários e de integração contínua devem rodar sobre mídia sintética gerada por script via FFmpeg, sem depender de arquivos binários pesados no repositório.

---

## 2. Geração de Mídia Sintética Determinística

Para gerar a suite completa de arquivos sintéticos de teste na pasta `tests/fixtures/synthetic/`, execute o script utilitário:

```powershell
.venv\Scripts\python tests/fixtures/gerar_midia_sintetica.py
```

### Casos de Teste Sintéticos Gerados:

| Arquivo | Duração | Resolução | Áudio | Cenário Testado |
| :--- | :--- | :--- | :--- | :--- |
| `sintetico_gameplay_facecam.mp4` | 6.0s | 1920x1080 @ 30fps | Senoide 440Hz com picos em 2s e 4s | Gameplay simulado com facecam no canto superior direito (PIP) e cortes de cena |
| `sintetico_gameplay_puro.mp4` | 5.0s | 1920x1080 @ 30fps | Senoide contínua 220Hz | Gameplay limpo em 16:9, sem pessoas e sem facecam |
| `sintetico_duas_pessoas.mp4` | 6.0s | 1920x1080 @ 30fps | Estéreo alternado (Esq / Dir) | Duas pessoas (simuladas à esquerda e à direita) para teste de separação e veto de fusão |
| `sintetico_sem_audio.mp4` | 3.0s | 1920x1080 @ 30fps | Ausente | Validação de resiliência e emissão da bandeira `requer_revisao` |

---

## 3. Manifesto Local de Gravações Autorizadas

Para os testes de calibração e avaliação com gravações reais (ex.: sessões autorizadas de GTA V), configure o arquivo local:

```powershell
Copy-Item tests/fixtures/manifesto_local.example.json tests/fixtures/manifesto_local.json
```

O arquivo `manifesto_local.json` é ignorado pelo Git e registra os metadados dos trechos selecionados para o baseline:

```json
{
  "versao_manifesto": "1.0.0",
  "data_auditoria": "2026-11-16",
  "avaliador_responsavel": "Equipe Fast Clipper",
  "gravacoes": [
    {
      "id": "gtav_sessao_01_facecam",
      "caminho_local": "C:/Dados/MidiasAutorizadas/gtav_stream_01.mp4",
      "duracao_segundos": 1800.0,
      "layout_categoria": "gameplay_com_facecam",
      "facecam_coordenadas": {
        "x": 1440,
        "y": 40,
        "largura": 440,
        "altura": 330
      },
      "gameplay_coordenadas": {
        "x": 0,
        "y": 0,
        "largura": 1920,
        "altura": 1080
      },
      "autorizacao": {
        "streamer": "Streamer Autorizado A",
        "canal": "https://youtube.com/@exemplo",
        "data_consentimento": "2026-11-15",
        "tipo_licenca": "Uso interno para testes e benchmarks do Fast Clipper"
      },
      "intervalos_rotulados": [
        {
          "intervalo_id": "int_01",
          "inicio_s": 124.5,
          "fim_s": 168.0,
          "tipo": "util",
          "evento": "Perseguição policial com capotamento e reação expressiva",
          "enquadramento_esperado": "empilhado_facecam_gameplay",
          "conjunto": "calibracao"
        },
        {
          "intervalo_id": "int_02",
          "inicio_s": 450.0,
          "fim_s": 510.0,
          "tipo": "inutil",
          "evento": "Menu de pausa e compra de munição em silêncio",
          "enquadramento_esperado": "nenhum",
          "conjunto": "avaliacao"
        }
      ]
    }
  ]
}
```

---

## 4. Separação Estrita: Calibração vs. Avaliação

Para evitar overfitting dos detectores e garantir medições idôneas:
- **Conjunto de Calibração (50% dos trechos):** Usado para ajustar parâmetros de detecção de corte de cena, sensibilidade de z-score de áudio e caixas de enquadramento.
- **Conjunto de Avaliação (50% dos trechos):** Mantido intocado e processado apenas na rodada final de medição de precisão, recall e tempo de intervenção humana.
