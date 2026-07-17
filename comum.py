# -*- coding: utf-8 -*-
"""
Funções e classes compartilhadas entre o TunelServidor e o TunelCliente.
"""

import ctypes
import json
import os
import select
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime

import paramiko

TAMANHO_BUFFER = 32768


def pasta_base():
    """Pasta onde está o .exe (quando empacotado) ou o .py - é onde fica o config.json."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def carregar_config():
    caminho = os.path.join(pasta_base(), "config.json")
    if not os.path.exists(caminho):
        raise SystemExit(
            "ERRO: config.json nao encontrado em:\n  " + pasta_base() + "\n"
            "Copie o config.exemplo.json para config.json (na mesma pasta do programa) "
            "e preencha os dados da VPS."
        )
    with open(caminho, "r", encoding="utf-8") as arquivo:
        return json.load(arquivo)


def definir_titulo(texto):
    """Muda o título da janela do console (só funciona no Windows)."""
    try:
        ctypes.windll.kernel32.SetConsoleTitleW(texto)
    except Exception:
        pass


def bipe():
    try:
        import winsound

        winsound.Beep(880, 400)
    except Exception:
        pass


def conectar_ssh(cfg):
    """Abre a conexão SSH com a VPS e devolve (cliente, transporte) já com keepalive."""
    cliente = paramiko.SSHClient()
    cliente.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cliente.connect(
        cfg["vps_host"],
        port=cfg.get("vps_porta", 22),
        username=cfg["usuario"],
        password=cfg["senha"],
        timeout=15,
        banner_timeout=15,
        auth_timeout=15,
        look_for_keys=False,
        allow_agent=False,
    )
    transporte = cliente.get_transport()
    transporte.set_keepalive(int(cfg.get("keepalive_segundos", 30)))
    return cliente, transporte


def transporte_responde(transporte, timeout=15):
    """Verificação ativa: manda um pedido à VPS e espera a resposta chegar.

    O keepalive do paramiko só ENVIA pacotes, não confere se houve resposta -
    numa queda de rede "silenciosa" (cabo/energia/roteador) a conexão ficaria
    pendurada por vários minutos até o TCP desistir. Sem resposta dentro do
    timeout, consideramos a conexão morta.
    """
    respondeu = threading.Event()

    def perguntar():
        try:
            transporte.global_request("keepalive@openssh.com", wait=True)
        except Exception:
            pass
        respondeu.set()

    threading.Thread(target=perguntar, daemon=True).start()
    return respondeu.wait(timeout)


def vigiar_transporte(transporte, registro, intervalo=45, timeout=15):
    """Fica testando a conexão; se a VPS parar de responder, derruba o transporte
    para o laço principal perceber a queda e reconectar na hora."""
    while transporte.is_active():
        time.sleep(intervalo)
        if not transporte.is_active():
            return
        if not transporte_responde(transporte, timeout):
            registro.add(
                "VPS parou de responder - derrubando a conexão para reconectar",
                "yellow",
            )
            try:
                transporte.close()
            except Exception:
                pass
            return


def encaminhar(canal, sock):
    """Copia dados nos dois sentidos entre o canal SSH e o socket até um lado fechar."""
    try:
        while True:
            prontos, _, _ = select.select([canal, sock], [], [], 300)
            if canal in prontos:
                dados = canal.recv(TAMANHO_BUFFER)
                if not dados:
                    break
                sock.sendall(dados)
            if sock in prontos:
                dados = sock.recv(TAMANHO_BUFFER)
                if not dados:
                    break
                canal.sendall(dados)
    except Exception:
        pass
    finally:
        for lado in (canal, sock):
            try:
                lado.close()
            except Exception:
                pass


def aguardar(estado, espera):
    """Espera antes da próxima tentativa (mostrando contagem no painel) e
    devolve o próximo tempo de espera, dobrado até o limite de 60s."""
    estado.fase = "aguardando"
    estado.proxima_tentativa = time.time() + espera
    while time.time() < estado.proxima_tentativa:
        time.sleep(0.2)
    return min(espera * 2, 60)


class Registro:
    """Guarda os últimos eventos para mostrar no painel."""

    def __init__(self, tamanho=300):
        self._eventos = deque(maxlen=tamanho)
        self._trava = threading.Lock()

    def add(self, texto, estilo="white"):
        with self._trava:
            self._eventos.append((datetime.now(), texto, estilo))

    def ultimos(self, quantos):
        with self._trava:
            return list(self._eventos)[-quantos:]


def fmt_duracao(segundos):
    segundos = max(0, int(segundos))
    horas, resto = divmod(segundos, 3600)
    minutos, seg = divmod(resto, 60)
    if horas:
        return f"{horas}h {minutos:02d}m {seg:02d}s"
    if minutos:
        return f"{minutos}m {seg:02d}s"
    return f"{seg}s"
