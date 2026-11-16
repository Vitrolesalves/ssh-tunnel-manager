# Redundancia do tunel (principal + secundario + terciario)

Problema (queda de 16/11/2026, 17 min): a rede do escritorio piscou, a sessao SSH antiga
ficou "morta" na VPS segurando a porta 5433 e o tunel novo nao conseguia subir
(`bind [127.0.0.1]:5433: Address already in use`). O sistema web ficou travado.

## O que ja esta corrigido

1. **VPS**: `/etc/ssh/sshd_config.d/zz-keepalive-tunel.conf` com `ClientAliveInterval 30` e
   `ClientAliveCountMax 3`: sessao morta cai em ~90 s (antes ~15 min). Aplicado em 16/11.
2. **TunelServidor** (esta versao):
   - se a porta estiver presa por sessao antiga, tenta de novo a cada ~8 s (antes: backoff de ate 60 s);
   - a cada 30 s testa de ponta a ponta (VPS -> tunel -> Postgres, via pedido de SSL) e refaz o
     tunel sozinho se a conexao SSH esta viva mas o banco nao responde; se o problema for o
     PostgreSQL local, avisa e NAO derruba o tunel.
3. **TunelCliente**: o monitor deixou de contar "porta aberta" como tunel de pe; exige o Postgres responder.
4. `--config <arquivo.json>` e `"nome"` no config: permite varias instancias (uma por tunel).

## Arquitetura com failover

```
 Escritorio                                   VPS
 Maquina A  TunelServidor  5441 ─────┐
 Maquina B  TunelServidor  5442 ─────┼──► HAProxy 127.0.0.1:5433 ──► Django (inalterado)
 Maquina C / OpenVPN       5443 ─────┘     (check por pedido SSL, troca em ~6 s)
```

- Cada maquina roda o `TunelServidor` com um `config.json` proprio; so muda `servidor.porta_remota`
  (5441, 5442, 5443) e, se o Postgres nao for local, `servidor.host_local` = IP do servidor do banco
  na rede interna. Qualquer PC da rede que alcance o banco pode ser o secundario.
- **Independencia real:** tuneis que saem do mesmo link do escritorio caem juntos. O ideal e o terceiro
  caminho por outro lugar: segunda saida de internet (4G/outro provedor) ou a VPN que a VPS
  ja mantem (VPS -> OpenVPN -> IP interno do banco:5432; precisa de `pg_hba.conf` liberando o IP do tun0
  da VPS). Nesse caso o backend do HAProxy e `server terciario <IP-do-banco>:5432 check backup`.

## Passo a passo (janela combinada: derruba o banco por ~1 min uma unica vez)

1. VPS: `apt install haproxy`; copiar `haproxy.cfg` para `/etc/haproxy/haproxy.cfg`; **nao** iniciar ainda.
2. Escritorio: mudar `porta_remota` do tunel atual de 5433 para **5441** e reiniciar o `TunelServidor`.
3. VPS: `ss -ltn | grep 5441` e testar o SSL request em 5441; `haproxy -c -f /etc/haproxy/haproxy.cfg`;
   `systemctl enable --now haproxy`. O Django volta a falar com 5433 (agora o HAProxy).
4. Subir o secundario (5442) e o terciario (5443); conferir `echo "show stat" | socat stdio /run/haproxy/admin.sock`.
5. Teste de falha: fechar o `TunelServidor` principal e ver o trafego passar para o secundario em segundos.

Se o HAProxy falhar: desligar o HAProxy e voltar `porta_remota` para 5433 (volta ao desenho antigo).

## Seguranca (recomendado)

Usuario SSH dedicado so para tunel (sem shell), com chave em vez de `root` + senha, e no `sshd_config`:

```
Match User tunel
    AllowTcpForwarding remote
    PermitListen 127.0.0.1:5441 127.0.0.1:5442 127.0.0.1:5443
    X11Forwarding no
    PermitTTY no
    ForceCommand /usr/sbin/nologin
```
