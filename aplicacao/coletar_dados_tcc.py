"""EXECUTÁVEL 3 - COLETA DE DADOS PARA O TCC.

Executa testes guiados e grava, em coleta_tcc/, tudo o que é preciso para
escrever as seções 5.8 a 5.11 e o capítulo 6 (Conclusão). Ao final gera
coleta_tcc/RELATORIO_TCC.md, organizado pelas seções do texto.

As etapas podem ser executadas em dias diferentes: cada uma salva seus
próprios arquivos e o relatório junta o que existir.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2

from epi_core import (
    BASE_DIR, CONFORME, NAO_CONFORME, SEM_TRABALHADOR, Config, Detector, FonteArquivo,
    FonteRTSP, Monitoramento, Notificador, mascarar_url,
)
from smartwatch import Relogio

PASTA = BASE_DIR / "coleta_tcc"
# Pasta do repositorio de treinamento (opcional). Pode ser alterada pela variavel REPO_TREINO.
REPO_TREINO = Path(os.environ.get("REPO_TREINO", BASE_DIR.parent / "DETECTOR_IA_YOLO"))
VIDEO_EXP12 = REPO_TREINO / "videos" / "video teste 4.mp4"  # mesmo vídeo da Tabela 10 (EXP12)

# Hash (início do SHA-256) dos pesos conhecidos, para identificar qual modelo está em uso.
MODELOS_CONHECIDOS = {
    "ba93a70c630c9cfd": "REF01 (YOLO11n, 640 px, 150 épocas, early stopping na época 114)",
    "2b39dcb0fef73b1a": "REF02 (YOLO11n, fine-tuning do REF01 em 960 px, 25 épocas)",
    "7534c66ecb8e7410": "EXP12 (YOLO11s, 640 px, etapa inicial)",
}

CENARIOS = [
    ("cena_vazia", "Cena vazia (ninguém na frente da câmera)", SEM_TRABALHADOR),
    ("todos_epis", "Trabalhador com capacete, cinta e luvas", CONFORME),
    ("sem_capacete", "Trabalhador SEM capacete (com cinta e luvas)", NAO_CONFORME),
    ("sem_luva", "Trabalhador SEM luvas (com capacete e cinta)", NAO_CONFORME),
    ("sem_cinta", "Trabalhador SEM cinta (com capacete e luvas)", NAO_CONFORME),
    ("sem_nenhum", "Trabalhador sem nenhum EPI", NAO_CONFORME),
]


# ============================================================
# utilidades
# ============================================================

def perguntar(texto: str, padrao: str = "s") -> bool:
    resposta = input(f"{texto} [{'S/n' if padrao == 's' else 's/N'}]: ").strip().lower()
    return (resposta or padrao) in {"s", "sim", "y"}


def numero(texto: str, padrao: float) -> float:
    resposta = input(f"{texto} [{padrao:g}]: ").strip().replace(",", ".")
    try:
        return float(resposta) if resposta else padrao
    except ValueError:
        return padrao


def salvar(nome: str, dados) -> Path:
    PASTA.mkdir(exist_ok=True)
    caminho = PASTA / nome
    caminho.write_text(json.dumps(dados, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"   -> salvo em {caminho}")
    return caminho


def ler(nome: str):
    caminho = PASTA / nome
    if caminho.is_file():
        return json.loads(caminho.read_text(encoding="utf-8"))
    return None


def sha16(caminho: Path) -> str:
    return hashlib.sha256(caminho.read_bytes()).hexdigest()[:16]


def powershell(comando: str) -> str:
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", comando], capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def config_para_coleta(**extras) -> Config:
    # Cada execução da coleta grava seus registros dentro de coleta_tcc/execucoes.
    return Config.carregar(pasta_registros=PASTA / "execucoes", **extras)


_detector_cache: dict[str, Detector] = {}


def detector(cfg: Config) -> Detector:
    chave = f"{cfg.modelo_epi}|{cfg.imgsz}"
    if chave not in _detector_cache:
        _detector_cache[chave] = Detector(cfg)
    return _detector_cache[chave]


# ============================================================
# ETAPA 1 - ambiente
# ============================================================

def etapa_ambiente():
    print("\n[1] Coletando informações do ambiente, modelo e conectividade...")
    cfg = config_para_coleta()
    import numpy
    import torch
    import ultralytics

    ambiente = {
        "data": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "computador": {
            "sistema": platform.platform(),
            "processador": powershell("(Get-CimInstance Win32_Processor).Name") or platform.processor(),
            "nucleos_logicos": __import__("os").cpu_count(),
            "memoria_ram_gb": powershell("[math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory/1GB,1)"),
            "placas_de_video": powershell("(Get-CimInstance Win32_VideoController).Name -join '; '"),
            "inferencia_em": "GPU (CUDA)" if torch.cuda.is_available() else "CPU (PyTorch sem CUDA)",
        },
        "software": {
            "python": sys.version.split()[0],
            "ultralytics": ultralytics.__version__,
            "pytorch": torch.__version__,
            "opencv": cv2.__version__,
            "numpy": numpy.__version__,
        },
        "configuracao": cfg.publica(),
    }

    det = detector(cfg)
    info = det.info()
    info["identificacao"] = MODELOS_CONHECIDOS.get(info.get("sha256", "")[:16], "modelo não catalogado")
    ambiente["modelo"] = info
    ambiente["modelos_disponiveis"] = {
        p.name: MODELOS_CONHECIDOS.get(sha16(p), "?") for p in [BASE_DIR / "best.pt", *sorted((BASE_DIR / "modelos").glob("*.pt"))]
    }
    print(f"   Modelo em uso: {cfg.modelo_epi.name} = {info['identificacao']}")

    # Câmera
    camera = {"configurada": bool(cfg.rtsp_url), "url": mascarar_url(cfg.rtsp_url)}
    if cfg.rtsp_url:
        try:
            fonte = FonteRTSP(cfg.rtsp_url)
            amostras, inicio = 0, time.perf_counter()
            while time.perf_counter() - inicio < 5:
                if fonte.ler() is not None:
                    amostras += 1
            camera.update(fonte.descricao())
            camera["frames_recebidos_em_5s"] = fonte.frames_recebidos
            camera["fps_recebido_medido"] = round(fonte.frames_recebidos / 5, 2)
            fonte.fechar()
            camera["ok"] = True
        except (RuntimeError, ValueError) as erro:
            camera.update(ok=False, erro=str(erro))
    ambiente["camera"] = camera
    print(f"   Câmera: {'ok' if camera.get('ok') else 'FALHOU'}")

    # Relógio
    relogio = Relogio()
    inicio = time.perf_counter()
    url = relogio.localizar()
    ambiente["relogio"] = {
        "encontrado": bool(url), "url": url, "tempo_localizacao_s": round(time.perf_counter() - inicio, 2),
        "health": relogio.health, "modelo": "LILYGO T-Watch S3 (ESP32-S3, DRV2605, tela ST7789V 240x240)",
    }

    notificador = Notificador(cfg)
    ambiente["canais"] = {"telegram_configurado": notificador.telegram_ativo, "email_configurado": notificador.email_ativo}
    ambiente["rede"] = {"computador": socket.gethostbyname(socket.gethostname())}
    salvar("1_ambiente.json", ambiente)


# ============================================================
# ETAPA 2 - vídeo gravado
# ============================================================

def etapa_video():
    print("\n[2] Teste completo com o vídeo gravado teste.MOV")
    alertas = perguntar("Enviar alertas reais (relógio, Telegram e e-mail) durante o teste?", "s")
    cfg = config_para_coleta(salvar_video=True, notificar_relogio=alertas,
                             notificar_telegram=alertas, notificar_email=alertas, enviar_resumo_final=False)
    notificador = Notificador(cfg)
    if alertas:
        notificador.conectar_relogio()
    monitor = Monitoramento(cfg, FonteArquivo(BASE_DIR / "teste.MOV"), "coleta_video", detector(cfg), notificador, "teste_MOV")
    resumo = monitor.executar(exibir=True, titulo_janela="Coleta TCC - video gravado")
    monitor.fonte.fechar()
    salvar("2_video_gravado.json", resumo)


# ============================================================
# ETAPA 3 - comparação de modelos e com o EXP12
# ============================================================

def etapa_comparacao():
    print("\n[3] Comparação REF01 x REF02 nos vídeos (sem janela e sem alertas).")
    videos = [BASE_DIR / "teste.MOV"]
    if VIDEO_EXP12.is_file():
        videos.append(VIDEO_EXP12)
        print(f"   Incluindo '{VIDEO_EXP12.name}' (mesmo vídeo usado com o EXP12 na Tabela 10).")
    modelos = [BASE_DIR / "modelos" / "REF01_best.pt", BASE_DIR / "modelos" / "REF02_best.pt"]
    print("   Isso pode levar de 10 a 40 minutos. Deixe o computador ligado.")
    if not perguntar("Continuar?", "s"):
        return
    resultados = []
    for video in videos:
        for modelo in modelos:
            for imgsz in ([640] if "REF01" in modelo.name else [640, 960]):
                print(f"   -> {video.name} | {modelo.name} | imgsz {imgsz}")
                cfg = config_para_coleta(modelo_epi=modelo, imgsz=imgsz, notificar_relogio=False,
                                         notificar_telegram=False, notificar_email=False, registrar_frames=True)
                monitor = Monitoramento(cfg, FonteArquivo(video), "coleta_comparacao", detector(cfg), None,
                                        f"{modelo.stem}_{imgsz}_{video.stem.replace(' ', '_')}")
                resumo = monitor.executar(exibir=False)
                monitor.fonte.fechar()
                resultados.append({
                    "video": video.name, "modelo": modelo.stem, "imgsz": imgsz,
                    "frames": resumo["frames_processados"], "tempo_total_s": resumo["tempo_total_s"],
                    "fps": resumo["fps_medio_processamento"], "tempos_ms": resumo["tempo_medio_ms"],
                    "status": resumo["frames_por_status_confirmado"], "status_instantaneo": resumo["frames_por_status_instantaneo"],
                    "percentual_conforme": resumo["percentual_conforme"], "ocorrencias": resumo["ocorrencias"],
                    "pasta": resumo["pasta"],
                })
                salvar("3_comparacao_modelos.json", resultados)


# ============================================================
# ETAPA 4 - latência dos canais
# ============================================================

def etapa_latencia():
    print("\n[4] Latência dos canais de alerta (envia mensagens REAIS).")
    cfg = config_para_coleta()
    notificador = Notificador(cfg)
    repeticoes = int(numero("Quantas vibrações de teste no relógio?", 5))
    dados = {"relogio": [], "telegram": [], "email": []}
    if notificador.conectar_relogio():
        for i in range(repeticoes):
            r = notificador.enviar_relogio(cfg.mensagem_relogio, f"Teste de latencia {i + 1}/{repeticoes}")
            print(f"   relógio {i + 1}: {r.get('ok')} {r.get('latencia_ms')} ms")
            dados["relogio"].append(r)
            time.sleep(9)  # o alerta fica 8 s na tela
        dados["percepcao_relogio"] = input("   Vocês SENTIRAM a vibração e VIRAM a mensagem na tela? Descreva: ").strip()
    if notificador.telegram_ativo and perguntar("Enviar 1 mensagem de teste ao Telegram?", "s"):
        frame = None
        r = notificador.enviar_telegram("🧪 Teste de latência - IoT Vision", frame)
        print(f"   telegram: {r}")
        dados["telegram"].append(r)
    if notificador.email_ativo and perguntar("Enviar 1 e-mail de teste?", "s"):
        r = notificador.enviar_email("🧪 Teste de latência - IoT Vision", "Teste de envio do IoT Vision.")
        print(f"   e-mail: {r}")
        dados["email"].append(r)
    for canal in ("relogio", "telegram", "email"):
        ok = [x["latencia_ms"] for x in dados[canal] if x.get("ok") and x.get("latencia_ms") is not None]
        dados[f"{canal}_resumo"] = {
            "tentativas": len(dados[canal]), "sucesso": len(ok),
            "media_ms": round(sum(ok) / len(ok), 1) if ok else None,
            "min_ms": min(ok) if ok else None, "max_ms": max(ok) if ok else None,
        }
    salvar("4_latencia_canais.json", dados)


# ============================================================
# ETAPA 5 - cenários guiados na câmera
# ============================================================

def etapa_cenarios():
    print("\n[5] Cenários guiados na câmera ao vivo.")
    print("    Para cada cenário, posicione-se na frente da câmera e pressione ENTER.")
    duracao = numero("Duração de cada cenário em segundos", 30)
    usar_relogio = perguntar("Acionar o RELÓGIO nas anomalias durante os cenários?", "s")
    usar_remotos = perguntar("Enviar também Telegram e e-mail nos cenários?", "n")
    cfg = config_para_coleta(notificar_relogio=usar_relogio, notificar_telegram=usar_remotos,
                             notificar_email=usar_remotos, salvar_video=True, enviar_resumo_final=False,
                             repetir_alerta_s=0)
    det = detector(cfg)
    notificador = Notificador(cfg)
    if usar_relogio:
        notificador.conectar_relogio()
    fonte = FonteRTSP(cfg.rtsp_url)
    resultados = ler("5_cenarios.json") or []
    feitos = {r["id"] for r in resultados}
    cenarios = list(CENARIOS)
    try:
        while True:
            pendentes = [c for c in cenarios if c[0] not in feitos]
            if not pendentes:
                descricao = input("\nOutro cenário? Descreva (ex.: 'luva a 5 metros') ou ENTER para terminar: ").strip()
                if not descricao:
                    break
                esperado = input("Status esperado (1=CONFORME, 2=NAO CONFORME, 3=SEM TRABALHADOR, ENTER=sem expectativa): ").strip()
                esperado = {"1": CONFORME, "2": NAO_CONFORME, "3": SEM_TRABALHADOR}.get(esperado)
                cenarios.append((f"extra_{len(cenarios) + 1}", descricao, esperado))
                continue
            ident, descricao, esperado = pendentes[0]
            print(f"\n=== Cenário: {descricao} | esperado: {esperado} ===")
            acao = input("ENTER = iniciar | P = pular | S = sair: ").strip().lower()
            if acao == "s":
                break
            if acao == "p":
                feitos.add(ident)
                continue
            for i in range(5, 0, -1):
                print(f"   começando em {i}...", end="\r")
                time.sleep(1)
            monitor = Monitoramento(cfg, fonte, "coleta_cenario", det, notificador, ident)
            resumo = monitor.executar(exibir=True, duracao_max_s=duracao,
                                      titulo_janela=f"Cenario: {descricao}", status_esperado=esperado)
            c = resumo["frames_por_status_confirmado"]
            predominante = max((s for s in c if s != "ANALISANDO"), key=lambda s: c[s])
            obs = input("   Observação sobre este cenário (ENTER para nenhuma): ").strip()
            resultados.append({
                "id": ident, "descricao": descricao, "esperado": esperado, "predominante": predominante,
                "acerto_pct": resumo.get("acerto_status_esperado_pct"), "frames": resumo["frames_processados"],
                "fps": resumo["fps_medio_processamento"], "status": c,
                "ocorrencias": resumo["ocorrencias"], "primeiro_alerta_s": resumo["primeiro_alerta_s"],
                "alertas_por_canal": resumo["alertas_por_canal"], "observacao": obs, "pasta": resumo["pasta"],
            })
            feitos.add(ident)
            print(f"   resultado: predominante {predominante} | acerto {resumo.get('acerto_status_esperado_pct')}%")
            salvar("5_cenarios.json", resultados)
    finally:
        fonte.fechar()


# ============================================================
# ETAPA 6 - estabilidade (operação contínua)
# ============================================================

def etapa_estabilidade():
    print("\n[6] Operação contínua na câmera ao vivo (como em produção).")
    minutos = numero("Duração em minutos", 5)
    alertas = perguntar("Com alertas reais (relógio, Telegram e e-mail)?", "s")
    cfg = config_para_coleta(notificar_relogio=alertas, notificar_telegram=alertas,
                             notificar_email=alertas, enviar_resumo_final=False)
    notificador = Notificador(cfg)
    if alertas:
        notificador.conectar_relogio()
    fonte = FonteRTSP(cfg.rtsp_url)
    monitor = Monitoramento(cfg, fonte, "coleta_estabilidade", detector(cfg), notificador, f"{minutos:g}min")
    resumo = monitor.executar(exibir=True, duracao_max_s=minutos * 60, titulo_janela="Coleta TCC - operacao continua")
    resumo["frames_recebidos_camera"] = fonte.frames_recebidos
    resumo["percentual_frames_analisados"] = (
        round(100 * resumo["frames_processados"] / fonte.frames_recebidos, 1) if fonte.frames_recebidos else None
    )
    fonte.fechar()
    salvar("6_estabilidade.json", resumo)


# ============================================================
# ETAPA 7 - relatório
# ============================================================

def _tabela(cabecalho: list[str], linhas: list[list]) -> str:
    texto = "| " + " | ".join(cabecalho) + " |\n|" + "---|" * len(cabecalho) + "\n"
    for linha in linhas:
        texto += "| " + " | ".join("" if v is None else str(v) for v in linha) + " |\n"
    return texto


def _resumo_execucao(r: dict) -> str:
    c = r["frames_por_status_confirmado"]
    t = r["tempo_medio_ms"]
    return _tabela(["Parâmetro", "Resultado"], [
        ["Fonte", f"{r['fonte'].get('origem')} ({r['fonte'].get('resolucao')}, {r['fonte'].get('fps_original')} FPS)"],
        ["Duração do vídeo", f"{r['fonte'].get('duracao_s')} s" if r["fonte"].get("duracao_s") else "-"],
        ["Frames processados", r["frames_processados"]],
        ["Tempo total de processamento", f"{r['tempo_total_s']} s"],
        ["Leitura média do frame", f"{t['leitura']} ms"],
        ["Inferência média (EPI)", f"{t['inferencia']} ms"],
        ["Inferência média (pessoa)", f"{t['pessoa']} ms"],
        ["Tempo médio total por frame", f"{t['total']} ms"],
        ["FPS médio de processamento", r["fps_medio_processamento"]],
        ["Frames CONFORME", c["CONFORME"]],
        ["Frames NÃO CONFORME", c["NAO CONFORME"]],
        ["Frames SEM TRABALHADOR", c["SEM TRABALHADOR"]],
        ["Percentual CONFORME", f"{r['percentual_conforme']}%"],
        ["Ocorrências (alertas gerados)", r["ocorrencias"]],
        ["Primeiro alerta após", f"{r['primeiro_alerta_s']} s" if r["primeiro_alerta_s"] is not None else "-"],
    ])


def etapa_relatorio():
    print("\n[7] Gerando RELATORIO_TCC.md ...")
    amb, video, comp = ler("1_ambiente.json"), ler("2_video_gravado.json"), ler("3_comparacao_modelos.json")
    lat, cen, est = ler("4_latencia_canais.json"), ler("5_cenarios.json"), ler("6_estabilidade.json")
    faltando = [n for n, d in [("1 ambiente", amb), ("2 vídeo gravado", video), ("3 comparação", comp),
                               ("4 latência", lat), ("5 cenários", cen), ("6 estabilidade", est)] if not d]
    md = [f"# Dados coletados para o TCC - IoT Vision\n\nGerado em {datetime.now():%d/%m/%Y %H:%M}.\n"]
    if faltando:
        md.append("> **Etapas ainda não executadas:** " + ", ".join(faltando) + "\n")

    # ---------------- 5.8.1 ----------------
    md.append("\n## 5.8.1 Funcionamento do fluxo da aplicação\n")
    if amb:
        pc, sw, m = amb["computador"], amb["software"], amb["modelo"]
        md.append(_tabela(["Item", "Valor"], [
            ["Processador", pc["processador"]], ["Memória RAM", f"{pc['memoria_ram_gb']} GB"],
            ["Placas de vídeo", pc["placas_de_video"]], ["Inferência executada em", pc["inferencia_em"]],
            ["Sistema", pc["sistema"]], ["Python / Ultralytics / PyTorch / OpenCV",
                                         f"{sw['python']} / {sw['ultralytics']} / {sw['pytorch']} / {sw['opencv']}"],
            ["Modelo de EPI", f"{m['arquivo']} = {m['identificacao']}"],
            ["Parâmetros do modelo", m.get("parametros")], ["Treino registrado no arquivo", ", ".join(f"{k}={v}" for k, v in (m.get("treino") or {}).items() if k != "model")],
            ["Detector de pessoas", m.get("detector_pessoas")],
            ["Confiança / IoU / imgsz", f"{amb['configuracao']['confianca']} / {amb['configuracao']['iou']} / {amb['configuracao']['imgsz']}"],
            ["Frames para confirmar estado", amb["configuracao"]["frames_confirmacao"]],
            ["Intervalo mínimo / repetição de alerta", f"{amb['configuracao']['intervalo_minimo_alerta_s']} s / {amb['configuracao']['repetir_alerta_s']} s"],
            ["Câmera", f"{amb['camera'].get('resolucao')}; {amb['camera'].get('fps_original')} FPS informados; "
                       f"{amb['camera'].get('fps_recebido_medido')} FPS recebidos; conexão em {amb['camera'].get('tempo_conexao_s')} s"],
            ["Relógio", f"{'encontrado' if amb['relogio']['encontrado'] else 'NÃO encontrado'} em "
                        f"{amb['relogio']['tempo_localizacao_s']} s; firmware {(amb['relogio'].get('health') or {}).get('firmware')}"],
            ["Telegram / E-mail configurados", f"{amb['canais']['telegram_configurado']} / {amb['canais']['email_configurado']}"],
        ]))
        if "REF02" in m["identificacao"]:
            md.append("\n> ⚠️ O `best.pt` em uso é o **REF02**, mas a seção 5.7 do TCC selecionou o **REF01**. "
                      "Use `MODELO_EPI=modelos/REF01_best.pt` no `.env` ou ajuste o texto (ver etapa 3).\n")
    md.append(
        "\nFluxo implementado (para descrever no texto): (1) carregamento do `best.pt` e do detector de pessoas; "
        "(2) localização automática do relógio na rede (`/health`); (3) abertura da câmera RTSP sobre TCP com uma "
        "thread que mantém só o frame mais recente; (4) detecção de pessoas (yolo11n, COCO) e recorte de cada pessoa "
        "com margem; (5) inferência do modelo de EPI em cada recorte (EPIs ampliados); (6) regra de conformidade por "
        "pessoa: sem pessoa = SEM TRABALHADOR, todas com os 3 EPIs = CONFORME, alguma sem EPI = NÃO CONFORME; "
        "(7) confirmação após N frames seguidos; (8) o relógio espelha o estado: em NÃO CONFORME fica com tela "
        "vermelha mostrando o que falta e vibra a cada 10 s até regularizar (tela verde ao voltar a CONFORME); "
        "(9) na entrada em NÃO CONFORME, registro da ocorrência (CSV + JPEG) e envio por Telegram e e-mail, com "
        "lembrete a cada 60 s; (10) resumo final.\n")

    # ---------------- 5.8.2 ----------------
    md.append("\n## 5.8.2 Avaliação prática da solução\n")
    if video:
        md.append("\n### Vídeo gravado (teste.MOV)\n\n" + _resumo_execucao(video))
        canais = video["alertas_por_canal"]
        md.append("\n" + _tabela(["Canal", "Tentativas", "Sucesso", "Latência média (ms)"],
                                 [[k, v["tentativas"], v["sucesso"], v["latencia_media_ms"]] for k, v in canais.items()]))
        md.append(f"\nPasta com evidências, prints e vídeo anotado: `{video['pasta']}`\n")
    if cen:
        md.append("\n### Cenários controlados na câmera ao vivo\n\n")
        md.append(_tabela(
            ["Cenário", "Esperado", "Obtido (predominante)", "Acerto (% frames)", "Alertas", "1º alerta (s)", "FPS", "Observação"],
            [[c["descricao"], c["esperado"], c["predominante"], c["acerto_pct"], c["ocorrencias"],
              c["primeiro_alerta_s"], c["fps"], c["observacao"]] for c in cen]))
        corretos = sum(1 for c in cen if c["esperado"] and c["esperado"] == c["predominante"])
        com_esperado = sum(1 for c in cen if c["esperado"])
        md.append(f"\nCenários com classificação predominante correta: **{corretos}/{com_esperado}**.\n")
    if est:
        md.append("\n### Operação contínua na câmera\n\n" + _resumo_execucao(est))
        md.append(_tabela(["Indicador", "Valor"], [
            ["Frames recebidos da câmera", est.get("frames_recebidos_camera")],
            ["Frames efetivamente analisados", f"{est['frames_processados']} ({est.get('percentual_frames_analisados')}%)"],
            ["Reconexões RTSP", est["reconexoes_camera"]],
        ]))
    if lat:
        md.append("\n### Latência dos canais de alerta\n\n")
        md.append(_tabela(["Canal", "Tentativas", "Sucesso", "Média (ms)", "Mín (ms)", "Máx (ms)"], [
            [k, lat[f"{k}_resumo"]["tentativas"], lat[f"{k}_resumo"]["sucesso"], lat[f"{k}_resumo"]["media_ms"],
             lat[f"{k}_resumo"]["min_ms"], lat[f"{k}_resumo"]["max_ms"]] for k in ("relogio", "telegram", "email")]))
        if lat.get("percepcao_relogio"):
            md.append(f"\nPercepção relatada no relógio: _{lat['percepcao_relogio']}_\n")

    # ---------------- 5.9 / 5.10 ----------------
    md.append("\n## 5.9 Resultado final da solução / 5.10 Discussão\n")
    if comp:
        md.append("\n### Comparação entre modelos nos vídeos\n\n")
        md.append(_tabela(["Vídeo", "Modelo", "imgsz", "Frames", "Inferência (ms)", "FPS", "% conforme", "Alertas"],
                          [[c["video"], c["modelo"], c["imgsz"], c["frames"], c["tempos_ms"]["inferencia"], c["fps"],
                            c["percentual_conforme"], c["ocorrencias"]] for c in comp]))
        md.append("\nReferência da etapa inicial (Tabela 10, EXP12/YOLO11s em `video teste 4.mp4`): "
                  "inferência média 136,39 ms e 4,64 FPS.\n")
    checklist = [
        ["Carregar o modelo treinado", "✅" if amb else "?"],
        ["Capturar a câmera ao vivo (RTSP)", "✅" if (amb or {}).get("camera", {}).get("ok") else "❌/?"],
        ["Detectar EPIs e avaliar conformidade", "✅" if video or cen else "?"],
        ["Registrar ocorrências (CSV + imagem)", "✅" if (video or {}).get("ocorrencias") or cen else "?"],
        ["Alerta no relógio (vibração + mensagem)", "✅" if (lat or {}).get("relogio_resumo", {}).get("sucesso") else "?"],
        ["Notificação via Telegram", "✅" if (lat or {}).get("telegram_resumo", {}).get("sucesso") else "?"],
        ["Notificação via e-mail", "✅" if (lat or {}).get("email_resumo", {}).get("sucesso") else "❌/?"],
    ]
    md.append("\n### Atendimento aos objetivos (evidência coletada)\n\n" + _tabela(["Objetivo", "Situação"], checklist))

    # ---------------- 5.11 ----------------
    md.append("\n## 5.11 Limitações observadas (detectadas automaticamente)\n\n")
    limitacoes = []
    if cen:
        for c in cen:
            if c["esperado"] and c["acerto_pct"] is not None and c["acerto_pct"] < 80:
                limitacoes.append(f"Cenário '{c['descricao']}': só {c['acerto_pct']}% dos frames no estado esperado.")
    if est and est.get("percentual_frames_analisados") is not None and est["percentual_frames_analisados"] < 50:
        limitacoes.append(f"Apenas {est['percentual_frames_analisados']}% dos frames da câmera foram analisados "
                          f"({est['fps_medio_processamento']} FPS contra {est['fonte'].get('fps_original')} FPS da câmera).")
    if lat and not lat.get("email_resumo", {}).get("sucesso"):
        limitacoes.append("E-mail não confirmado (verificar senha de app do Gmail).")
    if amb and amb["computador"]["inferencia_em"].startswith("CPU"):
        limitacoes.append("Inferência em CPU: a placa NVIDIA não foi usada pelo PyTorch instalado (versão sem CUDA).")
    if Config.carregar().recorte_pessoa:
        limitacoes.append("Conformidade avaliada por pessoa (recorte): pessoa parcialmente visível na borda da imagem "
                          "pode gerar NÃO CONFORME falso; pessoa muito próxima/vista de cima reduz a detecção da cinta.")
    else:
        limitacoes.append("Regra de conformidade por cena: com várias pessoas, EPIs de pessoas diferentes podem se somar.")
    md.append("\n".join(f"- {l}" for l in limitacoes) + "\n")

    md.append("\n## Prints e evidências para as figuras\n\n")
    for png in sorted((PASTA / "execucoes").glob("*/prints/*.png")) + sorted((PASTA / "execucoes").glob("*/evidencias/*.jpg"))[:20]:
        md.append(f"- `{png.relative_to(PASTA)}`\n")

    caminho = PASTA / "RELATORIO_TCC.md"
    caminho.write_text("".join(md), encoding="utf-8")
    print(f"   -> {caminho}")


# ============================================================
# MENU
# ============================================================

ETAPAS = {
    "1": ("Ambiente, modelo e conectividade (automático, ~1 min)", etapa_ambiente),
    "2": ("Teste completo com o vídeo gravado teste.MOV", etapa_video),
    "3": ("Comparação REF01 x REF02 nos vídeos (demorado, sem janela)", etapa_comparacao),
    "4": ("Latência dos canais: relógio, Telegram e e-mail", etapa_latencia),
    "5": ("Cenários guiados na câmera ao vivo (precisa de uma pessoa com os EPIs)", etapa_cenarios),
    "6": ("Operação contínua na câmera ao vivo", etapa_estabilidade),
    "7": ("Gerar o relatório RELATORIO_TCC.md", etapa_relatorio),
}

if __name__ == "__main__":
    print("=" * 64)
    print(" IoT Vision - COLETA DE DADOS PARA O TCC (seções 5.8 a 6.3)")
    print("=" * 64)
    while True:
        print()
        for chave, (titulo, _) in ETAPAS.items():
            feito = {"1": "1_ambiente.json", "2": "2_video_gravado.json", "3": "3_comparacao_modelos.json",
                     "4": "4_latencia_canais.json", "5": "5_cenarios.json", "6": "6_estabilidade.json",
                     "7": "RELATORIO_TCC.md"}[chave]
            marca = "✔" if (PASTA / feito).exists() else " "
            print(f" [{chave}] {marca} {titulo}")
        print(" [A]   Executar todas em sequência")
        print(" [0]   Sair")
        opcao = input("\nEscolha: ").strip().upper()
        if opcao == "0":
            break
        escolhidas = list(ETAPAS) if opcao == "A" else [opcao] if opcao in ETAPAS else []
        for chave in escolhidas:
            try:
                ETAPAS[chave][1]()
            except KeyboardInterrupt:
                print("\nEtapa interrompida.")
            except Exception as erro:
                print(f"\nERRO na etapa {chave}: {type(erro).__name__}: {erro}")
        if escolhidas and "7" not in escolhidas:
            etapa_relatorio()
