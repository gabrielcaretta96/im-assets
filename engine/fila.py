"""Ferramenta local do Claude para operar a fila do agendador.

  python3 engine/fila.py chave                 gera o par de chaves (imprime a privada UMA vez)
  python3 engine/fila.py config tokens.json    criptografa os tokens da Meta em config/tokens.enc
  python3 engine/fila.py agendar lote.json     criptografa posts e mídia na fila
  python3 engine/fila.py status                mostra o estado de cada post
  python3 engine/fila.py cancelar <id>         tira um post da fila (antes de publicar)

Formato de lote.json (lista de posts):
[{"id": "2026-10-28-1900-roubaar",
  "quando": "2026-10-28T19:00:00-03:00",
  "legenda": "texto padrão",
  "legendas": {"threads": "texto só do Threads"},          (opcional)
  "midia": ["/caminho/local/01.jpg", "/caminho/local/02.jpg"],
  "capa": "/caminho/local/capa.jpg",                          (opcional, reels)
  "destinos": [{"rede": "instagram", "tipo": "carrossel"},
               {"rede": "facebook", "tipo": "fotos"},
               {"rede": "threads", "tipo": "post"}]}]
Tipos: instagram imagem|carrossel|reels|story; facebook fotos|reels|story; threads post.
Depois de agendar: git add -A, commit e push. Nada é publicado antes do horário.
"""
import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(__file__))
from cripto import chave_publica, gerar_par, selar  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIPOS = {"instagram": {"imagem", "carrossel", "reels", "story"},
         "facebook": {"fotos", "reels", "story"},
         "threads": {"post"}}


def validar(post):
    assert re.fullmatch(r"[a-z0-9-]{4,80}", post["id"]), f"id inválido: {post['id']}"
    assert re.search(r"[+-]\d\d:\d\d$", post["quando"]), f"{post['id']}: horário sem fuso"
    for d in post["destinos"]:
        assert d["tipo"] in TIPOS[d["rede"]], f"{post['id']}: {d}"
        if d["rede"] == "instagram":
            assert post.get("midia"), f"{post['id']}: Instagram precisa de mídia"
            if d["tipo"] == "carrossel":
                assert 2 <= len(post["midia"]) <= 10, f"{post['id']}: carrossel com {len(post['midia'])}"
        if d["tipo"] == "reels":
            assert post["midia"][0].endswith(".mp4"), f"{post['id']}: reels precisa de mp4"
    for m in post.get("midia", []) + ([post["capa"]] if post.get("capa") else []):
        assert os.path.isfile(m), f"arquivo não existe: {m}"
        assert os.path.getsize(m) < 95 * 1024 * 1024, f"arquivo grande demais: {m}"


def agendar(arquivo):
    pub = chave_publica(RAIZ)
    posts = json.load(open(arquivo))
    for post in posts:
        validar(post)
    for post in posts:
        pasta = os.path.join(RAIZ, "midia", post["id"])
        shutil.rmtree(pasta, ignore_errors=True)
        os.makedirs(pasta)
        nomes = []
        for m in post.get("midia", []):
            nome = os.path.basename(m)
            open(os.path.join(pasta, nome + ".enc"), "wb").write(selar(open(m, "rb").read(), pub))
            nomes.append(nome)
        spec = {k: v for k, v in post.items() if k not in ("midia", "capa")}
        spec["midia"] = nomes
        if post.get("capa"):
            capa = os.path.basename(post["capa"])
            open(os.path.join(pasta, capa + ".enc"), "wb").write(selar(open(post["capa"], "rb").read(), pub))
            spec["capa"] = capa
        os.makedirs(os.path.join(RAIZ, "fila"), exist_ok=True)
        open(os.path.join(RAIZ, "fila", post["id"] + ".enc"), "wb").write(selar(json.dumps(spec).encode(), pub))
        print("na fila:", post["id"], post["quando"], [f"{d['rede']}:{d['tipo']}" for d in post["destinos"]])


def status():
    ids = sorted({os.path.basename(p)[:-4] for p in glob.glob(os.path.join(RAIZ, "fila", "*.enc"))} |
                 {os.path.basename(p)[:-5] for p in glob.glob(os.path.join(RAIZ, "estado", "*.json"))
                  if not os.path.basename(p).startswith("_")})
    for i in ids:
        try:
            est = json.load(open(os.path.join(RAIZ, "estado", i + ".json")))
        except FileNotFoundError:
            est = {}
        print(i, json.dumps(est.get("destinos", {}), ensure_ascii=False),
              ("simulação: " + json.dumps(est["simulacao"], ensure_ascii=False)) if est.get("simulacao") else "")


def main():
    cmd = sys.argv[1]
    if cmd == "chave":
        priv, pub = gerar_par()
        open(os.path.join(RAIZ, "engine", "chave_publica.txt"), "w").write(pub + "\n")
        print(priv)
    elif cmd == "config":
        os.makedirs(os.path.join(RAIZ, "config"), exist_ok=True)
        dados = open(sys.argv[2], "rb").read()
        json.loads(dados)
        open(os.path.join(RAIZ, "config", "tokens.enc"), "wb").write(selar(dados, chave_publica(RAIZ)))
        print("config/tokens.enc atualizado")
    elif cmd == "agendar":
        agendar(sys.argv[2])
    elif cmd == "status":
        status()
    elif cmd == "cancelar":
        pid = sys.argv[2]
        for p in (os.path.join(RAIZ, "fila", pid + ".enc"),):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(os.path.join(RAIZ, "midia", pid), ignore_errors=True)
        print("cancelado:", pid)


if __name__ == "__main__":
    main()
