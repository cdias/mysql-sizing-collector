# Coleta automática de dados de sizing (MySQL + Zabbix)

Script para preencher o data request de sizing sem digitar nada manualmente.
Roda na máquina de quem tem acesso aos bancos MySQL e à API do Zabbix (ex: um
bastion/jump host do time de DBA).

## Pré-requisitos

- Python 3 instalado (`python3 --version`)
- Client `mysql` instalado e no PATH (`mysql --version`)
- Acesso de rede dessa máquina para: cada host MySQL (porta 3306) e para a URL
  da API do Zabbix (`https://<seu-zabbix>/api_jsonrpc.php`)
- Um usuário MySQL **somente leitura** (basta `SELECT` em `information_schema`)
- Um usuário ou token de API do Zabbix com permissão de **leitura** nos hosts
  monitorados

## Passo 1 — Baixar os arquivos

Os 3 arquivos abaixo devem estar na mesma pasta:

- `collect_sizing_data.py`
- `instances.example.csv`
- `zabbix_item_map.example.json`

## Passo 2 — Configurar credenciais (variáveis de ambiente)

Nunca colocamos senha dentro dos arquivos. No terminal, antes de rodar o
script:

```bash
export MYSQL_USER="usuario_readonly"
export MYSQL_PWD="senha_do_mysql"

export ZABBIX_URL="https://SEU-ZABBIX/api_jsonrpc.php"
# opção A — já tem um token de API gerado no Zabbix:
export ZABBIX_TOKEN="token_gerado_no_zabbix"
# opção B — não tem token, só usuário/senha (o script loga e pega o token sozinho):
export ZABBIX_USER="usuario_zabbix"
export ZABBIX_PASSWORD="senha_zabbix"
```

## Passo 3 — Listar as instâncias

Copie `instances.example.csv` para `instances.csv` e preencha uma linha por
banco MySQL:

```bash
cp instances.example.csv instances.csv
```

Colunas:

| coluna | o que é |
|---|---|
| `label` | nome livre pra identificar a instância no resultado final |
| `mysql_host` | IP ou hostname do MySQL |
| `mysql_port` | porta (deixe `3306` se for o padrão) |
| `zabbix_host` | nome exato do host **como está cadastrado no Zabbix** |

## Passo 4 — Descobrir os item keys do Zabbix

Cada template de Zabbix nomeia os contadores de queries de um jeito diferente,
então primeiro rodamos um modo de descoberta:

```bash
python3 collect_sizing_data.py discover --instances instances.csv
```

Isso imprime, para cada host, uma lista de itens candidatos parecida com:

```
=== pagamentos-prod (zabbix host: db-pagamentos-prod) ===
  key=mysql.com_select         name=Com_select          units=
  key=mysql.com_insert         name=Com_insert          units=
  ...
```

Anote a `key=` correta para cada uma destas 4 métricas: **select, insert,
update, delete**.

## Passo 5 — Preencher o mapa de item keys

Copie o exemplo e ajuste com as keys do Passo 4:

```bash
cp zabbix_item_map.example.json zabbix_item_map.json
```

- Se **todos os hosts usam o mesmo template**, basta preencher o bloco
  `"default"`.
- Se algum host tiver keys diferentes, adicione um bloco extra com o nome
  exato do `zabbix_host` daquela instância (ele tem prioridade sobre o
  `"default"`).

## Passo 6 — Rodar a coleta

```bash
python3 collect_sizing_data.py collect \
  --instances instances.csv \
  --map zabbix_item_map.json \
  --days 30 \
  --output sizing_output.csv
```

- `--days 30` define a janela de histórico usada para calcular QPS pico/médio
  no Zabbix (pode aumentar, ex. `--days 90`, se quiser uma janela maior).
- O progresso aparece no terminal; qualquer erro por instância fica registrado
  nas colunas `mysql_error` / `zabbix_error` do CSV, sem travar as outras.

## Passo 7 — Conferir e enviar

Abra `sizing_output.csv` (Excel, Google Sheets, `cat`, etc.) e confira se não
há erros nas colunas `mysql_error`/`zabbix_error`. Depois é só colar as linhas
na planilha de data request.

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `Defina a variavel de ambiente MYSQL_USER` | esqueceu de exportar as variáveis do Passo 2 nessa sessão do terminal |
| `host 'X' nao encontrado no Zabbix` | nome em `zabbix_host` não bate exatamente com o cadastrado no Zabbix (confira em Hosts no painel) |
| `item key 'X' nao existe no host` | key copiada errada do `discover`, ou esse host usa um template diferente — rode `discover` de novo nele |
| erro de conexão MySQL | usuário sem permissão de rede/firewall para aquele host, ou porta errada |
| QPS/ratio parecem muito baixos ou muito altos | confira no `discover` se a key escolhida já é uma taxa por segundo (ex. unidade `qps`/`/s`) e não um contador cumulativo bruto |
