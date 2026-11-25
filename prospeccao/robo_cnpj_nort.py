# -*- coding: utf-8 -*-
"""
Robô de Prospecção Nort Contabilidade  (versão 2)
-------------------------------------------------
Gera duas bases para o app Prospecção Nort, com telefone e e-mail da Receita:

  1) EMPRESAS NOVAS: empresas ativas abertas desde 1º de janeiro
     (fonte: Dados Abertos do CNPJ, Receita Federal, atualização mensal)
  2) DÍVIDA ATIVA PGFN: empresas ativas com débito em cobrança na dívida ativa da União
     (fonte: Dados Abertos da PGFN, atualização trimestral)

Todo dia (agendado à meia-noite) ele confere se saiu base nova. Se não saiu, encerra.

Uso:
  python robo_cnpj_nort.py                    -> confere e gera o que for novo
  python robo_cnpj_nort.py --forcar           -> gera as duas bases de novo
  python robo_cnpj_nort.py --desde 2026-01-01 -> data inicial das empresas novas
  python robo_cnpj_nort.py --so-novas  |  --so-pgfn
  python robo_cnpj_nort.py --pgfn-valor-min 5000      (padrão: 1000)
  python robo_cnpj_nort.py --pgfn-ufs CE,PI,MA        (padrão: todos os estados)
  python robo_cnpj_nort.py --pasta-receita "C:\\zips"  --pasta-pgfn "C:\\zips_pgfn"   (arquivos já baixados)

Só usa bibliotecas que vêm com o Python.
"""
import argparse, base64, csv, datetime as dt, io, json, os, re, shutil, sqlite3, sys, time, zipfile
import urllib.request, urllib.error, urllib.parse
from xml.etree import ElementTree

SHARE_TOKEN = "gn672Ad4CF8N6TK"  # código público da pasta da Receita (pode mudar)
WEBDAV = "https://arquivos.receitafederal.gov.br/public.php/webdav/Dados/Cadastros/CNPJ"
DAV_FILES = "https://arquivos.receitafederal.gov.br/public.php/dav/files/{tok}/Dados/Cadastros/CNPJ"
PGFN = "https://dadosabertos.pgfn.gov.br"
PGFN_ARQS = {"Dados_abertos_Nao_Previdenciario.zip": 1, "Dados_abertos_Previdenciario.zip": 2, "Dados_abertos_FGTS.zip": 4}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RoboProspeccaoNort/2.0"}
LINHAS_POR_PARTE = int(os.environ.get("NORT_PARTE", "40000"))

csv.field_size_limit(10_000_000)


# ---------------------------------------------------------------- pastas e log
def area_de_trabalho():
    home = os.path.expanduser("~")
    for c in [os.path.join(home, "OneDrive", "Desktop"), os.path.join(home, "OneDrive", "Área de Trabalho"),
              os.path.join(home, "Desktop"), os.path.join(home, "Área de Trabalho")]:
        if os.path.isdir(c):
            return c
    return home


SAIDA = os.environ.get("NORT_SAIDA") or os.path.join(area_de_trabalho(), "Prospeccao Nort")
TRAB = os.environ.get("NORT_TRAB") or os.path.join(SAIDA, "_trabalho")
ARQ_ESTADO = os.path.join(SAIDA, "_controle.json")
ARQ_LOG = os.path.join(SAIDA, "_log.txt")


def log(msg):
    linha = f"{dt.datetime.now():%d/%m/%Y %H:%M:%S}  {msg}"
    print(linha, flush=True)
    try:
        with open(ARQ_LOG, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def ler_estado():
    try:
        with open(ARQ_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def salvar_estado(e):
    with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
        json.dump(e, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- internet
def _auth():
    return "Basic " + base64.b64encode(f"{SHARE_TOKEN}:".encode()).decode()


def propfind(url):
    req = urllib.request.Request(url, method="PROPFIND", headers={"Depth": "1", "Authorization": _auth(), **UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        root = ElementTree.fromstring(r.read())
    ns = {"d": "DAV:"}
    out = []
    for resp in root.findall("d:response", ns):
        href = resp.find("d:href", ns).text or ""
        tam = resp.find(".//d:getcontentlength", ns)
        out.append((urllib.parse.unquote(href), int(tam.text) if tam is not None and tam.text else 0))
    return out


def receita_ultima():
    itens = propfind(WEBDAV + "/")
    meses = sorted({m.group(1) for h, _ in itens for m in [re.search(r"(\d{4}-\d{2})/?$", h)] if m})
    if not meses:
        raise RuntimeError("não encontrei as pastas mensais")
    mes = meses[-1]
    arqs = {}
    for h, tam in propfind(f"{WEBDAV}/{mes}/"):
        m = re.search(r"/([^/]+\.zip)$", h, re.I)
        if m and re.match(r"(Estabelecimentos|Empresas|Simples|Cnaes|Municipios)", m.group(1), re.I):
            arqs[m.group(1)] = tam
    return mes, arqs


def pgfn_ultima():
    with urllib.request.urlopen(urllib.request.Request(PGFN + "/", headers=UA), timeout=60) as r:
        html = r.read().decode("utf-8", "ignore")
    tris = sorted(set(re.findall(r"(\d{4}_trimestre_\d{2})", html)))
    if not tris:
        raise RuntimeError("não encontrei os trimestres da PGFN")
    tri = tris[-1]
    arqs = {}
    for nome in PGFN_ARQS:
        try:
            req = urllib.request.Request(f"{PGFN}/{tri}/{nome}", method="HEAD", headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                arqs[nome] = int(r.headers.get("Content-Length") or 0)
        except urllib.error.URLError:
            log(f"   aviso: {nome} não encontrado em {tri}")
    return tri, arqs


def baixar(urls, destino, tam):
    if os.path.exists(destino) and tam and os.path.getsize(destino) == tam:
        log(f"   {os.path.basename(destino)} já baixado")
        return destino
    for tentativa in range(3):
        for url, hdr in urls:
            try:
                log(f"   baixando {os.path.basename(destino)} ({tam / 1e6:,.0f} MB)...")
                with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **hdr}), timeout=180) as r, \
                        open(destino + ".parcial", "wb") as f:
                    shutil.copyfileobj(r, f, 1024 * 1024)
                os.replace(destino + ".parcial", destino)
                return destino
            except (urllib.error.URLError, OSError) as e:
                log(f"   falhou ({e}); tentando de novo")
                time.sleep(15)
    raise RuntimeError(f"não consegui baixar {os.path.basename(destino)}")


# ---------------------------------------------------------------- leitura
def zip_textos(caminho):
    with zipfile.ZipFile(caminho) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            with z.open(info) as bruto:
                yield io.TextIOWrapper(bruto, encoding="latin-1", newline="")


def zip_linhas_csv(caminho):
    for t in zip_textos(caminho):
        yield from csv.reader(t, delimiter=";", quotechar='"')


def tabela_simples(caminho):
    return {r[0].strip(): r[1].strip() for r in zip_linhas_csv(caminho) if len(r) >= 2}


def so_digitos(s):
    return re.sub(r"[^0-9A-Za-z]", "", s or "").upper()


def fone(ddd, num):
    ddd, num = re.sub(r"\D", "", ddd or ""), re.sub(r"\D", "", num or "")
    if not num:
        return ""
    return ddd + num if ddd else num


def valor(s):
    s = (s or "").strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def data_br(s):
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s or "")
    if not m or m.group(3) == "1000":
        return ""
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"


def conectar():
    os.makedirs(TRAB, exist_ok=True)
    caminho = os.path.join(TRAB, "trabalho.db")
    if os.path.exists(caminho):
        os.remove(caminho)
    db = sqlite3.connect(caminho)
    db.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=MEMORY;
      CREATE TABLE pg(cnpj TEXT PRIMARY KEY, valor REAL, qtd INT, tipos INT, ajuiz INT, desde TEXT);
      CREATE TABLE est(cnpj TEXT PRIMARY KEY, basico TEXT, fant TEXT, abert TEXT, cnae TEXT, uf TEXT, mun TEXT,
                       bairro TEXT, ende TEXT, cep TEXT, tel1 TEXT, tel2 TEXT, email TEXT, matriz INT, novo INT, pg INT);
      CREATE TABLE emp(basico TEXT PRIMARY KEY, razao TEXT, porte TEXT);
      CREATE TABLE simp(basico TEXT PRIMARY KEY, simples INT, mei INT);
    """)
    return db


# ---------------------------------------------------------------- PGFN
def pgfn_agregar(db, arquivos, ufs):
    """arquivos: lista (caminho, mascara)"""
    col = {}
    for caminho, mascara in arquivos:
        log(f"Lendo dívida ativa: {os.path.basename(caminho)}")
        n = 0
        for texto in zip_textos(caminho):
            leitor = csv.reader(texto, delimiter=";", quotechar='"')
            lote = {}
            for row in leitor:
                if row and "CPF_CNPJ" in [c.strip().upper() for c in row]:
                    col = {c.strip().upper(): i for i, c in enumerate(row)}
                    continue
                if not col or len(row) < len(col) - 1:
                    continue
                g = lambda k: row[col[k]].strip() if k in col and col[k] < len(row) else ""
                if "jur" not in g("TIPO_PESSOA").lower():
                    continue
                if g("TIPO_DEVEDOR") and "principal" not in g("TIPO_DEVEDOR").lower():
                    continue
                sit = (g("TIPO_SITUACAO_INSCRICAO") + " " + g("SITUACAO_INSCRICAO")).lower()
                if any(x in sit for x in ("benef", "garant", "suspens", "parcel", "negocia", "extint")):
                    continue
                uf = g("UF_DEVEDOR") or g("UF_UNIDADE_RESPONSAVEL")
                if ufs and uf not in ufs:
                    continue
                cnpj = so_digitos(g("CPF_CNPJ"))
                if len(cnpj) != 14:
                    continue
                v = valor(g("VALOR_CONSOLIDADO"))
                aj = 1 if g("INDICADOR_AJUIZADO").upper().startswith("S") else 0
                d = data_br(g("DATA_INSCRICAO"))
                a = lote.get(cnpj)
                if a:
                    a[0] += v; a[1] += 1; a[3] |= aj
                    if d and (not a[4] or d < a[4]):
                        a[4] = d
                else:
                    lote[cnpj] = [v, 1, mascara, aj, d]
                n += 1
                if len(lote) >= 300000:
                    _pg_gravar(db, lote); lote = {}
            _pg_gravar(db, lote)
        log(f"   {n:,} inscrições de empresas em cobrança")


def _pg_gravar(db, lote):
    db.executemany("""INSERT INTO pg VALUES(?,?,?,?,?,?) ON CONFLICT(cnpj) DO UPDATE SET
        valor=valor+excluded.valor, qtd=qtd+excluded.qtd, tipos=tipos|excluded.tipos, ajuiz=max(ajuiz,excluded.ajuiz),
        desde=CASE WHEN desde='' OR (excluded.desde<>'' AND excluded.desde<desde) THEN excluded.desde ELSE desde END""",
                   [(k, *v) for k, v in lote.items()])
    db.commit()


# ---------------------------------------------------------------- Receita
def receita_processar(db, receita, desde, gerar_novas, cnpjs_pg, incluir_filiais):
    def grupo(p):
        return sorted(v for k, v in receita.items() if k.lower().startswith(p.lower()))

    desde_s = desde.strftime("%Y%m%d")
    marcas = set()
    d = desde.replace(day=1)
    while d <= dt.date.today():
        marcas.add(f'"{d:%Y%m}')
        d = (d + dt.timedelta(days=32)).replace(day=1)
    marcas = tuple(marcas)
    log(f"Lendo estabelecimentos (novas desde {desde:%d/%m/%Y}: {'sim' if gerar_novas else 'não'}; "
        f"devedores PGFN procurados: {len(cnpjs_pg):,})")
    sql = "INSERT OR REPLACE INTO est VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    for caminho in grupo("Estabelecimentos"):
        lote, nn, npg = [], 0, 0
        for texto in zip_textos(caminho):
            for linha in texto:
                chave = (linha[1:9] + linha[12:16] + linha[19:21]).upper()
                e_pg = chave in cnpjs_pg
                e_novo = gerar_novas and any(m in linha for m in marcas)
                if not e_pg and not e_novo:
                    continue
                try:
                    r = next(csv.reader([linha], delimiter=";", quotechar='"'))
                except StopIteration:
                    continue
                if len(r) < 28 or r[5] != "02":
                    continue
                novo = 1 if (e_novo and r[10] >= desde_s and (r[3] == "1" or incluir_filiais)) else 0
                pg = 1 if e_pg else 0
                if not novo and not pg:
                    continue
                end = " ".join(x for x in (r[13], r[14]) if x).strip()
                if r[15]:
                    end += ", " + r[15]
                if r[16]:
                    end += " - " + r[16]
                lote.append(((r[0] + r[1] + r[2]).upper(), r[0], r[4].strip(),
                             f"{r[10][:4]}-{r[10][4:6]}-{r[10][6:8]}", r[11], r[19], r[20], r[17].strip(), end.strip(),
                             r[18], fone(r[21], r[22]), fone(r[23], r[24]), r[27].strip().lower(),
                             1 if r[3] == "1" else 0, novo, pg))
                nn += novo; npg += pg
                if len(lote) >= 100000:
                    db.executemany(sql, lote); db.commit(); lote = []
        db.executemany(sql, lote); db.commit()
        log(f"   {os.path.basename(caminho)}: {nn:,} novas, {npg:,} devedoras")

    basicos = {b for (b,) in db.execute("SELECT DISTINCT basico FROM est")}
    log(f"Buscando razão social e porte de {len(basicos):,} empresas...")
    for caminho in grupo("Empresas"):
        lote = [(r[0], r[1].strip(), r[5]) for r in zip_linhas_csv(caminho) if r and r[0] in basicos and len(r) >= 6]
        db.executemany("INSERT OR REPLACE INTO emp VALUES(?,?,?)", lote); db.commit()
    log("Buscando opção pelo Simples/MEI...")
    for caminho in grupo("Simples"):
        lote = [(r[0], 1 if r[1] == "S" else 0, 1 if r[4] == "S" else 0)
                for r in zip_linhas_csv(caminho) if r and r[0] in basicos and len(r) >= 5]
        db.executemany("INSERT OR REPLACE INTO simp VALUES(?,?,?)", lote); db.commit()
    cnaes = tabela_simples(grupo("Cnaes")[0]) if grupo("Cnaes") else {}
    muns = tabela_simples(grupo("Municipios")[0]) if grupo("Municipios") else {}
    return cnaes, muns


# ---------------------------------------------------------------- saída
CAMPOS = ["cnpj", "razao", "fantasia", "abertura", "cnae", "uf", "mun", "bairro", "endereco", "cep",
          "tel1", "tel2", "email", "porte", "mei", "simples", "matriz",
          "divida_valor", "divida_qtd", "divida_tipos", "divida_ajuizada", "divida_desde"]


def gerar_base(db, tipo, ref, desde, ate, cnaes, muns):
    filtro = "e.novo=1" if tipo == "novas" else "e.pg=1"
    pasta = os.path.join(SAIDA, f"base_{tipo}_{ref}")
    if os.path.isdir(pasta):
        shutil.rmtree(pasta)
    os.makedirs(pasta)
    # planejamento das partes por estado e cidade
    contagem = db.execute(f"SELECT e.uf, e.mun, count(*) FROM est e WHERE {filtro} GROUP BY e.uf, e.mun ORDER BY e.uf, e.mun").fetchall()
    plano, partes, atual = {}, [], None
    for uf, mun, n in contagem:
        if atual is None or atual["uf"] != uf or (atual["n"] > 0 and atual["n"] + n > LINHAS_POR_PARTE):
            atual = {"uf": uf, "n": 0}; partes.append(atual)
        segs, resto = [], n
        while resto > 0:
            cabe = LINHAS_POR_PARTE - atual["n"]
            if cabe <= 0:
                atual = {"uf": uf, "n": 0}; partes.append(atual); continue
            t = min(cabe, resto)
            atual["n"] += t; resto -= t; segs.append((len(partes) - 1, t))
        plano[(uf, mun)] = segs
    for i, p in enumerate(partes):
        p["f"] = f"parte_{i + 1:04d}.json"

    info_muns, cont = {}, {}
    q = db.execute(f"""SELECT e.cnpj, COALESCE(m.razao,''), e.fant, e.abert, e.cnae, e.uf, e.mun, e.bairro, e.ende, e.cep,
          e.tel1, e.tel2, e.email, COALESCE(m.porte,''), COALESCE(s.mei,0), COALESCE(s.simples,0), e.matriz,
          COALESCE(p.valor,0), COALESCE(p.qtd,0), COALESCE(p.tipos,0), COALESCE(p.ajuiz,0), COALESCE(p.desde,'')
          FROM est e LEFT JOIN emp m ON m.basico=e.basico LEFT JOIN simp s ON s.basico=e.basico
          LEFT JOIN pg p ON p.cnpj=e.cnpj WHERE {filtro} ORDER BY e.uf, e.mun, e.abert DESC""")
    buf, idx_atual, usados = [], 0, set()
    total = 0

    def descarregar(i, linhas):
        if not linhas:
            return
        with open(os.path.join(pasta, partes[i]["f"]), "w", encoding="utf-8") as f:
            json.dump({"linhas": linhas}, f, ensure_ascii=False, separators=(",", ":"))

    ocup = {}
    csvs = {}
    cab = ["CNPJ", "Razão social", "Nome fantasia", "Abertura", "CNAE", "Atividade", "UF", "Município", "Bairro",
           "Endereço", "CEP", "Telefone 1", "Telefone 2", "E-mail", "MEI"] + (
        ["Valor em dívida", "Inscrições", "Ajuizada"] if tipo == "pgfn" else [])
    pasta_csv = os.path.join(pasta, "planilhas")
    os.makedirs(pasta_csv)
    for row in q:
        row = list(row)
        if tipo == "novas":
            row = row[:17]
        else:
            row[17] = round(row[17], 2)
        uf, mun = row[5], row[6]
        segs = plano[(uf, mun)]
        idxs = [i for i, _ in segs]
        k = ocup.get((uf, mun), 0)
        destino, acum = segs[-1][0], 0
        for i, t in segs:
            if k < acum + t:
                destino = i; break
            acum += t
        ocup[(uf, mun)] = k + 1
        if destino != idx_atual:
            descarregar(idx_atual, buf); buf = []; idx_atual = destino
        buf.append(row)
        usados.add(row[4])
        im = info_muns.get(mun)
        if not im:
            im = info_muns[mun] = [muns.get(mun, mun), uf, 0, idxs]
        im[2] += 1
        c = cont.setdefault(uf, {}).setdefault(row[4], [0, 0, 0, 0, 0])
        c[0] += 1; c[1] += 1 if (row[10] or row[11]) else 0; c[2] += 1 if row[12] else 0; c[3] += row[14]
        if tipo == "pgfn":
            c[4] = round(c[4] + row[17], 2)
        total += 1
        w = csvs.get(uf)
        if not w:
            fh = open(os.path.join(pasta_csv, f"{uf or 'EX'}.csv"), "w", encoding="utf-8-sig", newline="")
            w = csvs[uf] = (fh, csv.writer(fh, delimiter=";"))
            w[1].writerow(cab)
        extra = [str(row[17]).replace(".", ","), row[18], "Sim" if row[20] else "Não"] if tipo == "pgfn" else []
        w[1].writerow([row[0], row[1], row[2], row[3], row[4], cnaes.get(row[4], ""), uf, muns.get(mun, mun), row[7],
                       row[8], row[9], row[10], row[11], row[12], "Sim" if row[14] else "Não"] + extra)
    descarregar(idx_atual, buf)
    for fh, _ in csvs.values():
        fh.close()

    info = {"cnaes": {k: cnaes.get(k, k) for k in usados}, "muns": info_muns, "contagem": cont}
    with open(os.path.join(pasta, "info.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, separators=(",", ":"))
    manifest = {"app": "prospeccao-nort", "versao": 2, "tipo": tipo, "referencia": ref,
                "gerado_em": dt.datetime.now().isoformat(timespec="seconds"),
                "desde": desde, "ate": ate, "total": total, "campos": CAMPOS, "info": "info.json",
                "partes": [{"f": p["f"], "uf": p["uf"], "n": p["n"]} for p in partes]}
    with open(os.path.join(pasta, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    # apaga bases antigas do mesmo tipo
    for nome in os.listdir(SAIDA):
        if nome.startswith(f"base_{tipo}_") and nome != os.path.basename(pasta):
            shutil.rmtree(os.path.join(SAIDA, nome), ignore_errors=True)
    log(f"Base {tipo}: {total:,} empresas em {len(partes)} partes -> {pasta}")
    return pasta, total


# ---------------------------------------------------------------- principal
def desde_padrao():
    hoje = dt.date.today()
    return dt.date(hoje.year - (1 if hoje.month <= 2 else 0), 1, 1)


def main():
    ap = argparse.ArgumentParser(description="Robô de Prospecção Nort")
    ap.add_argument("--forcar", action="store_true")
    ap.add_argument("--desde", help="AAAA-MM-DD (padrão: 1º de janeiro do ano)")
    ap.add_argument("--so-novas", action="store_true")
    ap.add_argument("--so-pgfn", action="store_true")
    ap.add_argument("--pgfn-valor-min", type=float, default=1000)
    ap.add_argument("--pgfn-ufs", default="", help="ex.: CE,PI,MA (vazio = todos)")
    ap.add_argument("--pasta-receita", help="pasta com os zips da Receita já baixados")
    ap.add_argument("--pasta-pgfn", help="pasta com os zips da PGFN já baixados")
    ap.add_argument("--ref-receita", help="AAAA-MM quando usar --pasta-receita")
    ap.add_argument("--ref-pgfn", help="AAAA_trimestre_TT quando usar --pasta-pgfn")
    ap.add_argument("--incluir-filiais", action="store_true")
    ap.add_argument("--manter-zips", action="store_true")
    a = ap.parse_args()

    os.makedirs(SAIDA, exist_ok=True)
    estado = ler_estado()
    log("=== Robô de Prospecção Nort (v2) ===")
    inicio = time.time()

    # 1) o que tem de novo?
    try:
        if a.pasta_receita:
            ref_r = a.ref_receita or dt.date.today().strftime("%Y-%m")
            arq_r = {n: os.path.join(a.pasta_receita, n) for n in os.listdir(a.pasta_receita) if n.lower().endswith(".zip")}
            rem_r = None
        else:
            ref_r, rem_r = receita_ultima()
            arq_r = None
        log(f"Receita: base mais recente {ref_r}")
    except Exception as e:  # noqa
        log(f"ERRO ao consultar a Receita: {e}. Se persistir, baixe os zips em https://arquivos.receitafederal.gov.br "
            f"(Dados > Cadastros > CNPJ) e use --pasta-receita.")
        sys.exit(1)

    ref_p, arq_p, rem_p = None, None, None
    if not a.so_novas:
        try:
            if a.pasta_pgfn:
                ref_p = a.ref_pgfn or "manual"
                arq_p = [(os.path.join(a.pasta_pgfn, n), m) for n, m in PGFN_ARQS.items()
                         if os.path.exists(os.path.join(a.pasta_pgfn, n))]
            else:
                ref_p, rem_p = pgfn_ultima()
            log(f"PGFN: base mais recente {ref_p}")
        except Exception as e:  # noqa
            log(f"Aviso: não consegui consultar a PGFN ({e}). Sigo só com as empresas novas.")

    gerar_novas = not a.so_pgfn and (a.forcar or estado.get("novas_ref") != ref_r or bool(a.desde))
    gerar_pgfn = bool(ref_p) and not a.so_novas and (a.forcar or estado.get("pgfn_ref") != ref_p)
    if not gerar_novas and not gerar_pgfn:
        log("Nenhuma base nova na Receita nem na PGFN. Nada a fazer hoje.")
        return

    # 2) downloads
    os.makedirs(TRAB, exist_ok=True)
    if arq_r is None:
        arq_r = {}
        for n, tam in sorted(rem_r.items()):
            arq_r[n] = baixar([(f"{WEBDAV}/{ref_r}/{n}", {"Authorization": _auth()}),
                               (f"{DAV_FILES.format(tok=SHARE_TOKEN)}/{ref_r}/{n}", {})], os.path.join(TRAB, n), tam)
    if gerar_pgfn and arq_p is None:
        arq_p = [(baixar([(f"{PGFN}/{ref_p}/{n}", {})], os.path.join(TRAB, n), tam), PGFN_ARQS[n]) for n, tam in rem_p.items()]

    # 3) processamento
    db = conectar()
    cnpjs_pg = set()
    if gerar_pgfn:
        ufs = {u.strip().upper() for u in a.pgfn_ufs.split(",") if u.strip()}
        pgfn_agregar(db, arq_p, ufs)
        cnpjs_pg = {c for (c,) in db.execute("SELECT cnpj FROM pg WHERE valor>=?", (a.pgfn_valor_min,))}
        log(f"Empresas com dívida a partir de R$ {a.pgfn_valor_min:,.0f}: {len(cnpjs_pg):,}")
        if not a.manter_zips and not a.pasta_pgfn:
            for c, _ in arq_p:
                try:
                    os.remove(c)
                except OSError:
                    pass
    desde = dt.date.fromisoformat(a.desde) if a.desde else desde_padrao()
    cnaes, muns = receita_processar(db, arq_r, desde, gerar_novas, cnpjs_pg, a.incluir_filiais)
    del cnpjs_pg

    if gerar_novas:
        ate = db.execute("SELECT max(abert) FROM est WHERE novo=1").fetchone()[0] or desde.isoformat()
        gerar_base(db, "novas", ref_r, desde.isoformat(), ate, cnaes, muns)
        estado["novas_ref"] = ref_r
    if gerar_pgfn:
        gerar_base(db, "pgfn", ref_p, "", "", cnaes, muns)
        estado["pgfn_ref"] = ref_p
    db.close()
    estado["processado_em"] = dt.datetime.now().isoformat(timespec="seconds")
    salvar_estado(estado)
    if not a.manter_zips:
        shutil.rmtree(TRAB, ignore_errors=True)
    else:
        os.remove(os.path.join(TRAB, "trabalho.db"))
    log(f"PRONTO em {(time.time() - inicio) / 60:,.0f} min. Importe a(s) pasta(s) base_... no app Prospecção Nort.")


if __name__ == "__main__":
    main()
