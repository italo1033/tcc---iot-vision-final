"""Núcleo do IoT Vision: detecção de EPI, conformidade, alertas e registros.

Usado pelos três executáveis:
    executar_teste.py      -> vídeo gravado (teste.MOV)
    executar_producao.py   -> câmera ao vivo via RTSP
    coletar_dados_tcc.py   -> testes guiados e coleta de dados para o TCC
"""
from __future__ import annotations

import csv
import json
import os
import platform
import queue
import re
import smtplib
import ssl
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# RTSP sobre TCP evita perda de pacotes UDP em redes Wi-Fi/corporativas.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")

import cv2  # noqa: E402
import requests  # noqa: E402

from smartwatch import MENSAGEM_PADRAO, Relogio  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent

# ============================================================
# 1. CONFIGURAÇÃO
# ============================================================

EPIS_OBRIGATORIOS = ("capacete", "cinta", "luva")

# Nomes alternativos que o modelo pode usar para a mesma classe.
MAPA_CLASSES_EPI = {
    "capacete de seguranca": "capacete",
    "helmet": "capacete",
    "luvas": "luva",
    "luvas de protecao": "luva",
    "glove": "luva",
    "gloves": "luva",
    "cinto": "cinta",
    "cinta de seguranca": "cinta",
    "belt": "cinta",
}

SEM_TRABALHADOR = "SEM TRABALHADOR"
CONFORME = "CONFORME"
NAO_CONFORME = "NAO CONFORME"
INCONCLUSIVO = "INCONCLUSIVO"
ANALISANDO = "ANALISANDO"

CORES_EPI = {"capacete": (0, 215, 255), "cinta": (255, 140, 0), "luva": (255, 0, 255)}

CORES_STATUS = {
    CONFORME: (0, 180, 0),
    NAO_CONFORME: (0, 0, 255),
    INCONCLUSIVO: (0, 165, 255),
    SEM_TRABALHADOR: (200, 200, 200),
    ANALISANDO: (0, 215, 255),
}


def carregar_env(caminho: Path = BASE_DIR / ".env") -> dict[str, str]:
    """Lê KEY=VALOR de um arquivo .env (sem dependências externas)."""
    valores: dict[str, str] = {}
    if not caminho.is_file():
        return valores
    for linha in caminho.read_text(encoding="utf-8-sig").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        valores[chave.strip()] = valor
    return valores


def _bool(valor: str | None, padrao: bool) -> bool:
    if valor is None or valor == "":
        return padrao
    return valor.strip().lower() in {"1", "true", "sim", "s", "yes", "on"}


@dataclass
class Config:
    modelo_epi: Path = BASE_DIR / "best.pt"
    confianca: float = 0.40
    iou: float = 0.45
    imgsz: int = 640
    frames_confirmacao: int = 3

    # Detecção de pessoas (modelo COCO yolo11n.pt) para não alertar com a cena vazia.
    detectar_pessoa: bool = True
    modelo_pessoa: Path = BASE_DIR / "modelos" / "yolo11n.pt"
    confianca_pessoa: float = 0.45
    intervalo_pessoa: int = 3  # roda o detector de pessoas a cada N frames (sem recorte)
    imgsz_pessoa: int = 640
    # Recorta cada pessoa e analisa o recorte (EPIs ficam maiores para o modelo e a
    # conformidade é avaliada por pessoa). Requer o detector de pessoas.
    recorte_pessoa: bool = True
    max_pessoas: int = 4

    # Alertas
    mensagem_relogio: str = MENSAGEM_PADRAO
    relogio_vibrar_s: int = 10  # enquanto irregular, o relógio vibra a cada N segundos
    intervalo_minimo_alerta_s: float = 15.0  # entre dois alertas quaisquer
    repetir_alerta_s: float = 60.0  # lembrete enquanto continuar NÃO CONFORME (0 = não repete)
    notificar_relogio: bool = True
    notificar_telegram: bool = True
    notificar_email: bool = True
    enviar_resumo_final: bool = True

    # Credenciais (vêm do .env)
    rtsp_url: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    email_remetente: str = ""
    email_destinatario: str = ""
    email_senha: str = ""
    email_smtp: str = "smtp.gmail.com"
    email_porta: int = 465

    # Execução
    altura_exibicao: int = 720
    # Frames maiores (ex.: 4K do celular) são reduzidos antes da análise.
    # O YOLO já redimensiona para imgsz, então isso só economiza tempo de desenho/gravação.
    lado_max_processamento: int = 1280
    salvar_video: bool = False
    registrar_frames: bool = True
    pasta_registros: Path = BASE_DIR / "registros"

    @classmethod
    def carregar(cls, **substituicoes) -> "Config":
        env = {**carregar_env(), **{k: v for k, v in os.environ.items() if k.isupper()}}
        cfg = cls()
        modelo = env.get("MODELO_EPI")
        if modelo:
            caminho = Path(modelo)
            cfg.modelo_epi = caminho if caminho.is_absolute() else BASE_DIR / caminho
        cfg.confianca = float(env.get("CONFIANCA", cfg.confianca))
        cfg.iou = float(env.get("IOU", cfg.iou))
        cfg.imgsz = int(env.get("IMGSZ", cfg.imgsz))
        cfg.frames_confirmacao = int(env.get("FRAMES_CONFIRMACAO", cfg.frames_confirmacao))
        cfg.detectar_pessoa = _bool(env.get("DETECTAR_PESSOA"), cfg.detectar_pessoa)
        cfg.recorte_pessoa = _bool(env.get("RECORTE_PESSOA"), cfg.recorte_pessoa)
        cfg.imgsz_pessoa = int(env.get("IMGSZ_PESSOA", cfg.imgsz_pessoa))
        cfg.max_pessoas = int(env.get("MAX_PESSOAS", cfg.max_pessoas))
        cfg.relogio_vibrar_s = int(env.get("RELOGIO_VIBRAR_A_CADA_S", cfg.relogio_vibrar_s))
        cfg.intervalo_minimo_alerta_s = float(env.get("INTERVALO_MINIMO_ALERTA_S", cfg.intervalo_minimo_alerta_s))
        cfg.repetir_alerta_s = float(env.get("REPETIR_ALERTA_S", cfg.repetir_alerta_s))
        cfg.notificar_relogio = _bool(env.get("NOTIFICAR_RELOGIO"), cfg.notificar_relogio)
        cfg.notificar_telegram = _bool(env.get("NOTIFICAR_TELEGRAM"), cfg.notificar_telegram)
        cfg.notificar_email = _bool(env.get("NOTIFICAR_EMAIL"), cfg.notificar_email)
        cfg.mensagem_relogio = env.get("MENSAGEM_RELOGIO", cfg.mensagem_relogio)
        cfg.rtsp_url = env.get("RTSP_URL", "")
        cfg.telegram_token = env.get("TELEGRAM_BOT_TOKEN", "")
        cfg.telegram_chat_id = env.get("TELEGRAM_CHAT_ID", "")
        cfg.email_remetente = env.get("EMAIL_REMETENTE", "")
        cfg.email_destinatario = env.get("EMAIL_DESTINATARIO", cfg.email_remetente)
        cfg.email_senha = env.get("EMAIL_SENHA", "")
        cfg.email_smtp = env.get("EMAIL_SMTP_SERVIDOR", cfg.email_smtp)
        cfg.email_porta = int(env.get("EMAIL_SMTP_PORTA", cfg.email_porta))
        for chave, valor in substituicoes.items():
            setattr(cfg, chave, valor)
        return cfg

    def publica(self) -> dict:
        """Configuração sem credenciais, para relatórios do TCC."""
        dados = asdict(self)
        for chave in ("telegram_token", "telegram_chat_id", "email_senha", "email_remetente", "email_destinatario"):
            dados[chave] = "(configurado)" if dados[chave] else "(vazio)"
        dados["rtsp_url"] = mascarar_url(self.rtsp_url) if self.rtsp_url else "(vazio)"
        return {k: str(v) if isinstance(v, Path) else v for k, v in dados.items()}


def mascarar_url(url: str) -> str:
    """rtsp://usuario:senha@ip -> rtsp://***:***@ip (para logs e prints)."""
    return re.sub(r"//[^/@]*@", "//***:***@", url)


def sem_acentos(texto: object) -> str:
    texto = unicodedata.normalize("NFD", str(texto).strip().lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def nome_epi(nome: object) -> str:
    normalizado = sem_acentos(nome)
    return MAPA_CLASSES_EPI.get(normalizado, normalizado)


def lista(valores, vazio: str = "nenhum") -> str:
    itens = sorted(set(valores))
    return ", ".join(itens) if itens else vazio


def agora() -> str:
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


# ============================================================
# 2. FONTES DE VÍDEO
# ============================================================


class FonteArquivo:
    """Vídeo gravado: lê todos os frames em sequência."""

    tipo = "arquivo"

    def __init__(self, caminho: Path):
        self.caminho = Path(caminho)
        if not self.caminho.is_file():
            raise FileNotFoundError(f"Vídeo não encontrado: {self.caminho}")
        self.cap = cv2.VideoCapture(str(self.caminho))
        if not self.cap.isOpened():
            raise RuntimeError(f"Não foi possível abrir o vídeo: {self.caminho}")
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.total_frames = max(0, int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT)))
        self.largura = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.altura = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.reconexoes = 0
        self.frames_descartados = 0

    def descricao(self) -> dict:
        return {
            "tipo": self.tipo,
            "origem": self.caminho.name,
            "resolucao": f"{self.largura}x{self.altura}",
            "fps_original": round(self.fps, 2),
            "total_frames": self.total_frames,
            "duracao_s": round(self.total_frames / self.fps, 2) if self.fps else None,
        }

    def ler(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def fechar(self):
        self.cap.release()


class FonteRTSP:
    """Câmera ao vivo. Uma thread mantém apenas o frame mais recente,
    evitando atraso acumulado quando a inferência é mais lenta que a câmera."""

    tipo = "rtsp"

    def __init__(self, url: str, log=print, timeout_conexao_s: float = 15.0):
        if not url:
            raise ValueError("RTSP_URL não configurada no arquivo .env")
        self.url = url
        self.log = log
        self.cap = None
        self.fps = 0.0
        self.largura = self.altura = 0
        self.total_frames = 0
        self.reconexoes = 0
        self.frames_recebidos = 0
        self.frames_descartados = 0
        self._frame = None
        self._novo = threading.Event()
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._conectar()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        if not self._novo.wait(timeout_conexao_s):
            raise RuntimeError("A câmera conectou, mas nenhum frame chegou a tempo.")

    def _conectar(self):
        self.log(f"Câmera: conectando em {mascarar_url(self.url)} ...")
        inicio = time.perf_counter()
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("Não foi possível abrir o fluxo RTSP. Verifique rede, IP, usuário e senha.")
        self.cap = cap
        self.fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.largura = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.altura = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.tempo_conexao_s = round(time.perf_counter() - inicio, 2)
        self.log(f"Câmera: conectada em {self.tempo_conexao_s} s | {self.largura}x{self.altura} | {self.fps:.1f} FPS informados")

    def _loop(self):
        falhas = 0
        while not self._parar.is_set():
            ok, frame = self.cap.read()
            if not ok:
                falhas += 1
                if falhas >= 25:
                    self.log("Câmera: fluxo interrompido. Reconectando...")
                    self.cap.release()
                    time.sleep(2)
                    try:
                        self._conectar()
                        self.reconexoes += 1
                    except RuntimeError as erro:
                        self.log(f"Câmera: {erro}")
                        time.sleep(3)
                    falhas = 0
                continue
            falhas = 0
            with self._lock:
                if self._novo.is_set():
                    self.frames_descartados += 1
                self._frame = frame
                self.frames_recebidos += 1
            self._novo.set()

    def descricao(self) -> dict:
        return {
            "tipo": self.tipo,
            "origem": mascarar_url(self.url),
            "resolucao": f"{self.largura}x{self.altura}",
            "fps_original": round(self.fps, 2),
            "tempo_conexao_s": getattr(self, "tempo_conexao_s", None),
        }

    def ler(self):
        if not self._novo.wait(10):
            return None
        with self._lock:
            frame = self._frame
            self._novo.clear()
        return frame

    def fechar(self):
        self._parar.set()
        self._thread.join(timeout=3)
        if self.cap is not None:
            self.cap.release()


# ============================================================
# 3. CANAIS DE NOTIFICAÇÃO
# ============================================================


class Notificador:
    """Envia os alertas em segundo plano para não travar o vídeo."""

    def __init__(self, cfg: Config, log=print):
        self.cfg = cfg
        self.log = log
        self.executor = ThreadPoolExecutor(max_workers=4)
        self._pendentes = []
        self.relogio = Relogio(log=log) if cfg.notificar_relogio else None
        self.telegram_ativo = cfg.notificar_telegram and bool(cfg.telegram_token and cfg.telegram_chat_id)
        self.email_ativo = cfg.notificar_email and bool(cfg.email_remetente and cfg.email_senha)

    def conectar_relogio(self) -> bool:
        if not self.relogio:
            return False
        return self.relogio.localizar() is not None

    def canais(self) -> dict:
        return {
            "relogio": bool(self.relogio and self.relogio.url),
            "telegram": self.telegram_ativo,
            "email": self.email_ativo,
        }

    # -- canais individuais (retornam dict com ok e latência) --------
    def enviar_relogio(self, mensagem: str, detalhe: str) -> dict:
        if not self.relogio:
            return {"ok": False, "erro": "desativado"}
        return self.relogio.vibrar(mensagem, detalhe)

    def enviar_telegram(self, texto: str, frame=None) -> dict:
        if not self.telegram_ativo:
            return {"ok": False, "erro": "desativado"}
        base = f"https://api.telegram.org/bot{self.cfg.telegram_token}"
        inicio = time.perf_counter()
        try:
            if frame is not None:
                ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                resposta = requests.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": self.cfg.telegram_chat_id, "caption": texto[:1000]},
                    files={"photo": ("deteccao_epi.jpg", buffer.tobytes(), "image/jpeg")},
                    timeout=30,
                )
            else:
                resposta = requests.post(
                    f"{base}/sendMessage",
                    json={"chat_id": self.cfg.telegram_chat_id, "text": texto},
                    timeout=20,
                )
            latencia = round((time.perf_counter() - inicio) * 1000, 1)
            dados = resposta.json() if resposta.headers.get("content-type", "").startswith("application/json") else {}
            if resposta.ok and dados.get("ok"):
                return {"ok": True, "latencia_ms": latencia}
            return {"ok": False, "latencia_ms": latencia, "erro": dados.get("description", resposta.text[:200])}
        except requests.RequestException as erro:
            return {"ok": False, "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1), "erro": str(erro)}

    def enviar_email(self, assunto: str, texto: str, frame=None) -> dict:
        if not self.email_ativo:
            return {"ok": False, "erro": "desativado"}
        mensagem = EmailMessage()
        mensagem["From"] = self.cfg.email_remetente
        mensagem["To"] = self.cfg.email_destinatario
        mensagem["Subject"] = assunto
        mensagem.set_content(texto)
        if frame is not None:
            ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                mensagem.add_attachment(buffer.tobytes(), maintype="image", subtype="jpeg", filename="deteccao_epi.jpg")
        inicio = time.perf_counter()
        try:
            with smtplib.SMTP_SSL(self.cfg.email_smtp, self.cfg.email_porta, context=ssl.create_default_context(), timeout=30) as servidor:
                servidor.login(self.cfg.email_remetente, self.cfg.email_senha)
                servidor.send_message(mensagem)
            return {"ok": True, "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1)}
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected):
            # O Gmail recusa o login (ou fecha a conexão) quando a senha não é uma senha de app.
            return {
                "ok": False,
                "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1),
                "erro": "autenticação recusada (o Gmail exige senha de app)",
            }
        except (smtplib.SMTPException, OSError) as erro:
            return {"ok": False, "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1), "erro": str(erro)}

    # -- alerta completo --------------------------------------------
    def alertar(self, ocorrencia: dict, frame, ao_concluir) -> None:
        """Dispara relógio, Telegram e e-mail em paralelo e independentes."""
        faltantes = ocorrencia["faltantes"]
        detalhe = f"Faltando: {faltantes}"
        texto = (
            "🚨 ALERTA DE EPI\n\n"
            f"{self.cfg.mensagem_relogio}.\n"
            f"Status: {ocorrencia['status']}\n"
            f"EPIs detectados: {ocorrencia['detectados']}\n"
            f"EPIs faltantes: {faltantes}\n"
            f"Origem: {ocorrencia['origem']}\n"
            f"Frame: {ocorrencia['frame']}\n"
            f"Inferência: {ocorrencia['inferencia_ms']} ms\n"
            f"Data: {ocorrencia['data_hora']}"
        )
        tarefas = {
            # O relógio recebe o estado contínuo (fica sinalizando até regularizar).
            "relogio": lambda: self.relogio.enviar_status(
                "irregular", self.cfg.mensagem_relogio, faltantes, self.cfg.relogio_vibrar_s
            ) if self.relogio else {"ok": False, "erro": "desativado"},
            "telegram": lambda: self.enviar_telegram(texto, frame),
            "email": lambda: self.enviar_email("🚨 ALERTA DE EPI | IoT Vision", texto, frame),
        }
        for canal, tarefa in tarefas.items():
            def executar(canal=canal, tarefa=tarefa):
                try:
                    resultado = tarefa()
                except Exception as erro:  # um canal nunca derruba o monitoramento
                    resultado = {"ok": False, "erro": f"{type(erro).__name__}: {erro}"}
                ao_concluir(ocorrencia["id"], canal, resultado)
            self._pendentes.append(self.executor.submit(executar))

    def aguardar(self, timeout: float = 60):
        """Espera os alertas em andamento terminarem (sem desligar o notificador)."""
        from concurrent.futures import wait

        wait(self._pendentes, timeout=timeout)
        self._pendentes = [f for f in self._pendentes if not f.done()]

    def encerrar(self):
        self.executor.shutdown(wait=True, cancel_futures=False)


class SincronizadorRelogio:
    """Mantém o relógio espelhando o estado confirmado do detector.

    Enquanto NÃO CONFORME, reenvia o estado a cada `heartbeat_s` (o firmware
    encerra o alerta sozinho se ficar `validade_s` sem notícias). Roda em uma
    thread própria para não atrasar o processamento do vídeo.
    """

    def __init__(self, notificador: Notificador, log=print, heartbeat_s: float = 5.0, validade_s: int = 30):
        self.relogio = notificador.relogio
        self.cfg = notificador.cfg
        self.log = log
        self.heartbeat_s = heartbeat_s
        self.validade_s = validade_s
        self._desejado = ("idle", "")
        self._desejado_desde = time.monotonic()
        self._enviado = None
        self._ultimo_envio = 0.0
        self._evento = threading.Event()
        self._parar = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def atualizar(self, status: str, faltantes: set[str]):
        if status == NAO_CONFORME:
            desejado = ("irregular", lista(faltantes))
        elif status == CONFORME:
            desejado = ("regular", "")
        else:
            desejado = ("idle", "")
        if desejado != self._desejado:
            self._desejado = desejado
            self._desejado_desde = time.monotonic()
            self._evento.set()

    def _loop(self):
        while not self._parar.is_set():
            self._evento.wait(0.5)
            self._evento.clear()
            estado, faltando = self._desejado
            mudou = (estado, faltando) != self._enviado
            # Só a lista de EPIs mudou (continua irregular): espera 2 s de estabilidade
            # para a tela do relógio não ficar piscando com oscilações do detector.
            if (mudou and self._enviado and self._enviado[0] == estado == "irregular"
                    and time.monotonic() - self._desejado_desde < 2.0):
                mudou = False
            renovar = estado == "irregular" and time.monotonic() - self._ultimo_envio >= self.heartbeat_s
            if not (mudou or renovar):
                continue
            if not mudou and self._enviado and self._enviado[0] == estado:
                faltando = self._enviado[1]  # renovação: mantém a lista já exibida
            resultado = self.relogio.enviar_status(estado, self.cfg.mensagem_relogio, faltando,
                                                   self.cfg.relogio_vibrar_s, self.validade_s)
            self._ultimo_envio = time.monotonic()
            if resultado.get("ok"):
                if mudou:
                    self.log(f"   relógio -> {estado.upper()}" + (f" (faltando: {faltando})" if faltando else ""))
                self._enviado = (estado, faltando)
            elif mudou:
                self.log(f"   relógio: falha ao enviar estado ({resultado.get('erro')})")

    def encerrar(self):
        self._parar.set()
        self._thread.join(timeout=2)
        self.relogio.enviar_status("idle", validade_s=self.validade_s)


# ============================================================
# 4. REGISTRO DA SESSÃO (arquivos para auditoria e para o TCC)
# ============================================================


class Registro:
    CAMPOS_OCORRENCIA = [
        "id", "data_hora", "tempo_execucao_s", "origem", "frame", "tempo_video_s",
        "status", "detectados", "faltantes", "pessoas", "inferencia_ms",
        "tempo_ate_confirmar_s", "tipo_alerta", "evidencia",
        "relogio_ok", "relogio_ms", "relogio_erro",
        "telegram_ok", "telegram_ms", "telegram_erro",
        "email_ok", "email_ms", "email_erro",
    ]
    CAMPOS_FRAME = [
        "frame", "tempo_execucao_s", "leitura_ms", "inferencia_ms", "pessoa_ms", "total_ms",
        "pessoas", "deteccoes", "confianca_media", "detectados",
        "status_instantaneo", "status_confirmado",
    ]

    def __init__(self, cfg: Config, modo: str, rotulo: str = ""):
        carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome = f"{modo}_{carimbo}" + (f"_{rotulo}" if rotulo else "")
        self.pasta = cfg.pasta_registros / nome
        (self.pasta / "evidencias").mkdir(parents=True, exist_ok=True)
        (self.pasta / "prints").mkdir(exist_ok=True)
        self.ocorrencias: dict[int, dict] = {}
        self._lock = threading.Lock()
        self._arquivo_frames = None
        self._csv_frames = None
        if cfg.registrar_frames:
            self._arquivo_frames = open(self.pasta / "frames.csv", "w", newline="", encoding="utf-8-sig")
            self._csv_frames = csv.DictWriter(self._arquivo_frames, fieldnames=self.CAMPOS_FRAME, delimiter=";")
            self._csv_frames.writeheader()

    def frame(self, dados: dict):
        if self._csv_frames:
            self._csv_frames.writerow(dados)

    def nova_ocorrencia(self, dados: dict, frame) -> dict:
        with self._lock:
            dados["id"] = len(self.ocorrencias) + 1
            nome = f"ocorrencia_{dados['id']:03d}.jpg"
            cv2.imwrite(str(self.pasta / "evidencias" / nome), frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            dados["evidencia"] = f"evidencias/{nome}"
            self.ocorrencias[dados["id"]] = dados
            self._salvar_ocorrencias()
        return dados

    def resultado_canal(self, id_ocorrencia: int, canal: str, resultado: dict):
        with self._lock:
            ocorrencia = self.ocorrencias.get(id_ocorrencia)
            if ocorrencia is None:
                return
            ocorrencia[f"{canal}_ok"] = resultado.get("ok")
            ocorrencia[f"{canal}_ms"] = resultado.get("latencia_ms")
            ocorrencia[f"{canal}_erro"] = resultado.get("erro", "")
            self._salvar_ocorrencias()

    def _salvar_ocorrencias(self):
        with open(self.pasta / "ocorrencias.csv", "w", newline="", encoding="utf-8-sig") as arquivo:
            escritor = csv.DictWriter(arquivo, fieldnames=self.CAMPOS_OCORRENCIA, delimiter=";", extrasaction="ignore")
            escritor.writeheader()
            escritor.writerows(self.ocorrencias.values())

    def print_tela(self, frame, nome: str = "") -> Path:
        nome = nome or datetime.now().strftime("print_%H%M%S_%f")[:-3]
        caminho = self.pasta / "prints" / f"{nome}.png"
        cv2.imwrite(str(caminho), frame)
        return caminho

    def salvar_json(self, nome: str, dados: dict):
        (self.pasta / nome).write_text(json.dumps(dados, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def fechar(self):
        if self._arquivo_frames:
            self._arquivo_frames.close()


# ============================================================
# 5. DETECTOR
# ============================================================


class Detector:
    def __init__(self, cfg: Config, log=print):
        from ultralytics import YOLO  # import tardio: demora alguns segundos

        self.cfg = cfg
        if not cfg.modelo_epi.is_file():
            raise FileNotFoundError(f"Modelo de EPI não encontrado: {cfg.modelo_epi}")
        log(f"Modelo de EPI: {cfg.modelo_epi.name}")
        inicio = time.perf_counter()
        self.modelo = YOLO(str(cfg.modelo_epi))
        nomes = self.modelo.names
        self.nomes = {int(k): str(v) for k, v in (nomes.items() if isinstance(nomes, dict) else enumerate(nomes))}
        self.classes_ausentes = set(EPIS_OBRIGATORIOS) - {nome_epi(n) for n in self.nomes.values()}
        log("Classes do modelo: " + ", ".join(f"{k}={v}" for k, v in self.nomes.items()))
        if self.classes_ausentes:
            log("ATENÇÃO: o modelo não possui as classes " + lista(self.classes_ausentes) + " -> status INCONCLUSIVO.")

        self.modelo_pessoa = None
        if cfg.detectar_pessoa:
            if cfg.modelo_pessoa.is_file():
                self.modelo_pessoa = YOLO(str(cfg.modelo_pessoa))
                log(f"Detector de pessoas: {cfg.modelo_pessoa.name} (classe COCO 'person')")
            else:
                log(f"Detector de pessoas indisponível ({cfg.modelo_pessoa.name} não encontrado). "
                    "Presença será inferida pelos próprios EPIs.")
        self.tempo_carga_s = round(time.perf_counter() - inicio, 2)
        self._pessoas = 0

        # Aquecimento: a primeira inferência é sempre mais lenta.
        import numpy as np

        vazio = np.zeros((cfg.imgsz, cfg.imgsz, 3), dtype=np.uint8)
        self.modelo.predict(vazio, imgsz=cfg.imgsz, verbose=False)
        if self.modelo_pessoa:
            self.modelo_pessoa.predict(vazio, imgsz=640, classes=[0], verbose=False)

    def info(self) -> dict:
        info = {
            "arquivo": self.cfg.modelo_epi.name,
            "classes": self.nomes,
            "tempo_carga_s": self.tempo_carga_s,
            "detector_pessoas": self.cfg.modelo_pessoa.name if self.modelo_pessoa else None,
        }
        try:
            import hashlib

            info["sha256"] = hashlib.sha256(self.cfg.modelo_epi.read_bytes()).hexdigest()
            ckpt = getattr(self.modelo, "ckpt", None) or {}
            args = ckpt.get("train_args") or {}
            info["treino"] = {k: args.get(k) for k in ("model", "imgsz", "epochs", "batch", "optimizer", "lr0", "seed") if k in args}
            info["data_treino"] = ckpt.get("date")
            info["parametros"] = sum(p.numel() for p in self.modelo.model.parameters())
        except Exception:
            pass
        return info

    def analisar(self, frame, numero_frame: int) -> dict:
        """Analisa um frame e devolve o estado, os EPIs e o frame anotado.

        Modo recorte por pessoa (padrão quando há detector de pessoas): cada pessoa é
        recortada e o recorte é enviado ao modelo de EPI. Em câmeras de vigilância a
        pessoa ocupa poucos pixels; o recorte amplia capacete, cinta e luva antes da
        inferência e permite avaliar a conformidade de cada pessoa separadamente.
        """
        cfg = self.cfg
        if self.modelo_pessoa and cfg.recorte_pessoa:
            return self._analisar_por_pessoa(frame)

        inicio = time.perf_counter()
        resultado = self.modelo.predict(frame, imgsz=cfg.imgsz, conf=cfg.confianca, iou=cfg.iou, verbose=False)[0]
        inferencia_ms = (time.perf_counter() - inicio) * 1000

        pessoa_ms = 0.0
        intervalo = max(1, cfg.intervalo_pessoa)
        if self.modelo_pessoa and numero_frame % intervalo == 1 % intervalo:
            inicio = time.perf_counter()
            pessoas = self.modelo_pessoa.predict(frame, imgsz=cfg.imgsz_pessoa, conf=cfg.confianca_pessoa,
                                                 classes=[0], verbose=False)[0]
            pessoa_ms = (time.perf_counter() - inicio) * 1000
            self._pessoas = 0 if pessoas.boxes is None else len(pessoas.boxes)

        detectados, confiancas = set(), []
        if resultado.boxes is not None and len(resultado.boxes):
            detectados = {nome_epi(self.nomes[int(c)]) for c in resultado.boxes.cls.tolist()}
            confiancas = resultado.boxes.conf.tolist()
        pessoas = self._pessoas if self.modelo_pessoa else (1 if detectados else 0)
        status, faltantes = self.avaliar(detectados, pessoas)
        return {
            "status": status, "detectados": detectados, "faltantes": faltantes, "pessoas": pessoas,
            "confiancas": confiancas, "inferencia_ms": inferencia_ms, "pessoa_ms": pessoa_ms,
            "anotado": resultado.plot(), "por_pessoa": [],
        }

    def _analisar_por_pessoa(self, frame) -> dict:
        cfg = self.cfg
        altura, largura = frame.shape[:2]
        inicio = time.perf_counter()
        resultado_pessoas = self.modelo_pessoa.predict(frame, imgsz=cfg.imgsz_pessoa, conf=cfg.confianca_pessoa,
                                                       classes=[0], verbose=False)[0]
        pessoa_ms = (time.perf_counter() - inicio) * 1000
        caixas = [] if resultado_pessoas.boxes is None else resultado_pessoas.boxes.xyxy.tolist()
        # As maiores (mais próximas) primeiro.
        caixas = sorted(caixas, key=lambda c: (c[2] - c[0]) * (c[3] - c[1]), reverse=True)[: cfg.max_pessoas]

        recortes, origens = [], []
        for x1, y1, x2, y2 in caixas:
            w, h = x2 - x1, y2 - y1
            # Margem: capacete acima da cabeça e luvas com os braços abertos.
            rx1 = int(max(0, x1 - 0.25 * w))
            rx2 = int(min(largura, x2 + 0.25 * w))
            ry1 = int(max(0, y1 - 0.15 * h))
            ry2 = int(min(altura, y2 + 0.05 * h))
            recortes.append(frame[ry1:ry2, rx1:rx2])
            origens.append((rx1, ry1))

        inferencia_ms = 0.0
        resultados = []
        if recortes:
            inicio = time.perf_counter()
            resultados = self.modelo.predict(recortes, imgsz=cfg.imgsz, conf=cfg.confianca, iou=cfg.iou, verbose=False)
            inferencia_ms = (time.perf_counter() - inicio) * 1000

        anotado = frame.copy()
        espessura = max(2, round(altura / 360))
        escala_txt = max(0.5, altura / 1100)
        por_pessoa, detectados_total, faltantes_total, confiancas = [], set(), set(), []
        for indice, ((x1, y1, x2, y2), (ox, oy), resultado) in enumerate(zip(caixas, origens, resultados), start=1):
            detectados = set()
            if resultado.boxes is not None and len(resultado.boxes):
                for (bx1, by1, bx2, by2), classe, conf in zip(resultado.boxes.xyxy.tolist(),
                                                              resultado.boxes.cls.tolist(),
                                                              resultado.boxes.conf.tolist()):
                    nome = nome_epi(self.nomes[int(classe)])
                    detectados.add(nome)
                    confiancas.append(conf)
                    cor = CORES_EPI.get(nome, (255, 255, 255))
                    p1 = (int(bx1 + ox), int(by1 + oy))
                    p2 = (int(bx2 + ox), int(by2 + oy))
                    cv2.rectangle(anotado, p1, p2, cor, espessura)
                    rotulo = f"{nome} {conf:.2f}"
                    (tw, th), _ = cv2.getTextSize(rotulo, cv2.FONT_HERSHEY_SIMPLEX, escala_txt, 1)
                    cv2.rectangle(anotado, (p1[0], p1[1] - th - 6), (p1[0] + tw + 4, p1[1]), cor, -1)
                    cv2.putText(anotado, rotulo, (p1[0] + 2, p1[1] - 4), cv2.FONT_HERSHEY_SIMPLEX,
                                escala_txt, (0, 0, 0), 1, cv2.LINE_AA)
            faltantes = set(EPIS_OBRIGATORIOS) - detectados
            ok = not faltantes
            cor = (0, 200, 0) if ok else (0, 0, 255)
            cv2.rectangle(anotado, (int(x1), int(y1)), (int(x2), int(y2)), cor, espessura)
            rotulo = f"Pessoa {indice}: " + ("OK" if ok else "falta " + lista(faltantes))
            (tw, th), _ = cv2.getTextSize(rotulo, cv2.FONT_HERSHEY_SIMPLEX, escala_txt, 2)
            ty = int(y2) + th + 8 if int(y2) + th + 8 < altura else int(y1) - 8
            tx = max(0, min(int(x1), largura - tw - 8))  # mantém o rótulo dentro da imagem
            cv2.rectangle(anotado, (tx, ty - th - 6), (tx + tw + 6, ty + 4), cor, -1)
            cv2.putText(anotado, rotulo, (tx + 3, ty), cv2.FONT_HERSHEY_SIMPLEX,
                        escala_txt, (255, 255, 255), 2, cv2.LINE_AA)
            por_pessoa.append({"pessoa": indice, "detectados": detectados, "faltantes": faltantes})
            detectados_total |= detectados
            faltantes_total |= faltantes

        if self.classes_ausentes:
            status = INCONCLUSIVO
        elif not caixas:
            status = SEM_TRABALHADOR
        elif faltantes_total:
            status = NAO_CONFORME
        else:
            status = CONFORME
        return {
            "status": status, "detectados": detectados_total,
            "faltantes": faltantes_total if status == NAO_CONFORME else set(),
            "pessoas": len(caixas), "confiancas": confiancas, "inferencia_ms": inferencia_ms,
            "pessoa_ms": pessoa_ms, "anotado": anotado, "por_pessoa": por_pessoa,
        }

    def avaliar(self, detectados: set[str], pessoas: int) -> tuple[str, set[str]]:
        faltantes = set(EPIS_OBRIGATORIOS) - detectados
        if self.classes_ausentes:
            return INCONCLUSIVO, faltantes
        if pessoas == 0:
            return SEM_TRABALHADOR, set()
        if not faltantes:
            return CONFORME, set()
        return NAO_CONFORME, faltantes


# ============================================================
# 6. DESENHO NA TELA
# ============================================================


def desenhar_painel(frame, status, detectados, faltantes, linhas_info: list[str]):
    escala = max(0.6, frame.shape[0] / 900)
    espessura = max(1, round(escala * 2))
    y = int(30 * escala)
    for texto in linhas_info:
        cv2.putText(frame, texto, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * escala, (0, 0, 0), espessura + 2, cv2.LINE_AA)
        cv2.putText(frame, texto, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * escala, (255, 255, 255), espessura, cv2.LINE_AA)
        y += int(26 * escala)

    cor = CORES_STATUS.get(status, (255, 255, 255))
    titulos = {
        CONFORME: "TRABALHADOR: CONFORME",
        NAO_CONFORME: "ANOMALIA: FALTA DE EPI",
        INCONCLUSIVO: "AVALIACAO: INCONCLUSIVA",
        SEM_TRABALHADOR: "SEM TRABALHADOR NA CENA",
        ANALISANDO: "ANALISANDO...",
    }
    if status == NAO_CONFORME:
        detalhe = "Faltando: " + lista(faltantes)
    elif status == CONFORME:
        detalhe = "Todos os EPIs obrigatorios detectados"
    else:
        detalhe = ""
    topo = y + int(8 * escala)
    altura = int(95 * escala)
    largura = min(frame.shape[1] - 20, int(620 * escala))
    sobreposicao = frame.copy()
    cv2.rectangle(sobreposicao, (10, topo), (10 + largura, topo + altura), (0, 0, 0), -1)
    cv2.addWeighted(sobreposicao, 0.65, frame, 0.35, 0, frame)
    cv2.putText(frame, titulos.get(status, status), (22, topo + int(32 * escala)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.85 * escala, cor, espessura + 1, cv2.LINE_AA)
    cv2.putText(frame, "EPIs detectados: " + lista(set(detectados) & set(EPIS_OBRIGATORIOS)),
                (22, topo + int(60 * escala)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * escala, (255, 255, 255), espessura, cv2.LINE_AA)
    if detalhe:
        cv2.putText(frame, detalhe, (22, topo + int(85 * escala)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6 * escala, cor, espessura, cv2.LINE_AA)


# ============================================================
# 7. LAÇO DE MONITORAMENTO
# ============================================================


class Monitoramento:
    """Executa o fluxo completo: fonte -> YOLO -> conformidade -> alertas -> registro."""

    def __init__(self, cfg: Config, fonte, modo: str, detector: Detector | None = None,
                 notificador: Notificador | None = None, rotulo: str = "", log=print):
        self.cfg = cfg
        self.fonte = fonte
        self.modo = modo
        self.log = log
        self.detector = detector or Detector(cfg, log)
        self.notificador = notificador
        self.registro = Registro(cfg, modo, rotulo)
        self.parar = False

    def executar(self, exibir: bool = True, duracao_max_s: float | None = None,
                 titulo_janela: str = "IoT Vision - Detector de EPI", status_esperado: str | None = None) -> dict:
        cfg, fonte, det, reg = self.cfg, self.fonte, self.detector, self.registro
        writer = None
        frames = 0
        tempos = {"leitura": [], "inferencia": [], "pessoa": [], "total": []}
        contagem = {s: 0 for s in (CONFORME, NAO_CONFORME, INCONCLUSIVO, SEM_TRABALHADOR, ANALISANDO)}
        contagem_instantanea = dict(contagem)
        status, candidato, repeticoes = ANALISANDO, None, 0
        inicio_candidato = None
        detectados_exib, faltantes_exib = set(), set()
        ultimo_alerta = -1e9
        primeiro_alerta_s = None
        prints_auto = set()
        transicoes = []
        inicio = time.perf_counter()

        sinc = (SincronizadorRelogio(self.notificador, self.log)
                if self.notificador and self.notificador.relogio else None)
        self.log("Monitoramento iniciado. Teclas: Q = sair | S = salvar print da tela.")
        try:
            while not self.parar:
                t0 = time.perf_counter()
                frame = fonte.ler()
                leitura_ms = (time.perf_counter() - t0) * 1000
                if frame is None:
                    if fonte.tipo == "arquivo":
                        self.log("Fim do vídeo.")
                        break
                    self.log("Câmera sem frames há 10 s; aguardando reconexão...")
                    continue
                frames += 1
                decorrido = time.perf_counter() - inicio
                maior_lado = max(frame.shape[:2])
                if cfg.lado_max_processamento and maior_lado > cfg.lado_max_processamento:
                    escala = cfg.lado_max_processamento / maior_lado
                    frame = cv2.resize(frame, None, fx=escala, fy=escala, interpolation=cv2.INTER_AREA)

                analise = det.analisar(frame, frames)
                instantaneo, detectados, faltantes = analise["status"], analise["detectados"], analise["faltantes"]
                pessoas, confs = analise["pessoas"], analise["confiancas"]
                inf_ms, pes_ms = analise["inferencia_ms"], analise["pessoa_ms"]
                contagem_instantanea[instantaneo] += 1

                # Confirmação temporal: o novo estado precisa se repetir N frames seguidos.
                if instantaneo == candidato:
                    repeticoes += 1
                else:
                    candidato, repeticoes, inicio_candidato = instantaneo, 1, decorrido
                status_anterior = status
                if repeticoes >= cfg.frames_confirmacao and candidato != status:
                    status = candidato
                    transicoes.append({"tempo_s": round(decorrido, 2), "frame": frames, "de": status_anterior, "para": status})
                if repeticoes >= cfg.frames_confirmacao:
                    detectados_exib, faltantes_exib = detectados, faltantes
                contagem[status] += 1
                if sinc:
                    sinc.atualizar(status, faltantes_exib)

                anotado = analise["anotado"]
                fps_proc = frames / decorrido if decorrido > 0 else 0.0
                tempo_video = frames / fonte.fps if fonte.tipo == "arquivo" and fonte.fps else None
                info = [
                    f"Fonte: {'video ' + fonte.descricao()['origem'] if fonte.tipo == 'arquivo' else 'camera ao vivo (RTSP)'}",
                    f"Frame: {frames}" + (f"/{fonte.total_frames}" if fonte.total_frames else ""),
                    f"Inferencia: {inf_ms:.0f} ms | FPS processamento: {fps_proc:.2f}",
                    f"Conf {cfg.confianca:.2f} | IoU {cfg.iou:.2f} | imgsz {cfg.imgsz} | pessoas: {pessoas}",
                ]
                desenhar_painel(anotado, status, detectados_exib, faltantes_exib, info)

                # ---------------- alertas ----------------
                agora_s = decorrido
                entrou = status == NAO_CONFORME and status_anterior != NAO_CONFORME
                persiste = (status == NAO_CONFORME and cfg.repetir_alerta_s > 0
                            and agora_s - ultimo_alerta >= cfg.repetir_alerta_s)
                if (entrou and agora_s - ultimo_alerta >= cfg.intervalo_minimo_alerta_s) or (persiste and not entrou):
                    ultimo_alerta = agora_s
                    primeiro_alerta_s = primeiro_alerta_s if primeiro_alerta_s is not None else agora_s
                    ocorrencia = reg.nova_ocorrencia({
                        "data_hora": agora(),
                        "tempo_execucao_s": round(agora_s, 2),
                        "origem": fonte.tipo,
                        "frame": frames,
                        "tempo_video_s": round(tempo_video, 2) if tempo_video else "",
                        "status": status,
                        "detectados": lista(detectados_exib & set(EPIS_OBRIGATORIOS)),
                        "faltantes": lista(faltantes_exib),
                        "pessoas": pessoas,
                        "inferencia_ms": round(inf_ms, 1),
                        "tempo_ate_confirmar_s": round(agora_s - (inicio_candidato or agora_s), 2),
                        "tipo_alerta": "novo" if entrou else "lembrete",
                    }, anotado)
                    self.log(f"[{ocorrencia['data_hora']}] ANOMALIA #{ocorrencia['id']}: falta de {ocorrencia['faltantes']} "
                             f"(frame {frames}) -> enviando alertas")
                    if self.notificador:
                        self.notificador.alertar(ocorrencia, anotado.copy(), self._ao_concluir_canal)

                # Prints automáticos do primeiro frame de cada estado (para o TCC).
                if status not in prints_auto and status != ANALISANDO and repeticoes >= cfg.frames_confirmacao:
                    prints_auto.add(status)
                    reg.print_tela(anotado, "auto_" + status.replace(" ", "_").lower())

                total_ms = (time.perf_counter() - t0) * 1000
                for chave, valor in (("leitura", leitura_ms), ("inferencia", inf_ms), ("pessoa", pes_ms), ("total", total_ms)):
                    tempos[chave].append(valor)
                reg.frame({
                    "frame": frames, "tempo_execucao_s": round(decorrido, 3),
                    "leitura_ms": round(leitura_ms, 1), "inferencia_ms": round(inf_ms, 1),
                    "pessoa_ms": round(pes_ms, 1), "total_ms": round(total_ms, 1),
                    "pessoas": pessoas, "deteccoes": len(confs),
                    "confianca_media": round(sum(confs) / len(confs), 3) if confs else "",
                    "detectados": lista(detectados, ""), "status_instantaneo": instantaneo, "status_confirmado": status,
                })

                if cfg.salvar_video:
                    if writer is None:
                        altura = min(cfg.altura_exibicao, anotado.shape[0])
                        largura = round(anotado.shape[1] * altura / anotado.shape[0])
                        fps_saida = fonte.fps if fonte.tipo == "arquivo" and fonte.fps else max(1.0, round(fps_proc, 1))
                        writer = cv2.VideoWriter(str(reg.pasta / "video_anotado.mp4"),
                                                 cv2.VideoWriter_fourcc(*"mp4v"), fps_saida, (largura, altura))
                        tamanho_writer = (largura, altura)
                    writer.write(cv2.resize(anotado, tamanho_writer, interpolation=cv2.INTER_AREA))

                if exibir:
                    altura = cfg.altura_exibicao
                    largura = max(1, round(anotado.shape[1] * altura / anotado.shape[0]))
                    cv2.imshow(titulo_janela, cv2.resize(anotado, (largura, altura), interpolation=cv2.INTER_AREA))
                    tecla = cv2.waitKey(1) & 0xFF
                    if tecla in (ord("q"), ord("Q"), 27):
                        self.log("Encerrado pelo usuário (Q).")
                        break
                    if tecla in (ord("s"), ord("S")):
                        self.log(f"Print salvo: {reg.print_tela(anotado)}")

                if duracao_max_s and decorrido >= duracao_max_s:
                    break
        except KeyboardInterrupt:
            self.log("Interrompido (Ctrl+C).")
        finally:
            if sinc:
                sinc.encerrar()
            if writer is not None:
                writer.release()
            if exibir:
                cv2.destroyAllWindows()

        tempo_total = time.perf_counter() - inicio
        if self.notificador:
            self.notificador.aguardar()  # aguarda alertas pendentes
        reg.fechar()

        def media(v):
            return round(sum(v) / len(v), 2) if v else 0.0

        classificados = contagem[CONFORME] + contagem[NAO_CONFORME]
        resumo = {
            "modo": self.modo,
            "data_inicio": datetime.fromtimestamp(time.time() - tempo_total).strftime("%d/%m/%Y %H:%M:%S"),
            "fonte": fonte.descricao(),
            "modelo": det.info(),
            "configuracao": cfg.publica(),
            "plataforma": {"sistema": platform.platform(), "processador": platform.processor()},
            "frames_processados": frames,
            "tempo_total_s": round(tempo_total, 2),
            "fps_medio_processamento": round(frames / tempo_total, 2) if tempo_total else 0,
            "tempo_medio_ms": {k: media(v) for k, v in tempos.items()},
            "tempo_max_inferencia_ms": round(max(tempos["inferencia"]), 1) if tempos["inferencia"] else 0,
            "frames_por_status_confirmado": contagem,
            "frames_por_status_instantaneo": contagem_instantanea,
            "percentual_conforme": round(100 * contagem[CONFORME] / classificados, 2) if classificados else None,
            "transicoes_de_status": transicoes,
            "ocorrencias": len(reg.ocorrencias),
            "primeiro_alerta_s": round(primeiro_alerta_s, 2) if primeiro_alerta_s is not None else None,
            "alertas_por_canal": self._resumo_canais(),
            "reconexoes_camera": fonte.reconexoes,
            "frames_descartados_camera": fonte.frames_descartados,
            "status_esperado": status_esperado,
            "pasta": str(reg.pasta),
        }
        if status_esperado:
            total_validos = frames - contagem[ANALISANDO]
            resumo["acerto_status_esperado_pct"] = (
                round(100 * contagem.get(status_esperado, 0) / total_validos, 2) if total_validos else None
            )
        reg.salvar_json("resumo.json", resumo)
        (reg.pasta / "resumo.txt").write_text(texto_resumo(resumo), encoding="utf-8")
        return resumo

    def _ao_concluir_canal(self, id_ocorrencia, canal, resultado):
        self.registro.resultado_canal(id_ocorrencia, canal, resultado)
        if resultado.get("erro") == "desativado":
            return
        situacao = "ok" if resultado.get("ok") else f"FALHOU ({resultado.get('erro', '')})"
        latencia = f" em {resultado['latencia_ms']} ms" if resultado.get("latencia_ms") is not None else ""
        self.log(f"   alerta #{id_ocorrencia} -> {canal}: {situacao}{latencia}")

    def _resumo_canais(self) -> dict:
        resumo = {}
        for canal in ("relogio", "telegram", "email"):
            enviados = [o for o in self.registro.ocorrencias.values() if o.get(f"{canal}_ok") is not None
                        and o.get(f"{canal}_erro") != "desativado"]
            sucesso = [o for o in enviados if o.get(f"{canal}_ok")]
            latencias = [o[f"{canal}_ms"] for o in sucesso if o.get(f"{canal}_ms") is not None]
            resumo[canal] = {
                "tentativas": len(enviados),
                "sucesso": len(sucesso),
                "latencia_media_ms": round(sum(latencias) / len(latencias), 1) if latencias else None,
            }
        return resumo


def texto_resumo(r: dict) -> str:
    c = r["frames_por_status_confirmado"]
    linhas = [
        "RESUMO DA ANÁLISE DE EPI - IoT Vision",
        "=" * 50,
        f"Modo: {r['modo']} | Fonte: {r['fonte']['origem']} ({r['fonte']['resolucao']})",
        f"Modelo: {r['modelo']['arquivo']}",
        f"Frames processados: {r['frames_processados']}",
        f"Tempo total: {r['tempo_total_s']} s",
        f"Inferência média: {r['tempo_medio_ms']['inferencia']} ms",
        f"FPS médio de processamento: {r['fps_medio_processamento']}",
        f"Frames CONFORME: {c[CONFORME]}",
        f"Frames NÃO CONFORME: {c[NAO_CONFORME]}",
        f"Frames SEM TRABALHADOR: {c[SEM_TRABALHADOR]}",
        f"Frames INCONCLUSIVOS: {c[INCONCLUSIVO]}",
        f"Percentual CONFORME: {r['percentual_conforme']}%",
        f"Ocorrências (alertas): {r['ocorrencias']}",
    ]
    for canal, dados in r["alertas_por_canal"].items():
        if dados["tentativas"]:
            linhas.append(f"  {canal}: {dados['sucesso']}/{dados['tentativas']} ok, latência média {dados['latencia_media_ms']} ms")
    linhas.append(f"Registros em: {r['pasta']}")
    return "\n".join(linhas)


def enviar_resumo(notificador: Notificador, resumo: dict):
    texto = "📊 " + texto_resumo(resumo)
    if notificador.telegram_ativo:
        notificador.enviar_telegram(texto)
    if notificador.email_ativo:
        notificador.enviar_email("📊 Resumo da análise de EPI | IoT Vision", texto)


def iniciar(modo: str, fonte_factory, cfg: Config, exibir: bool = True) -> dict:
    """Sequência de inicialização usada pelos executáveis 1 e 2."""
    print("=" * 60)
    print(f"IoT Vision - {modo.upper()}")
    print("=" * 60)
    print("[1/4] Carregando o modelo...")
    detector = Detector(cfg)
    print("[2/4] Conectando ao relógio...")
    notificador = Notificador(cfg)
    if cfg.notificar_relogio and not notificador.conectar_relogio():
        print("      O monitoramento continua; o relógio será procurado de novo no próximo alerta.")
    print(f"      Telegram: {'ativo' if notificador.telegram_ativo else 'desativado'} | "
          f"E-mail: {'ativo' if notificador.email_ativo else 'desativado'}")
    print("[3/4] Abrindo a fonte de vídeo...")
    fonte = fonte_factory()
    print("      " + ", ".join(f"{k}: {v}" for k, v in fonte.descricao().items()))
    print("[4/4] Iniciando a análise...")
    monitor = Monitoramento(cfg, fonte, modo, detector, notificador)
    try:
        resumo = monitor.executar(exibir=exibir)
    finally:
        fonte.fechar()
    print()
    print(texto_resumo(resumo))
    if cfg.enviar_resumo_final:
        enviar_resumo(notificador, resumo)
    return resumo
