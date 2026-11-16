# IoT Vision

Sistema de monitoramento do uso de Equipamentos de Proteção Individual (EPIs) com visão computacional e Internet das Coisas.

Ele identifica a ausência de **capacete**, **cinta** e **luva** em imagens de câmeras de segurança e avisa:
- o **trabalhador**, por um relógio inteligente (LILYGO T-Watch S3);
- a **supervisão**, pelo Telegram.

Projeto desenvolvido como Trabalho de Conclusão de Curso do Bacharelado em Sistemas de Informação do Instituto Federal de Alagoas (IFAL), Campus Arapiraca.

**Autores:** Italo dos Santos e João Guilherme Silva Souza
**Orientadora:** Profª. Drª. Renata Imaculada Soares Pereira

---

## Estrutura

```
aplicacao/   aplicação de monitoramento em Python (vídeo gravado ou câmera RTSP)
firmware/    firmware do relógio LILYGO T-Watch S3 (C++, Arduino, PlatformIO)
```

O treinamento dos modelos (notebook, histórico dos experimentos e métricas) está no repositório **DETECTOR_IA_YOLO**.

---

## Como funciona

1. A câmera é lida por RTSP (ou um vídeo gravado é aberto), sempre mantendo o quadro mais recente.
2. Um YOLO11n pré-treinado no COCO localiza as pessoas presentes na cena.
3. Cada pessoa é recortada com margem e o recorte vai para o modelo de EPIs (**REF01**, YOLO11n treinado no projeto).
4. A conformidade é avaliada **por pessoa**. Os estados possíveis são `SEM TRABALHADOR`, `CONFORME`, `NAO CONFORME` e `INCONCLUSIVO`, e cada mudança de estado só vale depois de 3 quadros seguidos iguais.
5. Em `NAO CONFORME`:
   - a ocorrência é registrada em CSV, com uma imagem de evidência;
   - o Telegram recebe a mensagem com a imagem, com intervalo mínimo de 15 s entre alertas e lembrete a cada 60 s;
   - o relógio fica com a tela vermelha mostrando os EPIs ausentes e vibra a cada 10 s até a situação ser regularizada.

---

## Aplicação (`aplicacao/`)

### Requisitos
- Python 3.10 ou superior
- Dependências: `pip install -r requirements.txt`

### Configuração
1. Copie `.env.exemplo` para `.env`.
2. Preencha o endereço RTSP da câmera e o token e o chat do Telegram.
3. O arquivo `.env` **não deve ser versionado**.

### Execução (Windows)

| Arquivo | Função |
|---|---|
| `1_TESTE_VIDEO.bat` | Processa um vídeo gravado (padrão: `teste.MOV`, ou o caminho passado como argumento) |
| `2_PRODUCAO_CAMERA.bat` | Monitora a câmera ao vivo pelo endereço RTSP do `.env` |
| `3_COLETA_DADOS_TCC.bat` | Roda os testes padronizados de desempenho e gera um relatório |

Na primeira execução, o `_preparar_ambiente.bat` cria o ambiente virtual e instala as dependências, se for preciso.

Os mesmos programas podem ser executados com Python:

```bash
python executar_teste.py caminho/do/video.mp4
python executar_producao.py
python coletar_dados_tcc.py
```

### Modelos (`aplicacao/modelos/`)

| Arquivo | Descrição |
|---|---|
| `REF01_best.pt` | Modelo de EPIs usado na versão final (YOLO11n, 640 px) |
| `REF02_best.pt` | Ajuste fino do REF01 em 960 px (usado para comparação) |
| `yolo11n.pt` | Detector de pessoas (YOLO11n pré-treinado no COCO, classe *person*) |

---

## Firmware do relógio (`firmware/`)

### Requisitos
- PlatformIO CLI: `pip install platformio`
- Relógio LILYGO T-Watch S3 conectado por USB

### Configuração
Copie `.env.example` para `.env` e preencha a rede Wi-Fi e a senha de OTA.

### Compilar e gravar

```bash
cd firmware
pio run -e twatch-s3 -t upload --upload-port COM3
```

Também é possível usar os scripts em `scripts/`:
- `upload-usb-windows.ps1` (gravação por USB no Windows);
- `upload-usb.sh` (gravação por USB no Linux);
- `upload-ota.sh` (gravação pela rede, por OTA).

### Rotas HTTP do relógio

| Rota | Método | Função |
|---|---|---|
| `/` | GET | Página de teste manual |
| `/health` | GET | Versão do firmware e estado da tela, da vibração e do alerta |
| `/vibrate` | POST | Alerta pontual (vibração e mensagem na tela) |
| `/status` | POST | Estado contínuo: `irregular`, `regular` ou `idle`, com os EPIs ausentes, o intervalo de vibração e o tempo máximo sem atualização |

A aplicação encontra o relógio sozinha, procurando a rota `/health` na rede local.

> As rotas HTTP não têm autenticação. Use o relógio apenas em uma rede controlada.

---

## Dados e privacidade

- **Conjunto de dados:** as imagens usadas no treinamento **não estão neste repositório**. Elas incluem imagens de pessoas e imagens de conjuntos públicos sujeitas a licenças próprias.
- **Dados gerados na execução:** as pastas `registros/` e `coleta_tcc/` (evidências, prints e relatórios) são ignoradas pelo Git.
- **Credenciais:** usuários, senhas, endereços de câmeras e tokens ficam apenas nos arquivos `.env` locais.
