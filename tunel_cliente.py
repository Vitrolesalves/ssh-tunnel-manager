# -*- coding: utf-8 -*-
"""
TunelCliente - roda no SEU computador (ou dos colegas).

Faz duas coisas ao mesmo tempo:
  1. Abre o tunel local, equivalente a:
         ssh -L 5433:localhost:5432 root@VPS -N -o ServerAliveInterval=60
     com reconexao automatica.
  2. Monitora se o tunel do SERVIDOR DA BASE esta de pe na VPS (porta 5433)
     e avisa na tela (e com bipe) quando ele cair.

Uso:
    TunelCliente.exe                 (deixa rodando; Ctrl+C encerra)
    TunelCliente.exe --diagnostico   (testa VPS e tunel do servidor e sai)
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
        self.transporte = None
        self.tunel_base = None  # None = ainda nao verificado | True | False
        self.ultima_verificacao = None
        self.erro_porta_local = None
        self.trava = threading.Lock()


class Painel:
    def __init__(self, estado, registro, cfg):
        self.estado = estado
        self.registro = registro
        self.cfg = cfg

    def __rich__(self):
        e = self.estado
        cli = self.cfg["cliente"]
        agora = time.time()

        if e.fase == "online":
            vps = Text("CONECTADO", style="bold green")
            vps.append(f"  (ha {fmt_duracao(agora - e.online_desde)})", style="green")
        elif e.fase == "conectando":
            vps = Text("CONECTANDO...", style="bold yellow")
        elif e.fase == "aguardando":
            falta = fmt_duracao((e.proxima_tentativa or agora) - agora)
            vps = Text(f"SEM CONEXAO - nova tentativa em {falta}", style="bold red")
        else:
            vps = Text("INICIANDO...", style="bold cyan")

        if e.erro_porta_local:
            tunel_local = Text(e.erro_porta_local, style="bold red")
        else:
            tunel_local = Text(
                f"localhost:{cli['porta_local']}  ->  VPS "
                f"{cli['host_destino']}:{cli['porta_destino']}",
                style="cyan",
            )

        if e.tunel_base is True:
            base = Text("ONLINE", style="bold green")
            if e.ultima_verificacao:
                base.append(
                    f"  (verificado ha {fmt_duracao(agora - e.ultima_verificacao)})",
                    style="green",
                )
            borda = "green" if e.fase == "online" else "yellow"
        elif e.tunel_base is False:
            base = Text(
                "CAIU - VERIFIQUE O PROGRAMA NO SERVIDOR DA BASE!", style="bold red"
            )
            borda = "red"
        else:
            base = Text("aguardando verificacao...", style="yellow")
            borda = "yellow" if e.fase == "online" else "red"
        if e.fase == "aguardando":
            borda = "red"

        tab = Table.grid(padding=(0, 2))
        tab.add_column(justify="right", style="bold bright_black")
        tab.add_column()
        tab.add_row("VPS", vps)
        tab.add_row("Tunel local", tunel_local)
        tab.add_row("Tunel do servidor", base)
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
                title="[bold]GERENCIADOR DE TUNEL - CLIENTE (seu PC -> VPS)[/bold]",
                border_style=borda,
                padding=(1, 2),
            ),
            Panel(eventos, title="Eventos", border_style="bright_black", padding=(0, 1)),
        )


def repassar(canal, sock, estado):
    with estado.trava:
        estado.canais_ativos += 1
        estado.total_conexoes += 1
    try:
        encaminhar(canal, sock)
    finally:
        with estado.trava:
            estado.canais_ativos -= 1


def ouvir_local(cfg, estado, registro):
    """Escuta na porta local e repassa cada conexão para a VPS pelo SSH."""
    cli = cfg["cliente"]
    porta = cli["porta_local"]
    try:
        servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        servidor.bind(("127.0.0.1", porta))
        servidor.listen(100)
    except OSError as exc:
        estado.erro_porta_local = (
            f"Porta local {porta} ja esta em uso ({exc.strerror or exc}). "
            "Feche outros tuneis (ssh -L) e abra o programa de novo."
        )
        registro.add(estado.erro_porta_local, "bold red")
        return

    registro.add(
        f"Tunel local pronto: localhost:{porta} -> VPS "
        f"{cli['host_destino']}:{cli['porta_destino']}",
        "green",
    )
    while True:
        sock, origem = servidor.accept()
        transporte = estado.transporte
        if transporte is None or not transporte.is_active():
            registro.add(
                "Conexao local recusada: sem ligacao com a VPS no momento", "yellow"
            )
            sock.close()
            continue
        try:
            canal = transporte.open_channel(
                "direct-tcpip",
                (cli["host_destino"], cli["porta_destino"]),
                origem,
                timeout=10,
            )
        except Exception as exc:
            registro.add(
                f"VPS recusou a conexao para {cli['host_destino']}:"
                f"{cli['porta_destino']} - {exc}",
                "red",
            )
            sock.close()
            continue
        threading.Thread(target=repassar, args=(canal, sock, estado), daemon=True).start()


def monitorar_tunel_base(cfg, estado, registro):
    """Confere de tempos em tempos se o tunel do servidor da base esta de pe
    na VPS (se tem algo escutando na porta de verificacao)."""
    cli = cfg["cliente"]
    porta = cli["porta_verificacao_tunel"]
    intervalo = cli.get("intervalo_verificacao_segundos", 30)
    proximo = 0

    while True:
        time.sleep(1)
        transporte = estado.transporte
        if transporte is None or not transporte.is_active():
            estado.tunel_base = None
            continue
        if time.time() < proximo:
            continue

        try:
            canal = transporte.open_channel(
                "direct-tcpip", ("localhost", porta), ("127.0.0.1", 0), timeout=10
            )
            canal.close()
            online = True
        except Exception:
            online = False

        anterior = estado.tunel_base
        estado.tunel_base = online
        estado.ultima_verificacao = time.time()

        if online and anterior is not True:
            registro.add(
                f"Tunel do servidor esta ONLINE (VPS:{porta} respondendo)", "bold green"
            )
        elif not online and anterior is not False:
            registro.add(
                f"ATENCAO: TUNEL DO SERVIDOR CAIU - nada escutando na porta {porta} "
                "da VPS! Verifique o programa no servidor da base.",
                "bold red",
            )
            if cfg.get("alerta_sonoro", True):
                bipe()

        # quando o tunel esta fora, confere com mais frequencia para avisar
        # rapido quando voltar
        proximo = time.time() + (10 if not online else intervalo)


def rodar(cfg, estado, registro):
    espera = 3
    ja_conectou = False

    while True:
        estado.fase = "conectando"
        registro.add(f"Conectando na VPS {cfg['vps_host']}...", "cyan")
        try:
            cliente, transporte = conectar_ssh(cfg)
        except paramiko.AuthenticationException:
            registro.add(
                "Usuario ou senha recusados pela VPS - confira o config.json", "bold red"
            )
            espera = aguardar(estado, max(espera, 30))
            continue
        except Exception as exc:
            registro.add(f"Falha ao conectar: {exc or type(exc).__name__}", "red")
            espera = aguardar(estado, espera)
            continue

        espera = 3
        estado.transporte = transporte
        estado.fase = "online"
        estado.online_desde = time.time()
        if ja_conectou:
            estado.reconexoes += 1
            registro.add("Conexao com a VPS RESTABELECIDA!", "bold green")
        else:
            registro.add("Conectado na VPS!", "bold green")
        ja_conectou = True

        threading.Thread(
            target=vigiar_transporte, args=(transporte, registro), daemon=True
        ).start()

        while transporte.is_active():
            time.sleep(0.5)

        estado.transporte = None
        estado.tunel_base = None
        estado.quedas += 1
        registro.add("Conexao com a VPS CAIU! Tentando reconectar...", "bold red")
        if cfg.get("alerta_sonoro", True):
            bipe()
        try:
            cliente.close()
        except Exception:
            pass
        espera = aguardar(estado, espera)


def diagnostico(cfg):
    cli_cfg = cfg["cliente"]
    console.print(f"[cyan]1/2[/cyan] Conectando na VPS {cfg['vps_host']}...")
    try:
        cliente, transporte = conectar_ssh(cfg)
    except Exception as exc:
        console.print(f"[bold red]FALHOU:[/bold red] {exc}")
        return 1
    console.print("[green]OK - autenticado na VPS[/green]")

    porta = cli_cfg["porta_verificacao_tunel"]
    console.print(
        f"[cyan]2/2[/cyan] Verificando o tunel do servidor (VPS porta {porta})..."
    )
    try:
        canal = transporte.open_channel(
            "direct-tcpip", ("localhost", porta), ("127.0.0.1", 0), timeout=10
        )
        canal.close()
        console.print("[bold green]TUNEL DO SERVIDOR: ONLINE[/bold green]")
        resultado = 0
    except Exception as exc:
        console.print(f"[bold red]TUNEL DO SERVIDOR: FORA DO AR[/bold red] ({exc})")
        resultado = 1

    cliente.close()
    return resultado


def main():
    definir_titulo("Tunel - Cliente (seu PC -> VPS)")
    cfg = carregar_config()

    if "--diagnostico" in sys.argv:
        raise SystemExit(diagnostico(cfg))

    estado = Estado()
    registro = Registro()
    registro.add("Programa iniciado. Pressione Ctrl+C para encerrar.", "bright_black")

    threading.Thread(target=ouvir_local, args=(cfg, estado, registro), daemon=True).start()
    threading.Thread(
        target=monitorar_tunel_base, args=(cfg, estado, registro), daemon=True
    ).start()

    try:
        with Live(Painel(estado, registro, cfg), console=console, refresh_per_second=4):
            rodar(cfg, estado, registro)
    except KeyboardInterrupt:
        console.print("\n[bold]Encerrado pelo usuario. O tunel local foi desligado.[/bold]")


if __name__ == "__main__":
    main()
