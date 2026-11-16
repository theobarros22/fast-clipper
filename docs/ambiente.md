# Fast Clipper — Guia de Ambiente e Reproducibilidade

Este documento registra as versões fixadas de runtime, ferramentas de sistema, drivers e comandos reproduzíveis para garantir a instalação limpa e idêntica em qualquer estação de trabalho ou worker de processamento do Fast Clipper.

---

## 1. Especificações Fixadas de Hardware e Sistema

| Componente | Especificação Homologada | Notas |
| :--- | :--- | :--- |
| **Sistema Operacional** | Windows 11 Pro / Enterprise (x86_64) | PowerShell 7+ ou PowerShell 5.1 |
| **GPU Primária** | NVIDIA GeForce RTX 3050 Laptop GPU (6 GB VRAM) ou superior | Mínimo recomendado: 4 GB VRAM para inferência local |
| **Driver NVIDIA** | Versão 610.88+ | Suporte a CUDA UMD 12.x / 13.x |
| **CUDA Toolkit / Runtime** | CUDA 12.4 (`cu124`) | Casado com a build do PyTorch |
| **FFmpeg** | 9.0-full_build (gyan.dev) | Compilado com `nvenc`, `nvdec`, `libx264`, `libass` |
| **Python Runtime** | CPython 3.11.15 (64-bit) | Versão obrigatória para compatibilidade com wheels de PyTorch, MediaPipe e Numba |
| **Gerenciador de Pacotes** | `uv` 0.12.10+ (Astral) | Resolução de dependências ultrarrápida e determinística |

---

## 2. Procedimento de Instalação Limpa Passo a Passo

### Pré-requisitos de Sistema
Certifique-se de que o executável do `ffmpeg` e do `ffprobe` estejam presentes no `PATH` do sistema:
```powershell
ffmpeg -version
ffprobe -version
```

### Passo 1: Criação do Ambiente Virtual com Python 3.11
Utilize o `uv` para isolar o ambiente com a versão exata do Python:
```powershell
# Na raiz do repositório:
uv venv --python 3.11 .venv

# Ativação no Windows (PowerShell):
.venv\Scripts\Activate.ps1
```

### Passo 2: Instalação das Dependências Fixadas
Como o repositório utiliza o repositório oficial de wheels da NVIDIA/PyTorch (`https://download.pytorch.org/whl/cu124`) como índice complementar, utilize a flag `--index-strategy unsafe-best-match` para resolver pacotes padrão simultaneamente ao PyTorch CUDA:
```powershell
uv pip install -r requirements-lock.txt --index-strategy unsafe-best-match
```

*(Opcional: instalação de pacotes de desenvolvimento/testes complementares)*:
```powershell
uv pip install pytest pytest-cov
```

---

## 3. Comandos de Verificação e Sanidade

Após a instalação, execute a verificação automatizada para validar os módulos críticos:

```powershell
.venv\Scripts\python -c "
import sys
import torch
import cv2
import mediapipe
import gradio
import whisper
import yt_dlp

print('=== DIAGNÓSTICO DO AMBIENTE FAST CLIPPER ===')
print(f'Python: {sys.version}')
print(f'PyTorch: {torch.__version__} | CUDA disponível: {torch.cuda.is_available()}')
if torch.cuda.is_available():
    print(f'GPU: {torch.cuda.get_device_name(0)} (VRAM: {torch.cuda.get_device_properties(0).total_memory / (1024**3):.2f} GB)')
print(f'OpenCV: {cv2.__version__}')
print(f'MediaPipe: {mediapipe.__version__}')
print(f'Gradio: {gradio.__version__}')
print(f'OpenAI Whisper: {whisper.__version__}')
print(f'YT-DLP: {yt_dlp.__version__}')
print('Status: TODOS OS MÓDULOS ESSENCIAIS CARREGADOS COM SUCESSO.')
"
```

---

## 4. Variáveis de Ambiente e Diretórios Temporários

Para evitar falhas de escrita e garantir que arquivos temporários permaneçam em discos de alta velocidade com espaço garantido:

| Variável | Valor Padrão | Descrição |
| :--- | :--- | :--- |
| `FASTCLIPPER_DATA_DIR` | `%LOCALAPPDATA%\FastClipper\data` | Metadados locais e estado persistido |
| `GRADIO_TEMP_DIR` | `%LOCALAPPDATA%\FastClipper\gradio_temp` | Isolamento de uploads do Gradio contra falhas de drive |
| `FFMPEG_BINARY` | `ffmpeg` | Binário invocado para processamento e filtragem |
| `FFPROBE_BINARY` | `ffprobe` | Binário invocado para validação estrita de mídia |
