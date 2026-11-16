"""EXECUTÁVEL 2 - PRODUÇÃO: monitora a câmera ao vivo via RTSP.

Ao iniciar: carrega best.pt, localiza o relógio na rede, conecta à câmera
(RTSP_URL do arquivo .env) e analisa continuamente. Em caso de anomalia
(falta de EPI) o relógio vibra e mostra "Anomalia identificada: falta de EPI",
e o alerta também segue por Telegram e e-mail.
Encerrar: tecla Q na janela do vídeo.
"""
from epi_core import Config, FonteRTSP, iniciar

if __name__ == "__main__":
    cfg = Config.carregar()
    try:
        iniciar("producao", lambda: FonteRTSP(cfg.rtsp_url), cfg)
    except (RuntimeError, ValueError) as erro:
        print(f"\nERRO: {erro}")
    input("\nPressione ENTER para fechar...")
