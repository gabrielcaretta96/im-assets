"""Teste do ciclo com uma Meta falsa (sem rede)."""
import json, os, shutil, subprocess, sys, tempfile, datetime as dt

AQUI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(AQUI))

def preparar():
    tmp = tempfile.mkdtemp()
    shutil.copytree(os.path.join(REPO, "engine"), os.path.join(tmp, "engine"))
    subprocess.run(["git", "init", "-q", tmp], check=True)
    sys.path.insert(0, os.path.join(tmp, "engine"))
    import cripto
    priv, pub = cripto.gerar_par()
    open(os.path.join(tmp, "engine", "chave_publica.txt"), "w").write(pub)
    return tmp, priv

def main():
    tmp, priv = preparar()
    import importlib
    fila = importlib.import_module("fila"); fila.RAIZ = tmp
    ag = importlib.import_module("agendador"); ag.RAIZ = tmp
    os.environ["GITHUB_REPOSITORY"] = "x/y"
    os.environ["AGENDADOR_CHAVE"] = priv
    m = os.path.join(tmp, "m"); os.makedirs(m)
    for n in ["a.jpg", "b.jpg", "v.mp4", "c.jpg"]:
        open(os.path.join(m, n), "wb").write(os.urandom(100))
    cfg = {"page_token": "P", "ig_user_id": "IG", "fb_page_id": "PG", "threads_token": "T", "threads_user_id": "TU",
           "threads_expira": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=50)).isoformat()}
    json.dump(cfg, open(os.path.join(tmp, "t.json"), "w"))
    sys.argv = ["", "config", os.path.join(tmp, "t.json")]; fila.main()
    passado = (dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))) - dt.timedelta(minutes=5)).isoformat(timespec="seconds")
    futuro = (dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))) + dt.timedelta(days=1)).isoformat(timespec="seconds")
    lote = [
        {"id": "p1-carrossel", "quando": passado, "legenda": "L1", "legendas": {"threads": "T1"},
         "midia": [os.path.join(m, "a.jpg"), os.path.join(m, "b.jpg")],
         "destinos": [{"rede": "instagram", "tipo": "carrossel"}, {"rede": "facebook", "tipo": "fotos"}, {"rede": "threads", "tipo": "post"}]},
        {"id": "p2-reels", "quando": passado, "legenda": "L2", "midia": [os.path.join(m, "v.mp4")], "capa": os.path.join(m, "c.jpg"),
         "destinos": [{"rede": "instagram", "tipo": "reels"}, {"rede": "facebook", "tipo": "reels"}]},
        {"id": "p3-story", "quando": passado, "midia": [os.path.join(m, "c.jpg")],
         "destinos": [{"rede": "instagram", "tipo": "story"}, {"rede": "facebook", "tipo": "story"}]},
        {"id": "p4-futuro", "quando": futuro, "legenda": "F", "midia": [os.path.join(m, "a.jpg")],
         "destinos": [{"rede": "instagram", "tipo": "imagem"}]},
    ]
    json.dump(lote, open(os.path.join(tmp, "lote.json"), "w"))
    sys.argv = ["", "agendar", os.path.join(tmp, "lote.json")]; fila.main()

    chamadas = []
    def chamar(metodo, url, **kw):
        dados = kw.get("data") or kw.get("params") or {}
        chamadas.append((metodo, url.replace("https://", ""), {k: v for k, v in dados.items() if k != "access_token"}))
        if "fields" in dados:
            return {dados["fields"]: "FINISHED"}
        if url.endswith("video_reels") and dados.get("upload_phase") == "start":
            return {"video_id": "VID"}
        return {"id": f"id{len(chamadas)}", "post_id": "s1"}
    ag.chamar = chamar
    ag.enviar_commit = lambda msg: None
    ag.git = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    class R: status_code = 200
    ag.requests.head = lambda *a, **k: R()
    ag.time.sleep = lambda s: None
    ag.ciclo()

    for pid in ["p1-carrossel", "p2-reels", "p3-story"]:
        est = json.load(open(os.path.join(tmp, "estado", pid + ".json")))
        st = {k: v["status"] for k, v in est["destinos"].items()}
        assert all(v == "publicado" for v in st.values()), (pid, est)
        print("OK", pid, st)
    assert not os.path.exists(os.path.join(tmp, "estado", "p4-futuro.json")), "futuro não pode publicar"
    urls = [c[1] for c in chamadas]
    assert any("media_publish" in u for u in urls)
    assert any(c[2].get("media_type") == "CAROUSEL" for c in chamadas)
    assert any(c[2].get("media_type") == "REELS" and "cover_url" in c[2] for c in chamadas)
    assert any("attached_media[1]" in c[2] for c in chamadas)
    assert any("rupload.facebook.com" in u for u in urls)
    assert any("threads_publish" in u for u in urls)
    assert any(c[2].get("text") == "T1" for c in chamadas), "legenda específica do Threads"
    assert any("photo_stories" in u for u in urls)
    assert not os.path.exists(os.path.join(tmp, "live")) or not os.listdir(os.path.join(tmp, "live"))
    print("chamadas:", len(chamadas))

    # falha no Instagram segura os próximos posts
    tmp2 = tmp
    for f in os.listdir(os.path.join(tmp2, "estado")):
        os.remove(os.path.join(tmp2, "estado", f))
    def chamar_falha(metodo, url, **kw):
        if "IG/media" in url and "media_publish" not in url:
            raise ag.ErroAPI("400 falha simulada")
        return chamar(metodo, url, **kw)
    ag.chamar = chamar_falha
    ag.ciclo()
    e1 = json.load(open(os.path.join(tmp2, "estado", "p1-carrossel.json")))
    assert e1["destinos"]["instagram:carrossel"]["status"] == "erro"
    assert not os.path.exists(os.path.join(tmp2, "estado", "p2-reels.json")), "deveria travar após falha do IG"
    print("OK trava após falha do Instagram")
    print("TODOS OS TESTES PASSARAM")

main()
