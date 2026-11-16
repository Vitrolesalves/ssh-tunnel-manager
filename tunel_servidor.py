# -*- coding: utf-8 -*-
"""
TunelServidor - roda no SERVIDOR DA BASE e mantém o túnel reverso até a VPS.

Equivalente a:
    ssh -R 5433:localhost:5432 root@VPS -N -o ServerAliveInterval=60
mas com reconexão automática quando o túnel cai e painel de status no console.

Uso:
    TunelServidor.exe                (deixa rodando; Ctrl+C encerra)
    TunelServidor.exe --diagnostico  (testa VPS, porta remota e banco local e sai)
"""

import socket
import sys
import threading
import time

import paramiko
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from comum import (
    Registro,
    aguardar,
    bipe,
    carregar_config,
    conectar_ssh,
    definir_titulo,
    encaminhar,
    fmt_duracao,
    vigiar_caminho_completo,
    vigiar_transporte,
)

console = Console()


class Estado:
    def __init__(self):
        self.fase = "iniciando"  # conectando | online | aguardando
        self.online_desde = None
        self.proxima_tentativa = None
        self.reconexoes = 0
        self.quedas = 0
        self.canais_ativos = 0
        self.total_conexoes = 0
        self.trava = threading.Lock()


class Painel:
    def __init__(self, estado, registro, cfg):
        self.estado = estado
        self.registro = registro
        self.cfg = cfg

    def __rich__(self):
        e = self.estado
        srv = self.cfg["servidor"]
        agora = time.time()

        if e.fase == "online":
            status = Text("ONLINE", style="bold green")
            status.append(f"  (ha {fmt_duracao(agora - e.online_desde)})", style="green")
            borda = "green"
        elif e.fase == "conectando":
            status = Text("CONECTANDO...", style="bold yellow")
            borda = "yellow"
        elif e.fase == "aguardando":
            falta = fmt_duracao((e.proxima_tentativa or agora) - agora)
            status = Text(f"TUNEL CAIU - nova tentativa em {falta}", style="bold red")
            borda = "red"
        else:
            status = Text("INICIANDO...", style="bold cyan")
            borda = "cyan"

        tab = Table.grid(padding=(0, 2))
        tab.add_column(justify="right", style="bold bright_black")
        tab.add_column()
        tab.add_row("Status", status)
        tab.add_row(
            "Tunel",
            Text(
                f"VPS:{srv['porta_remota']}  ->  {srv['host_local']}:{srv['porta_local']}",
                style="cyan",
            ),
        )
        tab.add_row(
            "VPS",
            f"{self.cfg['usuario']}@{self.cfg['vps_host']}:{self.cfg.get('vps_porta', 22)}",
        )
        tab.add_row("Conexoes", f"{e.canais_ativos} ativas  |  {e.total_conexoes} no total")
        tab.add_row("Historico", f"{e.quedas} quedas  |  {e.reconexoes} reconexoes")

        linhas = []
        for quando, texto, estilo in self.registro.ultimos(14):
            linha = Text(quando.strftime("%d/%m %H:%M:%S  "), style="bright_black")
            linha.append(texto, style=estilo)
            linhas.append(linha)
        eventos = Text("\n").join(linhas) if linhas else Text("(sem eventos ainda)", style="bright_black")

        return Group(
            Panel(
                tab,
                title="[bold]GERENCIADOR DE TUNEL - SERVIDOR (base -> VPS)[/bold]",
                border_style=borda,
                padding=(1, 2),
            ),
            Panel(eventos, title="Eventos", border_style="bright_black", padding=(0, 1)),
        )


def atender_canal(canal, estado, registro, host_local, porta_local):
    """Cada conexão que chega pelo túnel é ligada ao banco local."""
    try:
        sock = socket.create_connection((host_local, porta_local), timeout=10)
    except Exception as exc:
        registro.add(
            f"Banco local nao respondeu em {host_local}:{porta_local} - {exc}", "red"
        )
        try:
            canal.close()
        except Exception:
            pass
        return
    with estado.trava:
        estado.canais_ativos += 1
        estado.total_conexoes += 1
    try:
        encaminhar(canal, sock)
    finally:
        with estado.trava:
            estado.canais_ativos -= 1


def rodar(cfg, estado, registro):
    srv = cfg["servidor"]
    espera = 3
    ja_conectou = False

    while True:
        estado.fase = "conectando"
        registro.add(f"Conectando na VPS {cfg['vps_host']}...", "cyan")
        cliente = None
        try:
            cliente, transporte = conectar_ssh(cfg)
            transporte.request_port_forward("", srv["porta_remota"])
        except paramiko.AuthenticationException:
            if cliente:
                cliente.close()
            registro.add(
                "Usuario ou senha recusados pela VPS - confira o config.json", "bold red"
            )
            espera = aguardar(estado, max(espera, 30))
            continue
        except Exception as exc:
            if cliente:
                cliente.close()
            msg = str(exc) or type(exc).__name__
            if "forward" in msg.lower() or "denied" in msg.lower():
                registro.add(
                    f"VPS recusou a porta {srv['porta_remota']} - ainda ha uma sessao antiga "
                    "segurando a porta (a VPS a derruba sozinha em ~90 s) ou outro tunel "
                    "ativo nela. Tentando de novo em poucos segundos.",
                    "bold red",
                )
                # Sessao zumbi some sozinha em ~90 s: nao adianta esperar ate 60 s
                # entre tentativas (backoff), tenta a cada poucos segundos.
                espera = 3
                aguardar(estado, cfg.get("espera_porta_ocupada_segundos", 8))
                continue
            registro.add(f"Falha ao conectar: {msg}", "red")
            espera = aguardar(estado, espera)
            continue

        espera = 3
        estado.fase = "online"
        estado.online_desde = time.time()
        if ja_conectou:
            estado.reconexoes += 1
            registro.add("Tunel RESTABELECIDO!", "bold green")
        else:
            registro.add(
                f"Tunel estabelecido: VPS:{srv['porta_remota']} -> "
                f"{srv['host_local']}:{srv['porta_local']}",
                "bold green",
            )
        ja_conectou = True

        threading.Thread(
            target=vigiar_transporte, args=(transporte, registro), daemon=True
        ).start()
        # Teste de ponta a ponta (VPS -> tunel -> banco) a cada 30 s: pega o caso em
        # que a conexao SSH esta viva mas o banco nao responde pelo tunel.
        threading.Thread(
            target=vigiar_caminho_completo,
            args=(
                transporte,
                registro,
                "127.0.0.1",
                srv["porta_remota"],
                srv["host_local"],
                srv["porta_local"],
                int(srv.get("intervalo_verificacao_segundos", 30)),
            ),
            daemon=True,
        ).start()

        try:
            while transporte.is_active():
                canal = transporte.accept(1)
                if canal is not None:
                    threading.Thread(
                        target=atender_canal,
                        args=(canal, estado, registro, srv["host_local"], srv["porta_local"]),
                        daemon=True,
                    ).start()
        finally:
            try:
                cliente.close()
            except Exception:
                pass

        estado.quedas += 1
        registro.add("O TUNEL CAIU! Tentando reconectar...", "bold red")
        if cfg.get("alerta_sonoro", True):
            bipe()
        espera = aguardar(estado, espera)


def diagnostico(cfg):
    srv = cfg["servidor"]
    console.print(f"[cyan]1/3[/cyan] Conectando na VPS {cfg['vps_host']}...")
    try:
        cliente, transporte = conectar_ssh(cfg)
    except Exception as exc:
        console.print(f"[bold red]FALHOU:[/bold red] {exc}")
        return 1
    console.print("[green]OK - autenticado na VPS[/green]")

    console.print(f"[cyan]2/3[/cyan] Testando a porta remota {srv['porta_remota']}...")
    try:
        transporte.request_port_forward("", srv["porta_remota"])
        transporte.cancel_port_forward("", srv["porta_remota"])
        console.print("[green]OK - porta livre (nenhum outro tunel ativo nela)[/green]")
    except Exception:
        console.print(
            "[yellow]Porta ocupada - ja existe um tunel ativo usando essa porta na VPS[/yellow]"
        )

    console.print(
        f"[cyan]3/3[/cyan] Testando o banco local {srv['host_local']}:{srv['porta_local']}..."
    )
    try:
        socket.create_connection((srv["host_local"], srv["porta_local"]), timeout=5).close()
        console.print("[green]OK - banco local respondendo[/green]")
    except Exception as exc:
        console.print(f"[bold red]FALHOU:[/bold red] banco local nao respondeu ({exc})")

    cliente.close()
    return 0


def main():
    cfg = carregar_config()
    definir_titulo(cfg.get("nome", "Tunel - Servidor (base -> VPS)"))

    if "--diagnostico" in sys.argv:
        raise SystemExit(diagnostico(cfg))

    estado = Estado()
    registro = Registro()
    registro.add("Programa iniciado. Pressione Ctrl+C para encerrar.", "bright_black")
    try:
        with Live(Painel(estado, registro, cfg), console=console, refresh_per_second=4):
            rodar(cfg, estado, registro)
    except KeyboardInterrupt:
        console.print("\n[bold]Encerrado pelo usuario. O tunel foi desligado.[/bold]")


if __name__ == "__main__":
    main()
