# Gerenciador de Túnel SSH (base ⇄ VPS)

Dois programas de console que mantêm de pé, sozinhos, o túnel SSH que liga o banco de dados on-premise de uma empresa à VPS onde roda o sistema web dela — com reconexão automática, verificação ativa de queda e um painel visual de status. Escritos em Python (paramiko + rich) e distribuídos como `.exe` standalone via PyInstaller, para qualquer pessoa da equipe usar com dois cliques.

Este repositório é uma versão pública de portfólio: a configuração real (IP da VPS, credenciais) fica num `config.json` que nunca foi versionado — o [`config.exemplo.json`](config.exemplo.json) mostra o formato.

## O problema

O sistema web da empresa rodava numa VPS, mas o banco de dados ficava na rede interna, acessível só por VPN. Toda query do site atravessava a VPN — e a latência deixava o site arrastado a ponto de atrapalhar a operação.

A solução que desenhei: montar um servidor PostgreSQL na própria base (rede interna) e ligá-lo à VPS por um **túnel SSH reverso**. O servidor da base abre a conexão de dentro para fora (não precisa de porta aberta no firewall) e publica o banco na porta 5433 da VPS. Do ponto de vista do Django na VPS, o banco vira `localhost:5433` — a latência de VPN praticamente sumiu do caminho das queries.

```
   REDE INTERNA (base)                        VPS (nuvem)
  ┌────────────────────┐                 ┌─────────────────────┐
  │  PostgreSQL :5432  │                 │  Django/web         │
  │         ▲          │   ssh reverso   │     │               │
  │  TunelServidor ────┼────────────────►│  localhost:5433     │
  └────────────────────┘  (de dentro     └─────────▲───────────┘
                           para fora)              │ ssh local
                                          TunelCliente (PC da equipe)
```

Só que túnel SSH mantido na mão (`ssh -R ... -N` numa janela aberta) cai — queda de energia, rede oscilando, reinício do servidor — e alguém precisava perceber e subir de novo. Foi daí que nasceram estes dois programas:

| Programa | Onde roda | O que faz | Substitui |
|---|---|---|---|
| `TunelServidor.exe` | Servidor da base | Mantém o túnel reverso VPS:5433 → localhost:5432 e reconecta sozinho quando cai | `ssh -R 5433:localhost:5432 root@VPS -N` |
| `TunelCliente.exe` | PC de quem precisa do banco | Abre o túnel local localhost:5433 → VPS:5432 **e monitora** se o túnel do servidor está de pé, avisando (visual + bipe) quando cair | `ssh -L 5433:localhost:5432 root@VPS -N` |

## Como funciona por dentro

O que diferencia isto de um script de `ssh` num loop:

- **Detecção ativa de queda.** O keepalive do paramiko só *envia* pacotes — numa queda "silenciosa" (cabo, energia, roteador) a conexão ficaria pendurada por minutos até o TCP desistir. Aqui, um watchdog manda um `global request` à VPS a cada 45s e espera a resposta chegar; sem resposta em 15s, a conexão é derrubada na hora e o laço principal reconecta ([`comum.py`](comum.py), `transporte_responde`).
- **Backoff exponencial.** Reconexão espera 3s, depois 6s, 12s... até o teto de 60s entre tentativas, com contagem regressiva visível no painel.
- **Encaminhamento multi-conexão.** Cada conexão que chega pelo túnel vira uma thread própria fazendo o relay de bytes nos dois sentidos (`select` + sockets), com contadores de conexões ativas/totais no painel.
- **Painel de status** (rich): verde = tudo de pé, vermelho = caiu (com bipe opcional), além do histórico dos últimos eventos com horário — pensado para ficar aberto num monitor da base e qualquer pessoa da equipe entender de relance.
- **Diagnóstico de um comando.** `--diagnostico` testa, na ordem: autenticação na VPS, disponibilidade da porta remota e resposta do banco local — e diz exatamente o que falhou.
- **Mensagens de erro que orientam.** Porta recusada pela VPS? O painel avisa que provavelmente há outro túnel ativo na mesma porta (o `ssh` manual antigo) em vez de só estourar a exceção.

## Como usar

1. Copie `config.exemplo.json` para `config.json` e preencha IP, usuário e senha da VPS (o `.gitignore` já impede o commit).
2. Rode o programa desejado:

```powershell
pip install -r requirements.txt
python tunel_servidor.py    # no servidor da base
python tunel_cliente.py     # no PC de quem consome o banco
```

3. Para gerar os executáveis standalone (o `config.json` fica **fora** do .exe, na mesma pasta):

```powershell
.\build.ps1    # gera dist\TunelServidor.exe e dist\TunelCliente.exe
```

4. Para o túnel da base voltar sozinho após reiniciar o servidor: `Win+R` → `shell:startup` → cole um atalho do `TunelServidor.exe`.

Modo diagnóstico:

```
TunelServidor.exe --diagnostico   # testa VPS, porta remota e banco local
TunelCliente.exe  --diagnostico   # testa VPS e se o túnel do servidor está online
```

## Segurança

- Credenciais vivem só no `config.json` local, nunca no código nem no repositório.
- Melhoria que eu recomendaria (e recomendo no README original da empresa): usuário SSH dedicado só para túnel, sem shell, autenticando por chave em vez de root com senha.
- Antivírus/SmartScreen podem desconfiar de `.exe` de PyInstaller na primeira execução — falso positivo conhecido; basta autorizar.

## Stack

Python 3 · paramiko (SSH/túneis) · rich (TUI) · PyInstaller (distribuição standalone)

---

Daniel Augusto — [daniel.aual08@gmail.com](mailto:daniel.aual08@gmail.com)
