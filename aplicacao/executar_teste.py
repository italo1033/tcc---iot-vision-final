"""EXECUTÁVEL 1 - TESTE: analisa o vídeo gravado teste.MOV.

Serve para validar o funcionamento completo (modelo, regra de conformidade,
relógio, Telegram e e-mail) sem depender da câmera.
Uso: python executar_teste.py [caminho_do_video]
"""
import sys
from pathlib import Path

from epi_core import BASE_DIR, Config, FonteArquivo, iniciar

if __name__ == "__main__":
    video = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "teste.MOV"
    cfg = Config.carregar(salvar_video=True)
    iniciar("teste", lambda: FonteArquivo(video), cfg)
    input("\nPressione ENTER para fechar...")
