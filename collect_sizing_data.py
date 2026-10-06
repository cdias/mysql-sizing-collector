#!/usr/bin/env python3
"""
Coleta dados de sizing (volume, QPS pico/medio, read/write ratio) de instancias
MySQL + Zabbix para preencher o data request de sizing do TiDB.

So usa biblioteca padrao do Python 3 (sem pip install) e o client `mysql` local.

Uso:
  1) Descobrir os item keys de QPS no Zabbix de cada host:
       python3 collect_sizing_data.py discover --instances instances.csv

  2) Preencher zabbix_item_map.json com os item keys corretos (um mapa global
     ou por host), usando a saida do discover como guia.

  3) Coletar os dados e gerar o CSV final:
       python3 collect_sizing_data.py collect --instances instances.csv \\
           --map zabbix_item_map.json --days 30 --output sizing_output.csv

Credenciais (nunca passadas por linha de comando, sempre por variavel de ambiente):
  MYSQL_USER, MYSQL_PWD        -> usuario/senha MySQL (read-only e suficiente)
  ZABBIX_URL                   -> ex: https://zabbix.empresa.com/api_jsonrpc.php
  ZABBIX_TOKEN                 -> token de API ja gerado (preferencial)
  ou ZABBIX_USER / ZABBIX_PASSWORD -> para o script logar e obter um token
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
import urllib.request

EXCLUDED_SCHEMAS = ("information_schema", "performance_schema", "mysql", "sys")
QPS_KEYWORDS = ("select", "insert", "update", "delete", "queries", "qps", "questions")
METRIC_NAMES = ("select", "insert", "update", "delete")


def read_instances(path):
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    required = {"label", "mysql_host", "zabbix_host"}
    for row in rows:
        missing = required - row.keys()
        if missing:
            sys.exit(f"instances.csv: faltam colunas {missing}")
    return rows


# ---------------------------------------------------------------- MySQL ----

def run_mysql_query(host, port, sql):
    user = os.environ.get("MYSQL_USER")
    if not user:
        sys.exit("Defina a variavel de ambiente MYSQL_USER")
    cmd = ["mysql", "-h", host, "-P", str(port or 3306), "-u", user, "-N", "-B", "-e", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "falha ao executar mysql")
    return result.stdout.strip()


def collect_mysql_info(host, port):
    version = run_mysql_query(host, port, "SELECT VERSION();")
    excluded = ",".join(f"'{s}'" for s in EXCLUDED_SCHEMAS)
    sql = (
        "SELECT ROUND(SUM(data_length+index_length)/1024/1024/1024, 2), "
        "IFNULL(SUM(table_rows), 0) "
        f"FROM information_schema.tables WHERE table_schema NOT IN ({excluded});"
    )
    out = run_mysql_query(host, port, sql)
    size_gb, approx_rows = (out.split("\t") + ["0", "0"])[:2]
    return {
        "mysql_version": version,
        "data_volume_gb": size_gb or "0",
        "approx_rows": approx_rows or "0",
    }


# --------------------------------------------------------------- Zabbix ----

class ZabbixClient:
    def __init__(self, url, token):
        self.url = url
        self.token = token
        self._id = 0

    @classmethod
    def from_env(cls):
        url = os.environ.get("ZABBIX_URL")
        if not url:
            sys.exit("Defina a variavel de ambiente ZABBIX_URL")
        token = os.environ.get("ZABBIX_TOKEN")
        if not token:
            zuser = os.environ.get("ZABBIX_USER")
            zpass = os.environ.get("ZABBIX_PASSWORD")
            if not (zuser and zpass):
                sys.exit("Defina ZABBIX_TOKEN ou ZABBIX_USER/ZABBIX_PASSWORD")
            client = cls(url, None)
            token = client._call("user.login", {"username": zuser, "password": zpass})
            client.token = token
            return client
        return cls(url, token)

    def _call(self, method, params):
        self._id += 1
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._id}
        if self.token and method not in ("user.login", "apiinfo.version"):
            payload["auth"] = self.token
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json-rpc"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        if "error" in body:
            raise RuntimeError(f"Zabbix API error em {method}: {body['error']}")
        return body["result"]

    def get_hostid(self, host_name):
        hosts = self._call("host.get", {"filter": {"host": [host_name]}, "output": ["hostid", "host"]})
        if not hosts:
            raise RuntimeError(f"host '{host_name}' nao encontrado no Zabbix")
        return hosts[0]["hostid"]

    def list_candidate_items(self, hostid):
        items = self._call(
            "item.get",
            {"hostids": [hostid], "output": ["itemid", "key_", "name", "units", "value_type"]},
        )
        out = []
        for it in items:
            haystack = f"{it['key_']} {it['name']}".lower()
            if any(k in haystack for k in QPS_KEYWORDS):
                out.append(it)
        return out

    def get_trend_stats(self, itemid, time_from, time_till):
        trends = self._call(
            "trend.get",
            {"itemids": [itemid], "time_from": time_from, "time_till": time_till, "output": "extend"},
        )
        if not trends:
            return None
        peak = max(float(t["value_max"]) for t in trends)
        total_weighted = sum(float(t["value_avg"]) * int(t["num"]) for t in trends)
        total_num = sum(int(t["num"]) for t in trends)
        avg = total_weighted / total_num if total_num else 0.0
        return {"peak": peak, "avg": avg}


def load_item_map(path, host_name):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if host_name in data:
        return data[host_name]
    if "default" in data:
        return data["default"]
    sys.exit(f"zabbix_item_map.json: sem mapa para '{host_name}' nem 'default'")


# ---------------------------------------------------------------- modes ----

def cmd_discover(args):
    instances = read_instances(args.instances)
    zc = ZabbixClient.from_env()
    for inst in instances:
        print(f"\n=== {inst['label']} (zabbix host: {inst['zabbix_host']}) ===")
        try:
            hostid = zc.get_hostid(inst["zabbix_host"])
            candidates = zc.list_candidate_items(hostid)
        except Exception as exc:
            print(f"  ERRO: {exc}")
            continue
        if not candidates:
            print("  nenhum item candidato encontrado (ajuste QPS_KEYWORDS se necessario)")
            continue
        for it in candidates:
            print(f"  key={it['key_']:<35} name={it['name']:<40} units={it.get('units','')}")
    print(
        "\nUse as keys acima para preencher zabbix_item_map.json, "
        "ex: {\"default\": {\"select\": \"mysql.com_select\", \"insert\": \"...\", "
        "\"update\": \"...\", \"delete\": \"...\"}}"
    )


def cmd_collect(args):
    instances = read_instances(args.instances)
    zc = ZabbixClient.from_env()
    time_till = int(time.time())
    time_from = time_till - args.days * 86400

    rows = []
    for inst in instances:
        label = inst["label"]
        print(f"Coletando {label}...", file=sys.stderr)
        row = {"instance": label}
        try:
            row.update(collect_mysql_info(inst["mysql_host"], inst.get("mysql_port")))
        except Exception as exc:
            row["mysql_error"] = str(exc)

        try:
            item_map = load_item_map(args.map, inst["zabbix_host"])
            hostid = zc.get_hostid(inst["zabbix_host"])
            metric_stats = {}
            for metric in METRIC_NAMES:
                key = item_map.get(metric)
                if not key:
                    continue
                items = zc._call("item.get", {"hostids": [hostid], "filter": {"key_": key}, "output": ["itemid"]})
                if not items:
                    raise RuntimeError(f"item key '{key}' nao existe no host")
                stats = zc.get_trend_stats(items[0]["itemid"], time_from, time_till)
                metric_stats[metric] = stats or {"peak": 0.0, "avg": 0.0}

            peak_total = sum(s["peak"] for s in metric_stats.values())
            avg_total = sum(s["avg"] for s in metric_stats.values())
            read_avg = metric_stats.get("select", {}).get("avg", 0.0)
            write_avg = sum(metric_stats.get(m, {}).get("avg", 0.0) for m in ("insert", "update", "delete"))
            ratio = read_avg / (read_avg + write_avg) if (read_avg + write_avg) else None

            row["peak_qps_approx"] = round(peak_total, 2)
            row["avg_qps_approx"] = round(avg_total, 2)
            row["read_write_ratio_pct"] = round(ratio * 100, 1) if ratio is not None else ""
        except Exception as exc:
            row["zabbix_error"] = str(exc)

        rows.append(row)

    fieldnames = [
        "instance", "mysql_version", "data_volume_gb", "approx_rows",
        "peak_qps_approx", "avg_qps_approx", "read_write_ratio_pct",
        "mysql_error", "zabbix_error",
    ]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nGravado em {args.output}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_discover = sub.add_parser("discover", help="lista item keys candidatos de QPS no Zabbix por host")
    p_discover.add_argument("--instances", default="instances.csv")
    p_discover.set_defaults(func=cmd_discover)

    p_collect = sub.add_parser("collect", help="coleta os dados e gera o CSV final")
    p_collect.add_argument("--instances", default="instances.csv")
    p_collect.add_argument("--map", default="zabbix_item_map.json")
    p_collect.add_argument("--days", type=int, default=30)
    p_collect.add_argument("--output", default="sizing_output.csv")
    p_collect.set_defaults(func=cmd_collect)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
