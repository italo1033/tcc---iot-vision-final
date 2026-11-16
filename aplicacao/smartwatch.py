"""Comunicação HTTP com o relógio LILYGO T-Watch S3 (firmware twatch-s3-visual).

O relógio expõe:
    GET  /health   -> estado do firmware (JSON)
    POST /vibrate  -> vibra e mostra o alerta na tela
                      parâmetros opcionais: effect, message, detail

Como o IP do relógio é atribuído por DHCP e pode mudar, a classe Relogio
primeiro testa o endereço salvo em smartwatch.json e, se ele não responder,
procura o relógio nas redes locais do computador. O endereço encontrado é
salvo de volta em smartwatch.json.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import socket
import time
import unicodedata
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

CONFIG_PATH = Path(__file__).with_name("smartwatch.json")
MENSAGEM_PADRAO = "Anomalia identificada: falta de EPI"

# Comando local: não encaminhar pelas configurações de proxy do Windows.
_OPENER = build_opener(ProxyHandler({}))


def _sem_acentos(texto: str) -> str:
    """A fonte do display do relógio não possui acentos."""
    texto = unicodedata.normalize("NFD", str(texto))
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _ler_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _salvar_url(url: str) -> None:
    config = _ler_config()
    if config.get("url") == url:
        return
    config["url"] = url
    config["atualizado_em"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    except OSError:
        pass


def _url_valida(url: str) -> str:
    url = url.strip().rstrip("/")
    partes = urlsplit(url)
    if partes.scheme != "http" or not partes.hostname or partes.path or partes.query:
        raise ValueError(f"URL do relógio inválida: {url!r}")
    return url


def consultar_health(url: str, timeout: float = 2.0) -> dict | None:
    """Retorna o JSON de /health se o endereço for o firmware do relógio."""
    try:
        with _OPENER.open(url + "/health", timeout=timeout) as resposta:
            dados = json.loads(resposta.read(4096))
    except (OSError, URLError, ValueError):
        return None
    if isinstance(dados, dict) and str(dados.get("firmware", "")).startswith("twatch"):
        return dados
    return None


def _redes_locais() -> list[ipaddress.IPv4Network]:
    """Redes /24 das interfaces IPv4 do computador (exceto loopback)."""
    redes = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                redes.add(ipaddress.ip_network(f"{ip}/24", strict=False))
    except OSError:
        pass
    # Descobre a interface usada para sair para a rede (sem enviar pacotes).
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            redes.add(ipaddress.ip_network(f"{s.getsockname()[0]}/24", strict=False))
    except OSError:
        pass
    return sorted(redes)


class Relogio:
    """Localiza o relógio na rede e envia os comandos de alerta."""

    def __init__(self, url: str | None = None, log=print):
        self.log = log
        configurada = url or os.environ.get("SMARTWATCH_URL") or _ler_config().get("url", "")
        self.url_configurada = configurada.strip().rstrip("/")
        self.url: str | None = None
        self.health: dict | None = None
        self._ultima_busca = 0.0

    # ------------------------------------------------------------------
    def localizar(self, varrer_rede: bool = True) -> str | None:
        """Testa o endereço salvo e, se necessário, varre as redes locais."""
        self._ultima_busca = time.monotonic()
        candidatos = [self.url, self.url_configurada]
        for url in dict.fromkeys(c for c in candidatos if c):
            try:
                url = _url_valida(url)
            except ValueError as erro:
                self.log(f"Relógio: {erro}")
                continue
            dados = consultar_health(url)
            if dados:
                return self._encontrado(url, dados)

        if not varrer_rede:
            return None

        redes = _redes_locais()
        hosts_salvos = set()
        if self.url_configurada:
            host = urlsplit(self.url_configurada).hostname
            try:
                redes.append(ipaddress.ip_network(f"{host}/24", strict=False))
                hosts_salvos.add(host)
            except ValueError:
                pass
        redes = sorted(set(redes))
        if not redes:
            self.log("Relógio: nenhuma rede local encontrada para busca.")
            return None

        self.log("Relógio: procurando na rede " + ", ".join(map(str, redes)) + " ...")
        ips = [str(ip) for rede in redes for ip in rede.hosts() if str(ip) not in hosts_salvos]
        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as executor:
            for ip, dados in zip(ips, executor.map(lambda h: consultar_health(f"http://{h}", 1.2), ips)):
                if dados:
                    return self._encontrado(f"http://{ip}", dados)
        self.log("Relógio: não encontrado. Verifique se está ligado e no mesmo Wi-Fi.")
        return None

    def _encontrado(self, url: str, dados: dict) -> str:
        self.url, self.health = url, dados
        _salvar_url(url)
        self.log(
            f"Relógio: conectado em {url} | firmware {dados.get('firmware')} | "
            f"vibração {'ok' if dados.get('haptic_ready') else 'indisponível'} | "
            f"tela {'ok' if dados.get('display_ready') else 'indisponível'}"
        )
        return url

    # ------------------------------------------------------------------
    def vibrar(
        self,
        mensagem: str = MENSAGEM_PADRAO,
        detalhe: str = "",
        efeito: int = 1,
        timeout: float = 4.0,
    ) -> dict:
        """Envia POST /vibrate. Retorna {'ok', 'latencia_ms', 'resposta'|'erro'}."""
        if not self.url:
            # Evita varrer a rede a cada frame quando o relógio está desligado.
            varrer = time.monotonic() - self._ultima_busca > 120
            if not self.localizar(varrer_rede=varrer):
                return {"ok": False, "latencia_ms": None, "erro": "relógio não localizado"}

        corpo = urlencode(
            {
                "effect": int(efeito),
                "message": _sem_acentos(mensagem)[:120],
                "detail": _sem_acentos(detalhe)[:100],
            }
        ).encode("ascii")
        inicio = time.perf_counter()
        try:
            requisicao = Request(self.url + "/vibrate", data=corpo, method="POST")
            with _OPENER.open(requisicao, timeout=timeout) as resposta:
                dados = json.loads(resposta.read(4096))
                latencia = (time.perf_counter() - inicio) * 1000
                ok = resposta.status == 200 and isinstance(dados, dict) and dados.get("ok") is True
                return {"ok": ok, "latencia_ms": round(latencia, 1), "resposta": dados}
        except (OSError, URLError, ValueError) as erro:
            latencia = (time.perf_counter() - inicio) * 1000
            # Na próxima tentativa o endereço será procurado de novo.
            self.url = None
            return {"ok": False, "latencia_ms": round(latencia, 1), "erro": f"{type(erro).__name__}: {erro}"}


    def enviar_status(
        self,
        estado: str,
        mensagem: str = MENSAGEM_PADRAO,
        faltando: str = "",
        vibrar_a_cada_s: int = 10,
        validade_s: int = 30,
        timeout: float = 3.0,
    ) -> dict:
        """Estado contínuo (firmware v4): 'irregular', 'regular' ou 'idle'.

        Em 'irregular' o relógio fica com a tela vermelha mostrando o que falta e
        vibra a cada `vibrar_a_cada_s` segundos até receber 'regular' ou 'idle'.
        Se não receber atualização em `validade_s` segundos, o próprio relógio
        encerra o alerta (proteção caso o detector seja fechado).
        """
        if not self.url:
            varrer = time.monotonic() - self._ultima_busca > 120
            if not self.localizar(varrer_rede=varrer):
                return {"ok": False, "latencia_ms": None, "erro": "relógio não localizado"}
        corpo = urlencode(
            {
                "state": estado,
                "message": _sem_acentos(mensagem)[:120],
                "missing": _sem_acentos(faltando)[:60],
                "interval": int(vibrar_a_cada_s),
                "ttl": int(validade_s),
            }
        ).encode("ascii")
        inicio = time.perf_counter()
        try:
            with _OPENER.open(Request(self.url + "/status", data=corpo, method="POST"), timeout=timeout) as resposta:
                dados = json.loads(resposta.read(4096))
                latencia = round((time.perf_counter() - inicio) * 1000, 1)
                return {"ok": resposta.status == 200 and dados.get("ok") is True, "latencia_ms": latencia, "resposta": dados}
        except (OSError, URLError, ValueError) as erro:
            self.url = None
            return {"ok": False, "latencia_ms": round((time.perf_counter() - inicio) * 1000, 1),
                    "erro": f"{type(erro).__name__}: {erro}"}


def vibrar_ao_encerrar() -> bool:
    """Envia um aviso pontual ao relógio ao encerrar o monitoramento."""
    resultado = Relogio().vibrar("Monitoramento encerrado", "")
    return bool(resultado.get("ok"))


if __name__ == "__main__":
    relogio = Relogio()
    if relogio.localizar():
        print(relogio.vibrar(MENSAGEM_PADRAO, "Teste manual do relogio"))
