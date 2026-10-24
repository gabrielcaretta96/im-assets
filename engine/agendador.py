"""Agendador da INTENT: publica a fila no Instagram, Facebook e Threads pela API oficial da Meta.

Roda no GitHub Actions a cada 15 minutos. Fluxo de cada ciclo:
  1. abre a fila criptografada (fila/*.enc) e pega os posts cujo horário já passou;
  2. publica a mídia do post em live/<aleatório>/ (público por poucos minutos);
  3. publica em cada rede na ordem do horário, registra o resultado em estado/<id>.json;
  4. apaga live/ e grava o estado.

Variáveis de ambiente:
  AGENDADOR_CHAVE  chave privada (segredo do repositório)
  SIMULAR=1        cria tudo na Meta sem publicar (teste)
  GITHUB_REPOSITORY  dono/repo (vem do Actions)
"""
import datetime as dt
import glob
import json
import os
import secrets
import subprocess
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(__file__))
from cripto import abrir, chave_publica, selar  # noqa: E402

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSAO = "v23.0"
GRAPH = f"https://graph.facebook.com/{VERSAO}"
THREADS = "https://graph.threads.net/v1.0"
MAX_TENTATIVAS = 3
SIMULAR = os.environ.get("SIMULAR") == "1"


class ErroAPI(Exception):
    pass


# ---------------------------------------------------------------- utilidades
def agora():
    return dt.datetime.now(dt.timezone.utc)


def log(*a):
    print(f"[{agora():%H:%M:%S}]", *a, flush=True)


def git(*args, check=True):
    return subprocess.run(["git", "-C", RAIZ, *args], check=check, capture_output=True, text=True)


def enviar_commit(msg):
    git("add", "-A")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    git("commit", "-qm", msg)
    for _ in range(4):
        if git("push", "-q", "origin", "HEAD", check=False).returncode == 0:
            return
        git("pull", "-q", "--rebase", "origin", "main", check=False)
        time.sleep(3)
    raise RuntimeError("não consegui dar push")


def chamar(metodo, url, **kw):
    for tentativa in range(3):
        r = requests.request(metodo, url, timeout=120, **kw)
        if r.status_code < 500:
            break
        time.sleep(5 * (tentativa + 1))
    try:
        corpo = r.json()
    except ValueError:
        corpo = {"bruto": r.text[:300]}
    if r.status_code >= 400 or "error" in corpo:
        erro = corpo.get("error", corpo)
        msg = erro.get("message", str(erro)) if isinstance(erro, dict) else str(erro)
        raise ErroAPI(f"{r.status_code} {msg}"[:400])
    return corpo


def esperar_status(url, token, campo, prontos=("FINISHED",), limite=600):
    fim = time.time() + limite
    while time.time() < fim:
        st = chamar("GET", url, params={"fields": campo, "access_token": token}).get(campo)
        if st in prontos:
            return
        if st in ("ERROR", "EXPIRED"):
            raise ErroAPI(f"processamento falhou: {st}")
        time.sleep(10)
    raise ErroAPI("tempo esgotado esperando processamento")


# ---------------------------------------------------------------- arquivos
def ler_config(priv):
    with open(os.path.join(RAIZ, "config", "tokens.enc"), "rb") as f:
        return json.loads(abrir(f.read(), priv))


def salvar_config(cfg):
    with open(os.path.join(RAIZ, "config", "tokens.enc"), "wb") as f:
        f.write(selar(json.dumps(cfg).encode(), chave_publica(RAIZ)))


def ler_fila(priv):
    posts = []
    for p in glob.glob(os.path.join(RAIZ, "fila", "*.enc")):
        with open(p, "rb") as f:
            post = json.loads(abrir(f.read(), priv))
        post["_quando"] = dt.datetime.fromisoformat(post["quando"])
        posts.append(post)
    return sorted(posts, key=lambda p: (p["_quando"], p["id"]))


def caminho_estado(pid):
    return os.path.join(RAIZ, "estado", f"{pid}.json")


def ler_estado(pid):
    try:
        with open(caminho_estado(pid)) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"id": pid, "destinos": {}}


def salvar_estado(est):
    os.makedirs(os.path.join(RAIZ, "estado"), exist_ok=True)
    with open(caminho_estado(est["id"]), "w") as f:
        json.dump(est, f, ensure_ascii=False, indent=1)


def chave_destino(d):
    return f"{d['rede']}:{d['tipo']}"


def pendente(est, d):
    s = est["destinos"].get(chave_destino(d), {})
    return s.get("status") not in ("publicado", "falhou")


def publicar_midia(post, priv):
    """Abre a mídia do post em live/<aleatório>/ e devolve {nome: url pública}."""
    repo = os.environ["GITHUB_REPOSITORY"]
    pasta = f"live/{secrets.token_hex(6)}"
    os.makedirs(os.path.join(RAIZ, pasta))
    nomes = list(post.get("midia", [])) + ([post["capa"]] if post.get("capa") else [])
    urls = {}
    for nome in nomes:
        with open(os.path.join(RAIZ, "midia", post["id"], nome + ".enc"), "rb") as f:
            dados = abrir(f.read(), priv)
        with open(os.path.join(RAIZ, pasta, nome), "wb") as f:
            f.write(dados)
        urls[nome] = f"https://raw.githubusercontent.com/{repo}/main/{pasta}/{nome}"
    enviar_commit(f"live {post['id']}")
    for url in urls.values():  # espera o CDN do GitHub servir os arquivos
        for _ in range(30):
            if requests.head(url, timeout=30).status_code == 200:
                break
            time.sleep(4)
        else:
            raise ErroAPI(f"mídia não ficou pública a tempo: {url.rsplit('/', 1)[-1]}")
    return pasta, urls


def apagar_midia_publica(pasta):
    git("rm", "-rq", pasta, check=False)
    subprocess.run(["rm", "-rf", os.path.join(RAIZ, pasta)])


# ---------------------------------------------------------------- Instagram
def instagram(cfg, d, post, urls, legenda):
    ig, tok = cfg["ig_user_id"], cfg["page_token"]
    base = f"{GRAPH}/{ig}/media"
    midia = [urls[n] for n in post["midia"]]
    tipo = d["tipo"]

    def criar(**campos):
        return chamar("POST", base, data={**campos, "access_token": tok})["id"]

    if tipo == "imagem":
        cid = criar(image_url=midia[0], caption=legenda)
    elif tipo == "carrossel":
        filhos = [criar(image_url=u, is_carousel_item="true") for u in midia]
        cid = criar(media_type="CAROUSEL", children=",".join(filhos), caption=legenda)
    elif tipo == "reels":
        campos = dict(media_type="REELS", video_url=midia[0], caption=legenda, share_to_feed="true")
        if post.get("capa"):
            campos["cover_url"] = urls[post["capa"]]
        cid = criar(**campos)
    elif tipo == "story":
        campo = "video_url" if midia[0].endswith(".mp4") else "image_url"
        cid = criar(media_type="STORIES", **{campo: midia[0]})
    else:
        raise ErroAPI(f"tipo de Instagram desconhecido: {tipo}")
    esperar_status(f"{GRAPH}/{cid}", tok, "status_code")
    if SIMULAR:
        return f"simulado:{cid}"
    return chamar("POST", f"{GRAPH}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok})["id"]


# ---------------------------------------------------------------- Facebook
def facebook(cfg, d, post, urls, legenda):
    pg, tok = cfg["fb_page_id"], cfg["page_token"]
    midia = [urls[n] for n in post["midia"]]
    tipo = d["tipo"]

    def foto_oculta(url):
        return chamar("POST", f"{GRAPH}/{pg}/photos", data={"url": url, "published": "false", "access_token": tok})["id"]

    if SIMULAR:  # sobe uma foto oculta e apaga: valida token, permissão e acesso à mídia
        imagens = [u for u in midia if not u.endswith(".mp4")] or ([urls[post["capa"]]] if post.get("capa") else [])
        if imagens:
            chamar("DELETE", f"{GRAPH}/{foto_oculta(imagens[0])}", params={"access_token": tok})
        return "simulado"
    if tipo == "fotos":
        if len(midia) == 1:
            return chamar("POST", f"{GRAPH}/{pg}/photos", data={"url": midia[0], "message": legenda, "access_token": tok})["id"]
        dados = {"message": legenda, "access_token": tok}
        for i, u in enumerate(midia):
            dados[f"attached_media[{i}]"] = json.dumps({"media_fbid": foto_oculta(u)})
        return chamar("POST", f"{GRAPH}/{pg}/feed", data=dados)["id"]
    if tipo == "reels":
        vid = chamar("POST", f"{GRAPH}/{pg}/video_reels", data={"upload_phase": "start", "access_token": tok})["video_id"]
        chamar("POST", f"https://rupload.facebook.com/video-upload/{VERSAO}/{vid}",
               headers={"Authorization": f"OAuth {tok}", "file_url": midia[0]})
        chamar("POST", f"{GRAPH}/{pg}/video_reels", data={
            "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
            "description": legenda, "access_token": tok})
        return vid
    if tipo == "story":
        return chamar("POST", f"{GRAPH}/{pg}/photo_stories",
                      data={"photo_id": foto_oculta(midia[0]), "access_token": tok}).get("post_id", "ok")
    raise ErroAPI(f"tipo de Facebook desconhecido: {tipo}")


# ---------------------------------------------------------------- Threads
def threads(cfg, d, post, urls, legenda):
    uid, tok = cfg["threads_user_id"], cfg["threads_token"]
    base = f"{THREADS}/{uid}/threads"
    midia = [urls[n] for n in post.get("midia", [])]

    def criar(**campos):
        return chamar("POST", base, data={**campos, "access_token": tok})["id"]

    def item(u, **extra):
        return dict(media_type="VIDEO", video_url=u, **extra) if u.endswith(".mp4") else dict(media_type="IMAGE", image_url=u, **extra)

    if not midia:
        cid = criar(media_type="TEXT", text=legenda)
    elif len(midia) == 1:
        cid = criar(text=legenda, **item(midia[0]))
    else:
        filhos = [criar(**item(u, is_carousel_item="true")) for u in midia[:20]]
        for f in filhos:
            esperar_status(f"{THREADS}/{f}", tok, "status")
        cid = criar(media_type="CAROUSEL", children=",".join(filhos), text=legenda)
    esperar_status(f"{THREADS}/{cid}", tok, "status")
    if SIMULAR:
        return f"simulado:{cid}"
    return chamar("POST", f"{THREADS}/{uid}/threads_publish", data={"creation_id": cid, "access_token": tok})["id"]


def renovar_threads(cfg):
    expira = dt.datetime.fromisoformat(cfg.get("threads_expira", "1970-01-01T00:00:00+00:00"))
    if expira - agora() > dt.timedelta(days=15):
        return False
    r = chamar("GET", "https://graph.threads.net/refresh_access_token",
               params={"grant_type": "th_refresh_token", "access_token": cfg["threads_token"]})
    cfg["threads_token"] = r["access_token"]
    cfg["threads_expira"] = (agora() + dt.timedelta(seconds=int(r.get("expires_in", 5184000)))).isoformat()
    return True


def completar_config(cfg):
    """Primeira execução: a partir do token de usuário de longa duração, descobre a Página, o token
    da Página (não expira), o Instagram ligado e o id do Threads. Devolve True se mudou algo."""
    mudou = False
    if cfg.get("user_token") and not cfg.get("page_token"):
        contas = chamar("GET", f"{GRAPH}/me/accounts", params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "access_token": cfg["user_token"]}).get("data", [])
        alvo = cfg.get("pagina_nome", "").lower()
        escolhida = next((c for c in contas if alvo and alvo in c["name"].lower()), None) or \
            next((c for c in contas if c.get("instagram_business_account")), None)
        if not escolhida:
            raise ErroAPI(f"nenhuma Página com Instagram ligado: {[c['name'] for c in contas]}")
        cfg.update(page_token=escolhida["access_token"], fb_page_id=escolhida["id"],
                   ig_user_id=escolhida["instagram_business_account"]["id"],
                   ig_username=escolhida["instagram_business_account"].get("username"),
                   pagina_nome=escolhida["name"])
        cfg.pop("user_token")
        log("config: Página", escolhida["name"], "| Instagram", cfg["ig_username"])
        mudou = True
    if cfg.get("threads_token") and not cfg.get("threads_user_id"):
        eu = chamar("GET", f"{THREADS}/me", params={"fields": "id,username", "access_token": cfg["threads_token"]})
        cfg["threads_user_id"], cfg["threads_username"] = eu["id"], eu.get("username")
        cfg.setdefault("threads_expira", (agora() + dt.timedelta(days=58)).isoformat())
        log("config: Threads", cfg["threads_username"])
        mudou = True
    return mudou


REDES = {"instagram": instagram, "facebook": facebook, "threads": threads}


# ---------------------------------------------------------------- ciclo
def ciclo():
    priv = os.environ["AGENDADOR_CHAVE"]
    cfg = ler_config(priv)
    if completar_config(cfg):
        salvar_config(cfg)
        enviar_commit("config completada")
    try:
        if not SIMULAR and cfg.get("threads_token") and renovar_threads(cfg):
            salvar_config(cfg)
            enviar_commit("token do Threads renovado")
            log("token do Threads renovado")
    except Exception as e:  # noqa: BLE001
        log("ERRO ao renovar token do Threads:", str(e)[:200])

    alvo = os.environ.get("SIMULAR_POST")
    vencidos = [p for p in ler_fila(priv)
                if (SIMULAR and (not alvo or p["id"] == alvo)) or (not SIMULAR and p["_quando"] <= agora())]
    for post in vencidos:
        est = ler_estado(post["id"])
        destinos = [d for d in post["destinos"] if pendente(est, d)]
        if not destinos:
            continue
        log("post", post["id"], [chave_destino(d) for d in destinos])
        pasta, urls = publicar_midia(post, priv)
        travar = False
        try:
            for d in destinos:
                chave = chave_destino(d)
                legenda = post.get("legendas", {}).get(d["rede"], post.get("legenda", ""))
                atual = est["destinos"].get(chave, {})
                try:
                    rid = REDES[d["rede"]](cfg, d, post, urls, legenda)
                    if SIMULAR:
                        est.setdefault("simulacao", {})[chave] = {"ok": True, "em": agora().isoformat()}
                    else:
                        est["destinos"][chave] = {"status": "publicado", "id": rid, "em": agora().isoformat()}
                    log("  ok", chave)
                except Exception as e:  # noqa: BLE001
                    if SIMULAR:
                        est.setdefault("simulacao", {})[chave] = {"ok": False, "erro": str(e)[:400], "em": agora().isoformat()}
                    else:
                        n = atual.get("tentativas", 0) + 1
                        est["destinos"][chave] = {"status": "falhou" if n >= MAX_TENTATIVAS else "erro",
                                                  "tentativas": n, "erro": str(e)[:400], "em": agora().isoformat()}
                        if d["rede"] == "instagram" and n < MAX_TENTATIVAS:
                            travar = True  # segura os próximos posts para não embaralhar o grid
                    log("  ERRO", chave, str(e)[:200])
        finally:
            apagar_midia_publica(pasta)
            salvar_estado(est)
            if not SIMULAR and not any(pendente(est, d) for d in post["destinos"]):
                git("rm", "-rq", f"fila/{post['id']}.enc", f"midia/{post['id']}", check=False)
            enviar_commit(f"estado {post['id']}")
        if travar:
            log("Instagram falhou; próximos posts ficam para o próximo ciclo")
            break

    batida = os.path.join(RAIZ, "estado", "_batida.json")
    hoje = agora().date().isoformat()
    try:
        ultima = json.load(open(batida)).get("dia")
    except (FileNotFoundError, ValueError):
        ultima = None
    if ultima != hoje:
        os.makedirs(os.path.dirname(batida), exist_ok=True)
        json.dump({"dia": hoje, "em": agora().isoformat()}, open(batida, "w"))
        enviar_commit("batida diária")


if __name__ == "__main__":
    ciclo()
