"""
Consulta de Registros — 65º BPM: robô do REDS (roda no GitHub Actions, workflow consulta-65.yml)

O militar pede a consulta no app (Apps Script) com o PRÓPRIO usuário e senha do REDS. O navegador dele cifra usuário e
senha com a chave pública do robô (RSA-OAEP/SHA-256); o app guarda só o texto cifrado e o entrega UMA vez ao robô, que o
decifra com a chave privada (GitHub Secret CONSULTA65_PRIVADA). A senha não é gravada em arquivo, não vai nas entradas do
workflow e é mascarada no log (::add-mask::). O log do GitHub só mostra etapas e contagens, nunca dados de registros.

Rapidez em primeiro lugar (a consulta perde o sentido se demorar):
 1. entra no REDS com o usuário do militar (se o SISP recusar, para na hora, sem tentar de novo, para não bloquear);
 2. UMA pesquisa traz todos os registros da área do 65º BPM ("Código da unidade de área militar e subordinadas" = M3853)
    no período, pela data de criação do registro ou pela data do fato; corta pelo horário e manda a lista ao app na hora;
 3. separa por órgão (PC, Bombeiros) e por quem registrou (unidades do 65º BPM; o que veio só por "quem registrou" é da
    área se a cidade é da área do 65º BPM) e procura as naturezas dos indicadores também como secundária/terciária;
 4. PDF só SOB DEMANDA (v1.2): depois da consulta o robô fica conectado (a "sessão") enquanto a página do militar estiver
    aberta; cada "Baixar PDF" vira um trabalho: acha o registro, clica em "Ver" e pega o arquivo NA REDE (o Chrome do
    GitHub abre o PDF no visualizador e não deixa ler a resposta — foi o "sem PDF" da v1.1). Do PDF lê quem registrou, a
    equipe, a natureza (tentado/consumado) e o grau de lesão das vítimas — só contagens — e marca CVPe, CVPa e MV;
 5. "Nova consulta" do mesmo militar com a sessão viva roda aqui mesmo, sem GitHub e sem login de novo.

Variáveis: CONSULTA_URL (URL /exec do app), CONSULTA_CHAVE (chave do robô), CONSULTA_PRIVADA (chave privada PEM),
PEDIDO (número do pedido, vem do app). Opcionais: CHROME_PATH (Chrome já instalado), CONSULTA_CONFIG (teste).
Uso local: python consulta65.py --pedido C2611231406abc123   (--ver mostra o navegador)
"""
import argparse, base64, collections, datetime as dt, io, json, os, re, subprocess, sys, threading, time, unicodedata
import requests
from playwright.sync_api import sync_playwright
import robo_reds as RR

BASE = os.path.dirname(os.path.abspath(__file__))
VERSAO = '1.2 (23/11/2026)'

PADRAO = {
    'url': 'https://web.sids.mg.gov.br/reds/index.do',
    'url_busca': 'https://web.sids.mg.gov.br/reds/consultas/buscaRegHistoricos.do?operation=loadForSearch',
    # Login de um envio só (igual ao reds_config.json, que funciona na nuvem desde 21/11/2026)
    'login': [
        {'acao': 'preencher', 'alvo': {'placeholder': 'Usuário'}, 'valor': '{usuario}'},
        {'acao': 'preencher', 'alvo': {'placeholder': 'Senha'}, 'valor': '{senha}'},
        {'acao': 'clicar', 'alvo': {'css': 'button:has-text("Confirmar")'}, 'ms': 20000},
        {'acao': 'sumir', 'alvo': {'placeholder': 'Senha'}, 'ms': 60000},
        {'acao': 'esperar_rede', 'ms': 40000},
    ],
    'formato_data': '%d/%m/%Y',
    # Formulário de Registros Históricos (nomes conferidos na calibração de 21/11/2026)
    'sel': {
        'data_filtro': 'select[name="data_filtro"], select#data_filtro',
        'orgao': 'select[name="id_orgao"], select#id_orgao',
        'inicio': '#inicio_periodo', 'fim': '#fim_periodo',
        'secao': 'Relacione abaixo valores a',
        'valor': 'input[name="consultaEspecifica"]',
        'campos': '#camposDisponiveis',
        'inserir': 'input[name="inserir"]',
        'regra': 'input[name="inserirRegra"]',
        'buscar': 'fieldset:has-text("Relacione abaixo valores a") input[name="buscar"]',
        'ordem': 'select[name="nom_ordenacao"], select#nom_ordenacao',          # Relevância / dataFato / dataInclusao
        'ordem_tipo': 'select[name="tipo_ordenacao"], select#tipo_ordenacao',   # false = crescente, true = decrescente
    },
    'campo_area': 'cod_unidades_area_militar_superiores',      # Código da unidade de área militar e subordinadas
    # Campos que podem ir junto na regra da área (o valor M3853 nunca casa com eles). Na 1ª consulta real (23/11/2026) o
    # REDS não mostrou as linhas desses campos: ficam desligados (custam tempo). Ex.: ['matriculaRelator', 'matriculaDigitador'].
    'campos_extras': [],
    'campo_resp': 'codigoUnidadesResponsaveisSuperiores',      # Código da unidade responsável e subordinadas
    'campo_natureza': 'natureza',
    'orgaos': {'PC': '1', 'BM': '2', 'PM': '0'},               # id_orgao do formulário: PC e BM no grupo deles; PM de outra unidade
    # naturezas dos indicadores também como secundária/terciária: 7 pesquisas (área + natureza). A lesão corporal (B01129)
    # fica de fora: só conta (MV) com vítima morta, o que só o PDF diz.
    'buscar_naturezas': True,
    'ordenar': True,                                           # resultado do mais novo para o mais velho: para no início do turno
    'exportar': True,                                          # se a paginação não chegar ao total, tenta a exportação (Excel)
    'paginas_max': 60,
    'pdf': {'ativo': True, 'espera_s': 30, 'lote_bytes': 4000000, 'lote_n': 8},
    'sessao_max_s': 2700,                                      # a sessão nunca passa de 45 min (o workflow corta em 50)
    'bloquear': True,                                          # o REDS abre mais rápido sem imagens e fontes
    'tempo_total_s': 1200,
}

# Indicadores (Caderno Metodológico GDO 2025 — BEPM nº 38, 27/12/2024): natureza do ENVOLVIDO (vítima) e grau da lesão
IND = {
    'MV': {'B01121', 'B01504', 'C01157', 'B01129', 'B02001', 'B01148'},   # vítima com lesão fatal
    'CVPe': {'B01121', 'B01504', 'B02001', 'B01148'},                     # lesão diferente de fatal; tentado e consumado
    'CVPa': {'C01157', 'C01158', 'C01159'},                               # lesão diferente de fatal; tentado e consumado
}
COD_IND = sorted(IND['MV'] | IND['CVPe'] | IND['CVPa'])
COD_BUSCA = [x for x in COD_IND if x != 'B01129']       # lesão corporal: só MV com morte, e isso só o PDF mostra
NOME_COD = {'B01121': 'Homicídio', 'B01504': 'Feminicídio', 'C01157': 'Roubo', 'B01129': 'Lesão corporal', 'B02001': 'Tortura',
            'B01148': 'Sequestro e cárcere privado', 'C01158': 'Extorsão', 'C01159': 'Extorsão mediante sequestro'}

RE_REDS = RR.RE_REDS
RE_DH = re.compile(r'(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2})(?::(\d{2}))?)?')
RE_COD = re.compile(r'\b([A-Z])\s?(\d{2})\.?(\d{3})\b')
RE_MAT = re.compile(r'(?<![\d/])(\d{7}|\d{3}\.\d{3}-\d)(?![\d/])')


class Parar(Exception):
    """O app mandou parar (consulta apagada pelo militar ou vencida)."""


class SessaoCaiu(Exception):
    """A sessão do REDS caiu (voltou a tela de login)."""


class Achou(Exception):
    """Achou na lista o que procurava: para de virar página."""


EXEC = os.environ.get('GITHUB_RUN_ID', 'local')


_TRAVA_LOG = threading.Lock()


def log(msg):
    with _TRAVA_LOG: print(msg, flush=True)


def norm(s):
    """Caixa alta, sem acento; mantém as quebras de linha."""
    s = unicodedata.normalize('NFD', str(s or '')).encode('ascii', 'ignore').decode()
    return '\n'.join(re.sub(r'[ \t\r\f\v]+', ' ', l).strip() for l in s.upper().split('\n'))


def chave(s):
    return RR.chave(s)


def norm_mun(s):
    """Nome de cidade para comparar: caixa alta, sem acento, sem "- MG" no fim."""
    k = re.sub(r'\s*[/-]\s*MG$', '', norm(s).replace('\n', ' ')).strip()
    return re.sub(r'\s+', ' ', k)


def digitos(s):
    return re.sub(r'\D', '', str(s or ''))


def mascarar(v):
    """Esconde o valor de qualquer linha do log do GitHub."""
    if not v or os.environ.get('GITHUB_ACTIONS') != 'true': return
    for parte in {str(v), str(v).strip()}:
        if parte: print('::add-mask::' + parte.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A'), flush=True)


def cfg():
    c = json.loads(json.dumps(PADRAO))
    arq = os.environ.get('CONSULTA_CONFIG')
    if arq:
        with open(arq if os.path.isabs(arq) else os.path.join(BASE, arq), encoding='utf-8') as f:
            extra = json.load(f)
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(c.get(k), dict): c[k].update(v)
            else: c[k] = v
    return c


# ----------------------------------------------------------------------------------------------- comunicação com o app
class App:
    """Fala com o app (Apps Script). "pegar", "trabalho", "erro" e "fim" esperam a resposta; lista, progresso e PDFs vão
    por uma fila própria, na ordem, para o robô não parar enquanto o app grava. Cada envio leva o número do pedido: uma
    sessão do robô atende várias consultas (e PDFs) do mesmo militar."""
    def __init__(self, url, chave_, pedido):
        self.url, self.chave, self.pedido = (url or '').strip(), (chave_ or '').strip(), (pedido or '').strip()
        if not self.url or not self.chave or not self.pedido:
            raise SystemExit('Faltam CONSULTA_URL, CONSULTA_CHAVE ou o número do pedido.')
        self.cond = threading.Condition()
        self.dados, self.prog, self.ocupado, self.vivo = collections.deque(), collections.OrderedDict(), False, True
        self.parados, self.pct, self.falhas = {}, {}, 0
        self.lote, self.lote_bytes, self.lote_ped = [], 0, None
        self.lote_n, self.lote_max = 8, 4000000
        self.th = threading.Thread(target=self._trabalhar, daemon=True); self.th.start()

    def chamar(self, acao, tentativas=3, pedido=None, **params):
        corpo = json.dumps({'acao': 'robo.' + acao, 'chave': self.chave, 'pedido': pedido or self.pedido, 'exec': EXEC, 'params': params})
        erro = None
        for i in range(tentativas):
            try:
                r = requests.post(self.url, data=corpo.encode('utf-8'), headers={'Content-Type': 'text/plain;charset=utf-8'}, timeout=240)
                r.raise_for_status(); j = r.json(); break
            except Exception as e:                                   # rede ou o Apps Script ocupado: tenta de novo
                erro = e; time.sleep(3 * (i + 1))
        else:
            raise RuntimeError('o app não respondeu (%s)' % str(erro).splitlines()[0][:160])
        if j.get('parar'): raise Parar(j.get('msg') or 'consulta encerrada no app')
        if not j.get('ok'): raise RuntimeError('o app recusou "%s": %s' % (acao, j.get('erro')))
        return j.get('dados') or {}

    def _trabalhar(self):
        while True:
            with self.cond:
                while self.vivo and not self.dados and not self.prog: self.cond.wait(0.5)
                if not self.dados and not self.prog: return
                if self.dados: acao, params, ped = self.dados.popleft()
                else: ped, params = self.prog.popitem(last=False); acao = 'progresso'
                self.ocupado = True
            try:
                if ped not in self.parados: self.chamar(acao, tentativas=2 if acao == 'progresso' else 3, pedido=ped, **params)
            except Parar as e:
                self.parados[ped] = e
            except Exception as e:
                self.falhas += 1; log('  (aviso ao app falhou: %s)' % str(e)[:160])
            finally:
                with self.cond: self.ocupado = False; self.cond.notify_all()

    def checar(self, pedido=None):
        e = self.parados.get(pedido or self.pedido)
        if e: raise e

    def mandar(self, acao, pedido=None, **params):
        ped = pedido or self.pedido; self.checar(ped)
        with self.cond: self.dados.append((acao, params, ped)); self.cond.notify_all()

    def progresso(self, etapa, msg, pct=None, silencioso=False, pedido=None, **extra):
        ped = pedido or self.pedido
        if not silencioso: log('[%s] %s' % (etapa, msg))
        self.checar(ped)
        with self.cond:
            if pct is not None: self.pct[ped] = max(self.pct.get(ped, 0), int(pct))
            self.prog[ped] = dict(extra, etapa=etapa, msg=msg[:300], pct=self.pct.get(ped, 0)); self.prog.move_to_end(ped); self.cond.notify_all()

    def pdf(self, reds, dados, info, pedido=None, ja=False):
        """PDFs em lotes (até 8 arquivos ou 4 MB); "ja" manda na hora (PDF pedido pelo militar)."""
        ped = pedido or self.pedido
        self.checar(ped)
        if dados and len(dados) > 15 * 1024 * 1024: dados, info = b'', dict(info, pdf='grande')   # com fotos: fica no REDS
        with self.cond:
            if self.lote and self.lote_ped != ped: self._fechar_lote()
            self.lote_ped = ped
            self.lote.append({'reds': reds, 'b64': base64.b64encode(dados).decode() if dados else '', 'info': info})
            self.lote_bytes += len(dados or b'')
            if ja or len(self.lote) >= self.lote_n or self.lote_bytes >= self.lote_max: self._fechar_lote()

    def _fechar_lote(self):
        if self.lote: self.dados.append(('pdfs', {'arquivos': self.lote}, self.lote_ped)); self.cond.notify_all()
        self.lote, self.lote_bytes = [], 0

    def esvaziar(self, limite_s=600, pedido=None):
        with self.cond:
            self._fechar_lote()
            fim = time.time() + limite_s
            while (self.dados or self.prog or self.ocupado) and time.time() < fim: self.cond.wait(0.5)
        if pedido: self.checar(pedido)

    def encerrar(self):
        with self.cond: self.vivo = False; self.cond.notify_all()
        self.th.join(timeout=30)


# ----------------------------------------------------------------------------------------------- credenciais cifradas
def pem_privado(txt):
    """Aceita o PEM como foi colado no GitHub (com ou sem as quebras de linha)."""
    corpo = re.sub(r'-----[A-Z ]+-----', '', (txt or '').strip())
    corpo = re.sub(r'[^A-Za-z0-9+/=]', '', corpo)
    if not corpo: raise ValueError('chave privada vazia (secret CONSULTA65_PRIVADA)')
    return ('-----BEGIN PRIVATE KEY-----\n' + '\n'.join(corpo[i:i + 64] for i in range(0, len(corpo), 64)) + '\n-----END PRIVATE KEY-----\n').encode()


def decifrar(cripto_b64, pem, n, m):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    chave_ = serialization.load_pem_private_key(pem_privado(pem), password=None)
    claro = chave_.decrypt(base64.b64decode(cripto_b64), padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    d = json.loads(claro.decode('utf-8'))
    if not isinstance(d, dict) or d.get('n') != n: raise ValueError('o texto cifrado não é deste pedido')
    if abs(time.time() * 1000 - float(d.get('t') or 0)) > 45 * 60 * 1000: raise ValueError('pedido velho demais (mais de 45 min)')
    if not d.get('teste') and m and digitos(d.get('u')) != m:       # usuário "PM" + número: o número tem de ser o do pedido
        raise ValueError('o usuário cifrado não é o Nº PM do pedido')
    return d


# ----------------------------------------------------------------------------------------------- datas e janela
def data_hora(txt):
    m = RE_DH.search(txt or '')
    if not m: return None
    d, mo, a, h, mi, s = m.groups()
    try: return dt.datetime(int(a), int(mo), int(d), int(h or 0), int(mi or 0), int(s or 0))
    except ValueError: return None


class Janela:
    def __init__(self, ini, fim, base):
        self.ini, self.fim = dt.datetime.fromisoformat(ini[:16]), dt.datetime.fromisoformat(fim[:16]).replace(second=59)
        self.base = base if base in ('dataInclusao', 'dataFato') else 'dataInclusao'

    def datas(self, fmt):
        return self.ini.strftime(fmt), self.fim.strftime(fmt)

    def quando(self, it):
        return data_hora(it.get('criacao') if self.base == 'dataInclusao' else it.get('fato'))

    def dentro(self, it):
        d = self.quando(it)
        return d is None or self.ini <= d <= self.fim


# ----------------------------------------------------------------------------------------------- lista do REDS
TITULOS = {'reds': 'NUMERODOREDS', 'tipo': 'TIPODERELATORIO', 'criacao': 'DATAHORADECRIACAO', 'fato': 'DATAHORADOFATO',
           'natureza': 'NATUREZAPRINCIPAL', 'endereco': 'ENDERECODOFATO', 'origem': 'SISTEMAORIGEM', 'protocolo': 'PROTOCOLOINICIAL'}


def interpretar(reg):
    cab, cel = reg.get('cab') or [], reg.get('cells') or []
    idx = {}
    for campo, t in TITULOS.items():
        for i, h in enumerate(cab):
            if t in chave(h): idx[campo] = i; break
    g = lambda k: (cel[idx[k]] if k in idx and idx[k] < len(cel) else '').strip()
    m = RE_REDS.search(g('reds') or ' '.join(cel))
    if not m: return None
    dh = lambda s: (lambda x: x.group(0) if x else '')(re.search(r'\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?', s or ''))
    return {'reds': m.group(0), 'tipo': g('tipo'), 'criacao': dh(g('criacao')), 'fato': dh(g('fato')), 'natureza': g('natureza'),
            'endereco': g('endereco'), 'origem': g('origem'), 'protocolo': g('protocolo'),
            '_extras': [(chave(x[0]), x[0], ' '.join(x[1:]).strip()) for x in reg.get('extras') or [] if x]}


def municipio_bairro(end):
    partes = [p.strip() for p in re.split(r'\s+-\s+', end or '') if p.strip()]
    if partes and re.fullmatch(r'[A-Z]{2}', norm(partes[-1])): partes = partes[:-1]      # "... - MG"
    if len(partes) >= 2: return partes[-1], (partes[-2] if len(partes) >= 3 else '')
    return '', ''


def grupo_tipo(tipo):
    k = chave(tipo)
    if k.startswith('RAT') or 'ATIVIDADE' in k: return 'RAT'
    if k.startswith('BOS') or 'SIMPLIFICADO' in k: return 'BOS'
    return 'BO'


def pessoas_do_texto(v):
    """'1234567 - FULANO DE TAL; 7654321 - BELTRANO' -> [{'mat', 'nome'}] (o nome pode faltar)."""
    t = re.sub(r'\(\s*PM\s*\d+\s*\)', ' ', norm(v).replace('\n', ' '))
    out, ms = [], list(RE_MAT.finditer(t))
    for i, m in enumerate(ms):
        fim = ms[i + 1].start() if i + 1 < len(ms) else len(t)
        nome = re.sub(r'^[\s\-–:,;/]+|[\s\-–:,;/]+$', '', t[m.end():fim])
        out.append({'mat': re.sub(r'\D', '', m.group(1)), 'nome': nome if re.fullmatch(r'[A-Z][A-Z ]{4,80}', nome or '') else ''})
    return out


def nomes_do_texto(v):
    return [p for p in (re.sub(r'\s+', ' ', x).strip() for x in re.split(r'[;,\n]|\s/\s', norm(v))) if re.fullmatch(r'[A-Z][A-Z ]{4,80}', p)]


def dados_das_extras(it):
    """Linhas extras da lista (se o REDS mostrar os campos pedidos junto com a área)."""
    o = {}
    for k, rot, v in it.get('_extras') or []:
        if not v: continue
        if 'UNIDADERESPONSAVELPELOREGISTRO' in k: o['unidadeResp'] = norm(v)[:120]
        elif 'RELATOR' in k:
            p = pessoas_do_texto(v)
            if p: o['relator'] = p[0]
        elif 'DIGITADOR' in k:
            p = pessoas_do_texto(v)
            if p: o['digitador'] = p[0]
        elif 'MATRICULADOINTEGRANTE' in k: o['_eqMat'] = pessoas_do_texto(v)
        elif 'NOMEDOINTEGRANTE' in k: o['_eqNome'] = nomes_do_texto(v)
    pes, nomes = o.pop('_eqMat', []), o.pop('_eqNome', [])
    if pes and len(nomes) == len(pes): eq = [{'mat': p['mat'], 'nome': p['nome'] or n} for p, n in zip(pes, nomes)]
    elif pes: eq = pes
    else: eq = [{'mat': '', 'nome': n} for n in nomes]
    if eq: o['equipe'] = eq[:8]
    return o


def codigos_do_texto(nat):
    """Natureza principal da lista (texto) -> códigos dos indicadores."""
    k = norm(nat)
    cods = {'%s%s%s' % m.groups() for m in RE_COD.finditer(k)}
    if 'FEMINICIDIO' in k: cods.add('B01504')
    elif 'HOMICIDIO' in k and 'CULPOS' not in k: cods.add('B01121')
    if 'EXTORSAO MEDIANTE SEQUESTRO' in k: cods.add('C01159')
    elif 'EXTORSAO' in k and 'INDIRETA' not in k: cods.add('C01158')
    if 'SEQUESTRO' in k and 'CARCERE' in k: cods.add('B01148')
    if 'ROUBO' in k or 'LATROCINIO' in k: cods.add('C01157')
    if 'TORTURA' in k: cods.add('B02001')
    if 'LESAO CORPORAL' in k and 'CULPOS' not in k: cods.add('B01129')
    return cods


def avaliar_indicadores(it):
    """CVPe / CVPa / MV. Com o PDF: conta as vítimas pela natureza do envolvido e pelo grau da lesão (como o Caderno
    Metodológico). Sem o PDF: só pela natureza, marcando dúvida quando não dá para saber se houve morte."""
    cods = set(it.get('codBusca') or []) | codigos_do_texto(it.get('natureza'))
    if it.get('natCod'): cods.add(it['natCod'])
    cods |= {x.get('cod') for x in it.get('natSec') or [] if x.get('cod')}
    rel = sorted(cods & set(COD_IND))
    ind = {'cod': rel, 'tags': [], 'duvida': [], 'vit': {}, 'fonte': '', 'nota': ''}
    if not rel:
        it['ind'] = ind; return
    if it.get('orgao') in ('PP', 'SE') or it.get('orgaoPdf') in ('PP', 'SE'):
        ind['nota'] = 'Registro da Polícia Penal/socioeducativo: não conta nos indicadores.'; it['ind'] = ind; return
    if it.get('naArea') is False:
        ind['nota'] = 'Fato fora da área do 65º BPM: conta para a unidade da área.'; it['ind'] = ind; return
    a_confirmar = it.get('naArea') is None
    vit = [v for v in it.get('vitimas') or [] if v.get('cod') in COD_IND]
    if vit:
        n = {'MV': 0, 'CVPe': 0, 'CVPa': 0}
        for v in vit:
            if v.get('fatal'):
                if v['cod'] in IND['MV']: n['MV'] += 1
            else:
                if v['cod'] in IND['CVPe']: n['CVPe'] += 1
                if v['cod'] in IND['CVPa']: n['CVPa'] += 1
        ind['vit'] = {k: q for k, q in n.items() if q}
        ind['tags'] = [k for k in ('MV', 'CVPe', 'CVPa') if n[k]]
        ind['fonte'] = 'pdf'
        if it.get('agenteServico') and ({'MV', 'CVPe'} & set(ind['tags'])):
            ind['nota'] = 'Há agente de segurança em serviço como autor: pode ser intervenção policial (não conta em MV/CVPe).'
    else:
        tags, duv = set(), set()
        st, k = it.get('natStatus') or '', norm(it.get('natureza'))
        fatal_txt = 'LATROCINIO' in k or 'SEGUIDA DE MORTE' in k
        for c in rel:
            if c in ('B01121', 'B01504'):
                if c == it.get('natCod') and st == 'TENTADO': tags.add('CVPe')
                elif c == it.get('natCod') and st == 'CONSUMADO': tags.add('MV')
                else: duv.update(('MV', 'CVPe'))
            elif c in ('B02001', 'B01148'): tags.add('CVPe')
            elif c == 'C01157' and fatal_txt: tags.add('MV')
            elif c in IND['CVPa']: tags.add('CVPa')
            elif c == 'B01129' and fatal_txt: tags.add('MV')
        ind['tags'] = [x for x in ('MV', 'CVPe', 'CVPa') if x in tags]
        ind['duvida'] = [x for x in ('MV', 'CVPe', 'CVPa') if x in duv and x not in tags]
        ind['fonte'] = 'natureza'
        if ind['duvida']: ind['nota'] = 'Tentado ou consumado ainda não confirmado (confira no PDF).'
    if a_confirmar and ind['tags']:                                     # resp. pelo 65º, mas não veio na pesquisa da área
        ind['duvida'] = [x for x in ('MV', 'CVPe', 'CVPa') if x in set(ind['tags']) | set(ind['duvida'])]; ind['tags'] = []
        ind['nota'] = ('Área do fato não confirmada no REDS: confira antes de contar. ' + ind['nota']).strip()
    it['ind'] = ind


def secundarias(it):
    """Naturezas além da principal: do PDF (secundárias/terciárias) e, sem PDF, as achadas na pesquisa por natureza."""
    out, vistos = [], {it.get('natCod')} | codigos_do_texto(it.get('natureza'))
    for x in it.get('natSec') or []:
        if x.get('cod') and x['cod'] not in vistos: vistos.add(x['cod']); out.append({'cod': x['cod'], 'desc': x.get('desc') or '', 'fonte': 'pdf'})
    for cod in it.get('codBusca') or []:
        if cod not in vistos: vistos.add(cod); out.append({'cod': cod, 'desc': NOME_COD.get(cod, ''), 'fonte': 'pesquisa'})
    it['secundarias'] = out
    return out


def area_do_pdf(unidade_area, unome):
    """Unidade de área do PDF -> True (é do 65º BPM), False (outra) ou None (sem informação)."""
    k, m = norm(unidade_area), re.match(r'\D*(\d+)\D*?([A-Za-z]{3,})', unome or '')
    if not k or not m: return None
    return bool(re.search(r'\b%s\s*[O°º]?\s*%s\b' % (m.group(1), m.group(2).upper()), k))


def mamb(it):
    k = norm(it.get('unidadeResp')) + ' ' + norm(it.get('tipo'))
    return 'MAMB' in k or 'MEIO AMBIENTE' in k or 'AMBIENTAL' in k


def regrupar(it, prontos):
    """Grupo da tela: AREA enquanto os órgãos não foram separados; depois BPM, PC, BM, MAMB, PMOUTRA ou OUTROS."""
    o = it.get('orgao') if it.get('orgao') in ('PC', 'BM', 'PM') else (it.get('orgaoPdf') or it.get('orgao') or '')
    it['orgaoFinal'] = o
    if not prontos: it['grupo'] = 'AREA'
    elif it.get('doBpm'): it['grupo'] = 'BPM'
    elif o in ('PC', 'BM'): it['grupo'] = o
    elif mamb(it): it['grupo'] = 'MAMB'
    elif o == 'PM': it['grupo'] = 'PMOUTRA'
    elif it.get('semResp'): it['grupo'] = 'AREA'                       # a pesquisa da unidade falhou: órgão não confirmado
    else: it['grupo'] = 'OUTROS'


# ----------------------------------------------------------------------------------------------- leitura do PDF do registro
CARGOS = (r'TENENTE[ -]CORONEL|TEN(?:ENTE)? CEL|CORONEL|CEL|MAJOR|MAJ|CAPITAO|CAP|(?:1O?|2O?|PRIMEIRO|SEGUNDO) TENENTE|[12]O? TEN|'
          r'ASPIRANTE(?: A OFICIAL)?|ASP OF|SUBTENENTE|SUBTEN|ST|(?:1O?|2O?|3O?|PRIMEIRO|SEGUNDO|TERCEIRO) SARGENTO|[123]O? SGT|'
          r'CABO|CB|SOLDADO(?: DE [12]A? CLASSE| [12]A? CLASSE)?|SD|DELEGADO(?: DE POLICIA)?(?: CIVIL)?|ESCRIVAO(?: DE POLICIA)?(?: CIVIL)?|'
          r'INVESTIGADOR(?: DE POLICIA)?(?: CIVIL)?|AGENTE DE POLICIA(?: CIVIL)?|PERITO(?: CRIMINAL)?|BOMBEIRO MILITAR')
SIGLA = [(r'TENENTE[ -]CORONEL|TEN(?:ENTE)? CEL', 'TEN CEL'), (r'CORONEL|CEL', 'CEL'), (r'MAJOR|MAJ', 'MAJ'), (r'CAPITAO|CAP', 'CAP'),
         (r'(?:1O?|PRIMEIRO) TENENTE|1O? TEN', '1º TEN'), (r'(?:2O?|SEGUNDO) TENENTE|2O? TEN', '2º TEN'), (r'ASPIRANTE(?: A OFICIAL)?|ASP OF', 'ASP OF'),
         (r'SUBTENENTE|SUBTEN|ST', 'SUBTEN'), (r'(?:1O?|PRIMEIRO) SARGENTO|1O? SGT', '1º SGT'), (r'(?:2O?|SEGUNDO) SARGENTO|2O? SGT', '2º SGT'),
         (r'(?:3O?|TERCEIRO) SARGENTO|3O? SGT', '3º SGT'), (r'CABO|CB', 'CB'), (r'SOLDADO.*|SD', 'SD')]
RE_CARGO = re.compile(r'\b(' + CARGOS + r')\b')
NAO_NOME = re.compile(r'\b(POLICIA|MILITAR|CIVIL|BOMBEIROS?|CORPORACAO|UNIDADE|MATRICULA|CARGO|NOME|COMPLETO|VIATURA|NUM|SIM|NAO|'
                      r'HIPOTECADO|ASSINATURA|INTEGRANTES?|RELATOR|OCORRENCIA|DADOS|CONTROLE|INTERNO|BOLETIM|NUMERO|FL|PAGINA|'
                      r'SISTEMA|INTEGRADO|SEGURANCA|PUBLICA|DIGITADOR|GERADO|ORGAO|PREFIXO|PLACA)\b')
SECOES = ['HISTORICO DA OCORRENCIA', 'MILITARES/POLICIAIS INTEGRANTES', 'DADOS PARA CONTROLE INTERNO', 'MATERIAIS / PRODUTOS', 'VIATURAS',
          'AUTUACOES E PROCEDIMENTOS', 'RECIBO DA AUTORIDADE', 'PERICIA TECNICA', 'FOTOS', 'ANEXO MEIO AMBIENTE', 'CHECK-LIST']
UNIDADE_RX = re.compile(r'\b(GP|GPM|PEL|CIA|BPM|RPM|BBM|COB|CPE|CME|DP|DEPOL|DELEGACIA|DRPC|COMPANHIA|PELOTAO|BATALHAO|MAMB|SEDE|SECAO|SPA)\b')


def sigla_cargo(c):
    c = (c or '').strip()
    for rx, s in SIGLA:
        if re.fullmatch(rx, c): return s
    return c


def _letra(ch):
    """Caixa alta sem acento, um caractere por caractere (não muda as colunas do texto em leiaute)."""
    if ch in 'ºª°': return 'O'
    b = unicodedata.normalize('NFD', ch)[0]
    return b.upper() if ord(b) < 128 else ' '


def textos_pdf(dados):
    """Duas leituras do PDF: T (texto corrido, normalizado) e L (linhas em leiaute, com as colunas no lugar)."""
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(dados))
    corrido, leiaute = [], []
    for p in r.pages[:40]:
        try: corrido.append(p.extract_text() or '')
        except Exception: pass
        try: leiaute += [''.join(_letra(ch) for ch in l) for l in (p.extract_text(extraction_mode='layout') or '').split('\n')]
        except Exception: pass
    return norm('\n'.join(corrido)), [l.rstrip() for l in leiaute]


ROTULOS = ['UNIDADE RESPONSAVEL PELO REGISTRO', 'UNIDADE DE AREA RESPONSAVEL', 'UNIDADE MILITAR', 'UNIDADE POLICIAL', 'DATA DO REGISTRO',
           'DESTINATARIO', 'ORIGEM DA COMUNICACAO', 'PROVAVEL DESCRICAO', 'TENTADO / CONSUMADO', 'ALVO DO EVENTO', 'NATUREZA SECUNDARIA',
           'DATA/HORA', 'DESCRICAO DO LUGAR', 'SEXO', 'TIPO ENVOLVIMENTO', 'TIPO DE PESSOA', 'COD. NATUREZA', 'DESCRICAO NATUREZA',
           'NOME COMPLETO', 'IDADE APARENTE', 'GRAU DA LESAO', 'CPF / CNPJ', 'NUM VIATURA', 'MATRICULA', 'CARGO', 'CORPORACAO',
           'UNIDADE', 'ASSINATURA', 'EM SERVICO', 'AGENTE DE SEGURANCA', 'ESTADO CIVIL', 'NACIONALIDADE']


def eh_rotulo(t):
    t = re.sub(r'\s+', ' ', t or '').strip(' :')
    return any(t == r or t.startswith(r + ' ') or t.startswith(r + ':') for r in ROTULOS)


def celulas(linha):
    """Pedaços da linha separados por 2 ou mais espaços: [(coluna, texto)]."""
    return [(m.start(), m.group(0).strip()) for m in re.finditer(r'\S+(?: \S+)*', linha)]


def valor_abaixo(L, rotulo, ini=0, fim=None):
    """Valor de um rótulo do PDF: na mesma linha depois de ':' ou na 1ª linha de baixo, na mesma coluna."""
    fim = len(L) if fim is None else fim
    for i in range(ini, fim):
        j = L[i].find(rotulo)
        if j < 0: continue
        resto = L[i][j + len(rotulo):]
        m = re.match(r'\s*:\s*(\S+(?: \S+)*)', resto)
        if m and not eh_rotulo(m.group(1)): return m.group(1).strip()
        for k in range(i + 1, min(i + 4, fim)):
            if not L[k].strip(): continue
            cel = [t for c, t in celulas(L[k]) if abs(c - j) <= 3]
            if cel: return '' if eh_rotulo(cel[0]) else cel[0]
            break
        return ''
    return ''


def texto_pdf(dados):
    return textos_pdf(dados)[0]


def bloco(T, inicio, fins, tam=4000):
    i = T.find(inicio)
    if i < 0: return ''
    j = i + len(inicio); fim = min(len(T), j + tam)
    for f in fins:
        k = T.find(f, j)
        if 0 <= k < fim: fim = k
    return T[j:fim]


def nome_perto(trecho):
    """Primeira sequência com cara de nome (2 a 8 palavras só de letras, sem rótulos, unidades ou cargos)."""
    for linha in trecho.split('\n'):
        l = RE_CARGO.sub(' ', re.sub(r'\S*/\S*', ' ', RE_MAT.sub(' ', linha)))
        l = re.sub(r'\b\d+\b', ' ', l)
        for pedaco in re.split(r'\s{2,}|\b(?:POLICIA MILITAR|POLICIA CIVIL|CORPO DE BOMBEIROS MILITAR|SIM|NAO)\b', l):
            p = re.sub(r'\s+', ' ', pedaco or '').strip()
            if re.fullmatch(r'[A-Z]{2,}(?: (?:D[AEO]S?|E|[A-Z]{2,})){1,7}', p) and not NAO_NOME.search(p) and not UNIDADE_RX.search(p):
                return p
    return ''


RE_NATL = re.compile(r'\b([A-Z])\s?(\d{2})\.?(\d{3})\s*-\s*(?:[A-Z]\s?\d{2}\.\d{3}\s*-\s*)?([A-Z][^\n]{1,90})')


def natureza_de(txt):
    m = RE_NATL.search(txt or '')
    if not m: return None
    desc = re.split(r'\s{2,}|\bTENTADO\b|\bCONSUMADO\b|\bALVO DO EVENTO\b|\bNATUREZA SECUNDARIA\b', m.group(4))[0].strip(' -')
    desc = re.sub(r'(?<=[A-Z])(CONSUMADO|TENTADO)$', '', desc).strip(' -')          # coluna vizinha grudada no fim
    return {'cod': '%s%s%s' % m.groups()[:3], 'desc': desc[:80]}


def ler_pdf_reds(dados, reds=''):
    """Só o que a tela mostra: órgão, unidade, natureza (tentado/consumado), vítimas (natureza e se a lesão foi fatal — sem
    nomes), relator, digitador e equipe (policiais). Também diz se o nº do REDS está no PDF e quais rótulos achou.
    Lê o PDF de dois jeitos (em colunas e corrido) e usa o que achar."""
    T, L = textos_pdf(dados)
    o, achou = {}, []
    if reds:
        corrido = re.sub(r'\s', '', T)
        o['_confere'] = reds in corrido or reds.replace('-', '') in corrido
    m = re.search(r'SISTEMA INTEGRADO DE SEGURANCA PUBLICA\s*-\s*([A-Z ]{5,60}?)\s*(?:N\s*O?\s*\d|\n|$)', T)
    if m:
        achou.append('orgao'); s = m.group(1)
        o['orgaoPdf'] = ('BM' if 'BOMBEIRO' in s else 'PC' if 'CIVIL' in s else 'PP' if 'PENAL' in s else 'PRF' if 'RODOVIARIA FEDERAL' in s
                         else 'SE' if 'SOCIOEDUC' in s else 'PM' if 'MILITAR' in s else 'OUTRO')
    for rot, k in (('UNIDADE RESPONSAVEL PELO REGISTRO', 'unidadeResp'), ('UNIDADE DE AREA RESPONSAVEL', 'unidadeArea')):
        v = valor_abaixo(L, rot)
        if not v:                                                       # texto corrido: 1ª linha que não é rótulo
            for l in bloco(T, rot, ['DATA DO REGISTRO', 'DESTINATARIO', 'ORIGEM DA COMUNICACAO'], 500).split('\n'):
                if l.strip(' :-') and not eh_rotulo(l): v = l.strip(' :-'); break
        if v: o[k] = re.sub(r'\s+', ' ', v)[:120]; achou.append(k)
    # natureza principal ("C01157 - C 01.157 - ROUBO"), tentado/consumado e secundárias
    nat = natureza_de(valor_abaixo(L, 'PROVAVEL DESCRICAO DA OCORRENCIA PRINCIPAL'))
    bn = bloco(T, 'PROVAVEL DESCRICAO DA OCORRENCIA PRINCIPAL', ['DATA/HORA DO FATO', 'DESCRICAO DO LUGAR', 'ENVOLVIDO'], 1500) or bloco(T, 'DADOS DA OCORRENCIA', ['DATA/HORA DO FATO', 'ENVOLVIDO'], 1500)
    nats = [natureza_de(x.group(0)) for x in RE_NATL.finditer(bn)] if bn else []
    if nat: nats = [nat] + [x for x in nats if x['cod'] != nat['cod']]
    vistos = set(); nats = [x for x in nats if x and not (x['cod'] in vistos or vistos.add(x['cod']))]
    if nats:
        achou.append('natureza'); o['natCod'], o['natDesc'], o['natSec'] = nats[0]['cod'], nats[0]['desc'], nats[1:6]
    st = re.search(r'\b(CONSUMADO|TENTADO)\b', valor_abaixo(L, 'TENTADO / CONSUMADO')) or (re.search(r'\b(CONSUMADO|TENTADO)\b', re.sub(r'TENTADO\s*/\s*CONSUMADO', ' ', bn)) if bn else None)
    if st: o['natStatus'] = st.group(1); achou.append('tentadoConsumado')
    # envolvidos: só natureza, tentado/consumado, se é vítima e se a lesão foi fatal (nada de nome, documento ou endereço)
    vit, agente, achou_env = [], False, False
    cab = [i for i, l in enumerate(L) if re.fullmatch(r'\s*ENVOLVIDO\s+\d{1,2}\s*', l)]
    for n, i in enumerate(cab):
        fim = cab[n + 1] if n + 1 < len(cab) else len(L)
        for k in range(i + 1, fim):
            if any(L[k].strip().startswith(x) for x in SECOES): fim = k; break
        tipo, cod = valor_abaixo(L, 'TIPO ENVOLVIMENTO', i, fim), valor_abaixo(L, 'COD. NATUREZA', i, fim)
        tc, lesao = valor_abaixo(L, 'TENTADO / CONSUMADO', i, fim), valor_abaixo(L, 'GRAU DA LESAO', i, fim)
        mc = RE_COD.search(cod or '')
        if tipo or mc: achou_env = True
        if valor_abaixo(L, 'EM SERVICO', i, fim).startswith('SIM') and 'AGENTE DE SEGURANCA' in ' '.join(L[i:fim]) and 'AUTOR' in tipo: agente = True
        if mc and 'VITIMA' in tipo:
            vit.append({'cod': '%s%s%s' % mc.groups(), 'tc': tc if tc in ('CONSUMADO', 'TENTADO') else '', 'fatal': 'FATAL' in lesao})
    if not achou_env:                                                   # texto corrido
        cabecas = list(re.finditer(r'(?m)^\s*ENVOLVIDO\s+(\d{1,2})\b(?!\s*NR)', T))
        for i, cb in enumerate(cabecas):
            fim = cabecas[i + 1].start() if i + 1 < len(cabecas) else len(T)
            for s_ in SECOES:
                k = T.find(s_, cb.end())
                if 0 <= k < fim: fim = k
            b = T[cb.end():fim]; b2 = re.sub(r'TENTADO\s*/\s*CONSUMADO', ' ', b)
            mc = re.search(r'COD\.?\s*NATUREZA\s*:?\s*\n?\s*([A-Z])\s?(\d{2})\.?(\d{3})', b) or RE_COD.search(b2)
            if re.search(r'AGENTE DE SEGURANCA', b) and re.search(r'EM SERVICO\??\s*:?\s*SIM', b) and re.search(r'\bAUTOR\b', b2): agente = True
            if mc and re.search(r'\bVITIMA', b2):
                tc = re.search(r'\b(CONSUMADO|TENTADO)\b', b2)
                vit.append({'cod': '%s%s%s' % mc.groups(), 'tc': tc.group(1) if tc else '', 'fatal': bool(re.search(r'\bFATAL\b', b2))})
            achou_env = True
    if achou_env: achou.append('envolvidos')
    o['vitimas'] = vit
    if agente: o['agenteServico'] = True
    # equipe: "NUM VIATURA / MATRÍCULA / CARGO" e o nome logo abaixo
    be = bloco(T, 'MILITARES/POLICIAIS INTEGRANTES', ['DADOS PARA CONTROLE INTERNO', 'RECIBO DA AUTORIDADE', 'ANEXO MEIO AMBIENTE', 'AUTUACOES'], 8000)
    eq = []
    for mm in re.finditer(r'(?<!\d)(\d{7}|\d{3}\.\d{3}-\d)\s+(' + CARGOS + r')\b', be):
        mat = re.sub(r'\D', '', mm.group(1))
        if mat in [x['mat'] for x in eq]: continue
        eq.append({'mat': mat, 'cargo': sigla_cargo(mm.group(2)), 'nome': nome_perto(be[mm.end():mm.end() + 400])})
    if eq: o['equipe'] = eq[:10]; achou.append('equipe')
    # relator: bloco "DADOS PARA CONTROLE INTERNO/RELATOR DA OCORRÊNCIA" (rótulos em cima, valores embaixo)
    ir = next((i for i, l in enumerate(L) if 'RELATOR DA OCORRENCIA' in l), -1)
    rel = {}
    if ir >= 0:
        mat = re.sub(r'\D', '', valor_abaixo(L, 'MATRICULA', ir, ir + 14))
        if len(mat) == 7:
            rel = {'mat': mat, 'cargo': sigla_cargo(valor_abaixo(L, 'CARGO', ir, ir + 14)), 'nome': valor_abaixo(L, 'NOME COMPLETO', ir, ir + 14)}
            if not re.fullmatch(r'[A-Z][A-Z ]{4,80}', rel['nome'] or ''): rel['nome'] = ''
    if not rel:
        br = bloco(T, 'RELATOR DA OCORRENCIA', ['ASSINATURA', 'RECIBO DA AUTORIDADE', 'DESTINATARIO /'], 900) or bloco(T, 'DADOS PARA CONTROLE INTERNO', ['ASSINATURA', 'RECIBO'], 900)
        mr = RE_MAT.search(br or '')
        if mr:
            cargo = RE_CARGO.search(br[mr.end():] or '') or RE_CARGO.search(br)
            rel = {'mat': re.sub(r'\D', '', mr.group(1)), 'cargo': sigla_cargo(cargo.group(1)) if cargo else '', 'nome': nome_perto(br[mr.end():]) or nome_perto(br)}
    if rel:
        igual = next((x for x in eq if x['mat'] == rel['mat']), None)        # o mesmo militar na equipe: nome e cargo de lá
        if igual:
            rel['nome'] = igual['nome'] or rel.get('nome', ''); rel['cargo'] = igual['cargo'] or rel.get('cargo', '')
        o['relator'] = rel; achou.append('relator')
    md = re.search(r'DIGITADOR\s*:?\s*(\d{7}|\d{3}\.\d{3}-\d)', T)
    if md: o['digitador'] = {'mat': re.sub(r'\D', '', md.group(1))}; achou.append('digitador')
    o['_achou'] = achou
    return o


# ----------------------------------------------------------------------------------------------- REDS: busca e PDF
def opcoes_campos(page, c):
    try: return [x[0] for x in page.eval_on_selector_all(c['sel']['campos'] + ' option', "os => os.map(o => [o.value])")]
    except Exception: return []


def tela_de_login(page):
    try: return page.get_by_placeholder('Senha').first.is_visible()
    except Exception: return False


def abrir_formulario(page, c):
    """Abre a pesquisa. Se o REDS voltou para a tela de login (sessão caiu), avisa na hora, sem esperar os 30 s."""
    page.goto(c['url_busca'], wait_until='domcontentloaded', timeout=60000)
    alvo, fim, login = c['sel']['inicio'], time.time() + 30, 0
    while time.time() < fim:
        try:
            if page.query_selector(alvo): return
        except Exception: pass                                         # página ainda trocando
        login = login + 1 if tela_de_login(page) else 0                # tela de login em 2 olhadas seguidas (sem o formulário)
        if login >= 2: raise SessaoCaiu('a sessão do REDS caiu (voltou a tela de login)')
        page.wait_for_timeout(250)
    try: page.wait_for_selector(alvo, state='attached', timeout=2000)
    except Exception:
        if tela_de_login(page): raise SessaoCaiu('a sessão do REDS caiu (voltou a tela de login)')
        raise


JS_MARCAR = r"""() => { window.__roboAntes = 1; window.__roboMud = 0;
  try { new MutationObserver(() => { window.__roboMud++; }).observe(document.body, { childList: true, subtree: true, characterData: true }); } catch (e) {} }"""


def submeter(page, seletor, limite_s=90):
    """Clica em "buscar" e espera a resposta, sem esperar a rede ficar parada à toa: a página nova carregada (o comum), ou
    a lista desenhada na mesma página; se a mesma página mudou e a rede parou sem lista nenhuma, a pesquisa veio vazia."""
    try: page.evaluate(JS_MARCAR)
    except Exception: pass
    clicar(page.locator(seletor).first, 15000, 'buscar')
    t0 = time.time(); fim = t0 + limite_s
    while time.time() < fim:
        try: nova, mud = page.evaluate('() => [!window.__roboAntes, window.__roboMud || 0]')
        except Exception: nova, mud = False, 0                          # trocando de página
        if nova:
            try: page.wait_for_load_state('load', timeout=max(1000, int((fim - time.time()) * 1000)))
            except Exception: pass
            return 'pagina'
        if reds_da_pagina(page) or total_da_pagina(page): return 'mesma'
        if mud and time.time() - t0 > 1.5:                              # a mesma página mudou: rede parada = terminou
            try:
                page.wait_for_load_state('networkidle', timeout=3000)
                if page.evaluate('() => !!window.__roboAntes'): return 'mesma'
            except Exception: pass
        page.wait_for_timeout(200)
    raise RuntimeError('o REDS não respondeu à pesquisa em %d s' % limite_s)


def pdf_de_outro(dados, reds):
    """O PDF traz o número de OUTRO registro (e não o pedido)? Sem número nenhum no texto não dá para dizer: aceita."""
    try: nums = set(re.findall(r'\d{4}-\d{9}-\d{3}', texto_pdf(dados)))
    except Exception: return False
    return bool(nums) and reds not in nums


def buscar(robo, page, c, jan, regras, orgao=None, nome='busca', abrir=None):
    """Uma pesquisa em Registros Históricos. regras = [(valor, [campos])]: os campos de uma regra recebem o mesmo valor;
    regras diferentes valem juntas (E). Ordena do mais novo para o mais velho (pela data da janela). Devolve (página, campos).
    abrir() abre o formulário (a sessão do robô refaz o login se o REDS derrubou)."""
    s = c['sel']
    d_ini, d_fim = jan.datas(c['formato_data'])
    if abrir: abrir()
    else: abrir_formulario(page, c)
    passos = []
    if jan.base == 'dataInclusao': passos.append({'acao': 'selecionar', 'alvo': {'css': s['data_filtro']}, 'valor': 'dataInclusao'})
    if orgao is not None: passos.append({'acao': 'selecionar', 'alvo': {'css': s['orgao']}, 'valor': str(orgao)})
    passos += [{'acao': 'definir', 'alvo': {'css': s['inicio']}, 'valor': d_ini}, {'acao': 'definir', 'alvo': {'css': s['fim']}, 'valor': d_fim}]
    if c.get('ordenar'):                                               # a seção "Ordenação" pode estar fechada: valor por JS
        passos += [{'acao': 'definir', 'alvo': {'css': s['ordem']}, 'valor': jan.base, 'opcional': True},
                   {'acao': 'definir', 'alvo': {'css': s['ordem_tipo']}, 'valor': 'true', 'opcional': True}]
    passos.append({'acao': 'clicar', 'alvo': {'text': s['secao']}})
    page = robo.executar(page, passos, {}, nome)
    disp = set(opcoes_campos(page, c))
    usados, passos = [], []
    for valor, campos in regras:
        cs = [x for x in campos if x in disp] if disp else campos[:1]     # sem ler a lista: só o campo principal
        if not cs: raise RuntimeError('o REDS não tem o campo "%s" na lista de pesquisa' % campos[0])
        passos.append({'acao': 'preencher', 'alvo': {'css': s['valor']}, 'valor': valor})
        for x in cs:
            passos += [{'acao': 'selecionar', 'alvo': {'css': s['campos']}, 'valor': x}, {'acao': 'clicar', 'alvo': {'css': s['inserir']}}]
        passos.append({'acao': 'clicar', 'alvo': {'css': s['regra']}})
        usados.append(cs)
    page = robo.executar(page, passos, {}, nome)
    submeter(page, s['buscar'])
    return page, usados


PREF_CLIQUE = {}


def clicar(loc, tempo=5000, tipo=None):
    """Clica; se o navegador recusar (botão coberto pela coluna fixa da tabela, escondido, fora da tela...), clica por
    JavaScript. Lembra, por tipo de botão, o jeito que funcionou."""
    def js():
        try: loc.evaluate('e => e.click()')
        except Exception as e:
            if 'context' not in str(e) and 'navigat' not in str(e).lower(): raise    # a página trocou: o clique valeu
        return 'js'
    if tipo and PREF_CLIQUE.get(tipo) == 'js': return js()
    try:
        loc.click(timeout=tempo); return 'clique'
    except Exception:
        r = js()
        if tipo: PREF_CLIQUE[tipo] = 'js'
        return r


JS_TOTAL = r"""() => { const t = document.body ? document.body.innerText : '';
  let m = t.match(/Registros?\s+(\d+)\s*(?:a|-|até)\s*(\d+)\s*\(?\s*(?:Total\s*:?|de)\s*(\d+)/i);
  if (m) return [+m[1], +m[2], +m[3]];
  m = t.match(/(\d+)\s+registros?\s+encontrad/i) || t.match(/Total\s+de\s+registros?\s*:?\s*(\d+)/i);
  return m ? [0, 0, +m[1]] : null; }"""
JS_REDS = r"""() => Array.from(document.querySelectorAll('td')).map(td => (td.innerText || '').trim()).filter(t => /^\d{4}-\d{9}-\d{3}$/.test(t))"""
# próxima página: o número (ou "Próxima", ">", seta) perto de "Página"/"Registros x a y"; vale <a> sem href, <span>, botão,
# imagem; nunca uma célula de linha de registro
JS_PROXIMA = r"""(n) => {
  const tx = e => (e.innerText || e.value || e.getAttribute('alt') || e.getAttribute('title') || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const deDado = e => { const tr = e.closest('tr'); return !!tr && Array.from(tr.cells || []).some(c => /^\s*\d{4}-\d{9}-\d{3}\s*$/.test(c.innerText || '')); };
  const perto = e => { let p = e.parentElement; for (let i = 0; i < 4 && p; i++, p = p.parentElement) { const t = p.innerText || ''; if (t.length > 700) break;
    if (/P[aá]gina|Registros?\s+\d+\s*(a|-|até)\s*\d+|Pr[oó]xim|Anterior|Primeir|[UÚ]ltim/i.test(t)) return true; } return false; };
  const clicavel = e => /^(A|BUTTON|INPUT)$/.test(e.tagName) || e.hasAttribute('onclick') || !!e.closest('a,button') || getComputedStyle(e).cursor === 'pointer';
  const folha = e => e.children.length === 0 || /^(A|BUTTON)$/.test(e.tagName);
  // fora do formulário de pesquisa (lá há um botão ">" que insere campo na regra) e fora das linhas de registro
  const todos = Array.from(document.querySelectorAll('a,span,button,input[type=button],input[type=submit],input[type=image],img,li,td,font,b,strong,div'))
    .filter(e => vis(e) && !deDado(e) && !e.closest('fieldset') && !/^(consultaEspecifica|inserir|inserirRegra|buscar)$/.test(e.name || ''));
  const marcar = (e, como) => { document.querySelectorAll('[data-robo-pag]').forEach(x => x.removeAttribute('data-robo-pag')); e.setAttribute('data-robo-pag', '1'); return como; };
  const num = todos.filter(e => tx(e) === String(n) && folha(e) && perto(e));
  if (num.length) return marcar(num.find(clicavel) || num[0], 'numero');
  for (const rx of [/^pr[oó]xim[ao]s?$/i, /^avan[cç]ar$/i, /^seguinte$/i, /^>$/, /^>>$/, /^»$/]) {
    const c = todos.filter(e => rx.test(tx(e)) && folha(e) && (/\w/.test(tx(e)) || perto(e)));     // setas só perto da paginação
    if (c.length) return marcar(c.find(clicavel) || c[0], 'proxima');
  }
  const img = todos.filter(e => (e.tagName === 'IMG' || e.type === 'image') && /next|pr[oó]xim|avan[cç]|seta.?dir|right|forward/i.test(e.getAttribute('src') || '') && perto(e));
  if (img.length) return marcar(img[0].closest('a') || img[0], 'seta');
  return '';
}"""


def em_frames(page, js, *arg):
    for fr in page.frames:
        try:
            r = fr.evaluate(js, *arg)
            if r: return fr, r
        except Exception: pass
    return None, None


def total_da_pagina(page):
    return em_frames(page, JS_TOTAL)[1]


def reds_da_pagina(page):
    out = []
    for fr in page.frames:
        try: out += fr.evaluate(JS_REDS) or []
        except Exception: pass
    return out


def proxima_pagina(page, n):
    """Vai para a página n. Devolve como achou o controle ('' = não achou; 'parada' = clicou e a lista não mudou)."""
    antes = reds_da_pagina(page)
    fr, alvo = em_frames(page, JS_PROXIMA, n)
    if not alvo: return ''
    clicar(fr.locator('[data-robo-pag="1"]').first, 4000, 'pagina')
    fim = time.time() + 20
    while time.time() < fim:
        try: page.wait_for_load_state('networkidle', timeout=3000)
        except Exception: pass
        agora = reds_da_pagina(page)
        if agora and agora != antes: return alvo
        page.wait_for_timeout(300)
    return 'parada'


def percorrer(robo, page, c, ao_ler=None, atributos=None, jan=None, info=None):
    """Lê as páginas do resultado. Com a lista do mais novo para o mais velho, para quando passar do início da janela.
    ao_ler(page, itens_da_pagina) roda antes de ir para a página seguinte. info recebe total, lidos, páginas e parada."""
    info = {} if info is None else info
    tot = total_da_pagina(page)
    info.update(total=tot[2] if tot else None, paginas=0, parouCedo=False)
    out, vistos, pagina, anteriores, ultimo = [], set(), 1, None, None
    while True:
        atuais, novos = [], []
        for reg in robo.registros_da_pagina(page):
            it = interpretar(reg)
            if not it: continue
            atuais.append(it['reds'])
            if it['reds'] in vistos: continue
            vistos.add(it['reds']); out.append(it); novos.append(it)
        if not atuais or atuais == anteriores: break
        anteriores, info['paginas'], info['naPagina'] = atuais, pagina, len(atuais)
        if atributos is not None:
            for fr in page.frames:
                try: atributos.update(fr.evaluate(JS_ATRIBUTOS) or {})
                except Exception: pass
        if ao_ler and novos: ao_ler(page, novos)
        if info['total'] and len(out) >= info['total']: break
        if jan and c.get('ordenar') and novos:                           # do mais novo para o mais velho? passou da janela?
            ds = [jan.quando(x) for x in novos]
            if all(ds) and all(ds[i] >= ds[i + 1] for i in range(len(ds) - 1)) and (ultimo is None or ultimo >= ds[0]):
                ultimo = ds[-1]
                if ds[-1] < jan.ini: info['parouCedo'] = True; break
            else: ultimo = None; jan = None                            # não está ordenado: lê tudo
        pagina += 1
        if pagina > int(c.get('paginas_max', 60)): info['limite'] = True; break
        como = proxima_pagina(page, pagina)
        if not como or como == 'parada': info['semProxima'] = como or 'nao achou'; break
        info['controle'] = como
    info['lidos'] = len(out)
    info['incompleta'] = bool(info['total'] and len(out) < info['total'] and not info['parouCedo'])
    info['suspeita'] = bool(not info['total'] and info.get('semProxima') and info.get('naPagina', 0) >= 10)
    return out


# atributos do botão "Ver" de cada registro (para aprender o endereço do PDF; ficam só na memória do robô)
JS_ATRIBUTOS = r"""() => {
  const tx = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim(), out = {};
  for (const tr of document.querySelectorAll('tr')) {
    const c = Array.from(tr.cells || []).find(c => /^\s*\d{4}-\d{9}-\d{3}\s*$/.test(c.innerText || ''));
    if (!c) continue;
    const v = Array.from(tr.querySelectorAll('input,button,a,img')).find(e => /^ver$/i.test(tx(e)) || /^ver$/i.test(e.getAttribute('title') || e.getAttribute('alt') || ''));
    if (!v) continue;
    let s = Array.from(v.attributes).map(a => a.name + '=' + a.value).join(' | ');
    if (v.form) s += ' || ' + Array.from(v.form.elements).filter(x => x.type === 'hidden' && x.name).map(x => x.name + '=' + x.value).join('&');
    out[c.innerText.trim()] = s.slice(0, 800);
  }
  return out;
}"""

# estrutura da página de resultado, para o diagnóstico (sem dados: números longos viram #)
JS_ESTRUTURA = r"""() => {
  const mask = s => String(s || '').replace(/\d{5,}/g, m => '#'.repeat(m.length)).replace(/\d{4}-#+-\d{3}/g, '####-#########-###');
  const tx = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
  const html = e => mask(e.outerHTML).replace(/\s+/g, ' ').slice(0, 500);
  const out = {};
  const ver = Array.from(document.querySelectorAll('input,button,a,img')).filter(e => /^ver$/i.test(tx(e)) || /^ver$/i.test(e.getAttribute('title') || e.getAttribute('alt') || ''));
  if (ver.length) {
    const v = ver.find(e => e.offsetParent || e.getClientRects().length) || ver[0];
    out.ver = { quantos: ver.length, visiveis: ver.filter(e => e.offsetParent || e.getClientRects().length).length, html: html(v).slice(0, 300),
      form: v.form ? mask((v.form.getAttribute('method') || 'get') + ' ' + (v.form.getAttribute('action') || '') + ' target=' + (v.form.target || '')) : '',
      pais: (() => { const r = []; let p = v.parentElement; for (let i = 0; i < 7 && p; i++, p = p.parentElement) r.push(p.tagName + (p.className ? '.' + String(p.className).split(' ').slice(0, 2).join('.') : '') + (p.id ? '#' + p.id : '')); return r.join(' < '); })() };
    const r = v.getBoundingClientRect(), topo = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    out.ver.cobertoPor = topo && topo !== v && !v.contains(topo) ? topo.tagName + (topo.className ? '.' + String(topo.className).split(' ')[0] : '') : '';
  }
  const pag = Array.from(document.querySelectorAll('div,td,span,p,table')).filter(e => /P[aá]gina/i.test(e.innerText || '') && (e.innerText || '').length < 200);
  pag.sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
  if (pag[0]) out.paginacao = html(pag[0]).slice(0, 400);
  const nums = Array.from(document.querySelectorAll('a,span,td,b,font,li,input')).filter(e => /^\s*[2-9]\s*$/.test(e.innerText || e.value || '') && !e.closest('fieldset'));
  if (nums[0]) out.numero = html(nums[0].parentElement || nums[0]).slice(0, 400);
  const exp = Array.from(document.querySelectorAll('a,img,input,button')).filter(e => /csv|excel|xls|planilha|pdf|export/i.test([e.title, e.alt, e.className, e.getAttribute('src'), e.getAttribute('href'), e.getAttribute('onclick'), e.innerText].join(' ')));
  out.exportar = exp.slice(0, 4).map(e => html(e).slice(0, 220));
  const sel = Array.from(document.querySelectorAll('select')).map(s => (s.name || s.id) + '=' + s.value);
  out.selects = sel.join(' ').slice(0, 400);
  const t = (document.body.innerText.match(/Registros\s+\d+\s+a\s+\d+\s*\(\s*Total[^)]*\)/i) || [''])[0];
  out.total = t;
  return out;
}"""


def estrutura(page):
    fr, r = em_frames(page, JS_ESTRUTURA)
    return r or {}


JS_VER = r"""(reds) => {
  const tx = e => (e.innerText || e.value || '').replace(/\s+/g, ' ').trim();
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  let achado = null;
  for (const tr of document.querySelectorAll('tr')) {
    const cs = Array.from(tr.cells || []);
    if (!cs.some(c => tx(c) === reds)) continue;
    const alvo = Array.from(tr.querySelectorAll('input,button,a,img')).find(e => /^ver$/i.test(tx(e)) || /^ver$/i.test(e.getAttribute('title') || e.getAttribute('alt') || ''));
    if (!alvo) continue;
    if (!achado || (vis(alvo) && !vis(achado))) achado = alvo;
    if (vis(alvo)) break;
  }
  if (!achado) return false;
  document.querySelectorAll('[data-robo-ver]').forEach(x => x.removeAttribute('data-robo-ver'));
  achado.setAttribute('data-robo-ver', reds); return true;
}"""


def caminho(url):
    """Só o caminho e os nomes dos parâmetros (para o diagnóstico, sem valores)."""
    u = url.split('//', 1)[-1]
    p = '/' + u.split('/', 1)[-1] if '/' in u else '/'
    base, _, q = p.partition('?')
    return base[:80] + ('?' + '&'.join(sorted({x.split('=')[0] for x in q.split('&') if x})) if q else '')


JS_SEGUNDO = r"""() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const c = Array.from(document.querySelectorAll('a,button,input,img,span')).filter(e => vis(e) && /pdf|imprimir|gerar|baixar|download/i.test(
    [e.title, e.alt, e.value, e.getAttribute('src'), e.getAttribute('href'), e.getAttribute('onclick'), e.children.length ? '' : e.innerText].join(' ')));
  if (!c.length) return false;
  const e = c[0].tagName === 'IMG' && c[0].closest('a') ? c[0].closest('a') : c[0];
  document.querySelectorAll('[data-robo-seg]').forEach(x => x.removeAttribute('data-robo-seg')); e.setAttribute('data-robo-seg', '1'); return true;
}"""


TIPOS_REDE = ('document', 'xhr', 'fetch', 'other')


def capturar(page, marca_js, arg, aceita, espera_s, sel='[data-robo-ver]', tipo='ver', segundo=True):
    """Clica no elemento marcado por marca_js (atributo do seletor sel) e pega o arquivo que vier.

    1º jeito (o que vale no GitHub): pega o arquivo NA REDE, antes de chegar à aba. O Chrome do GitHub (headless novo)
       abre o PDF "inline" no visualizador dele, e aí o corpo da resposta não pode mais ser lido — foi o que deixou todos os
       registros "sem PDF" na v1.1. Por isso, durante o clique, as respostas de página/arquivo passam pelo robô
       (route.fetch) e seguem iguais para a aba.
    2º: download; 3º: a resposta (e, se o corpo sumiu, pede de novo com a mesma sessão); 4º: página (HTML) com botão de
    PDF/imprimir (clica nele uma vez). Nunca derruba a consulta: devolve (bytes ou None, detalhes)."""
    fr, ok = em_frames(page, marca_js, arg)
    if not ok: return None, {'erro': 'botão não achado'}
    alvo = fr.locator(sel).first
    ctx, pego, dg, pedidos = page.context, {}, {'abas': 0, 'respostas': [], 'dialogos': []}, []

    def na_rede(route):
        rq = route.request
        try: tp = rq.resource_type
        except Exception: tp = 'other'
        if tp not in TIPOS_REDE or 'arquivo' in pego: return route.fallback()
        try: resp = route.fetch(max_redirects=0, timeout=max(30000, int(espera_s * 1000)))   # mesmo endereço e cookies da aba
        except Exception: return route.fallback()
        try:
            corpo = resp.body()
            ct = (resp.headers.get('content-type') or '').lower()
            if corpo and 'arquivo' not in pego and aceita(corpo, ct): pego['arquivo'] = (corpo, rq.url, rq.method, rq.post_data or '')
            route.fulfill(response=resp, body=corpo)
        except Exception:
            try: route.fulfill(response=resp)
            except Exception: pass

    def ao_baixar(d): pego.setdefault('download', d)

    def ao_pedir(rq):
        try:
            if rq.is_navigation_request() or rq.resource_type == 'document': pedidos.append((rq.method, rq.url, rq.post_data or ''))
        except Exception: pass

    def ao_responder(r):
        try:
            ct = (r.headers.get('content-type') or '').lower()
            if len(dg['respostas']) < 6: dg['respostas'].append([ct.split(';')[0], caminho(r.url)])
            if 'resp' not in pego and aceita(None, ct): pego['resp'] = r
        except Exception: pass

    def ao_abrir(p):
        dg['abas'] += 1; pego.setdefault('abas', []).append(p); p.on('download', ao_baixar)

    def ao_dialogo(d):
        dg['dialogos'].append(d.type)
        try: d.accept()
        except Exception: pass

    def de_novo(r):
        """O corpo da resposta sumiu (visualizador de PDF): pede o mesmo endereço de novo, com os cookies da sessão."""
        rq = r.request
        hd = {k: v for k, v in (rq.headers or {}).items() if k.lower() in ('content-type', 'referer')}
        r2 = ctx.request.fetch(rq.url, method=rq.method, data=rq.post_data_buffer if rq.method == 'POST' else None, headers=hd, timeout=60000)
        return r2.body() if r2.ok else b''

    rede = True
    try: ctx.route('**/*', na_rede)
    except Exception: rede = False
    page.on('download', ao_baixar); page.on('dialog', ao_dialogo)
    ctx.on('response', ao_responder); ctx.on('page', ao_abrir); ctx.on('request', ao_pedir)
    dados = None
    try:
        dg['clique'] = clicar(alvo, 4000, tipo)
        fim, viu_resp = time.time() + espera_s, 0
        while time.time() < fim and 'download' not in pego and 'arquivo' not in pego:
            if 'resp' in pego:                                            # a aba com o arquivo pode virar download: espera um pouco
                viu_resp = viu_resp or time.time()
                if time.time() - viu_resp > 2.5: break
            page.wait_for_timeout(150)
        if 'arquivo' in pego:
            dados, dg['url'], dg['metodo'], dg['post'] = pego['arquivo']; dg['via'] = 'rede'
        if not dados and 'download' not in pego and 'resp' in pego:
            r = pego['resp']
            try:
                dados = r.body(); dg['via'], dg['url'] = 'resposta', r.url
                dg['metodo'], dg['post'] = r.request.method, r.request.post_data or ''
            except Exception: dados = None
            if not (dados and aceita(dados, '')):
                try:
                    d2 = de_novo(r)
                    if d2 and aceita(d2, ''): dados, dg['via'], dg['url'] = d2, 'de-novo', r.url; dg['metodo'], dg['post'] = r.request.method, r.request.post_data or ''
                except Exception as e: dg['deNovo'] = curto(e)
            if not (dados and aceita(dados, '')):
                dados = None
                while time.time() < fim and 'download' not in pego: page.wait_for_timeout(150)
        if not dados and 'download' in pego:
            d = pego['download']
            try:
                with open(d.path(), 'rb') as f: dados = f.read()
                dg['via'], dg['url'] = 'download', d.url
                met = [x for x in pedidos if x[1] == d.url]
                dg['metodo'], dg['post'] = (met[-1][0], met[-1][2]) if met else ('GET', '')
            except Exception as e: dg['erro'] = 'download: ' + str(e)[:120]
        if not dados:                                                     # abriu uma aba com a página (HTML) do arquivo?
            for p in pego.get('abas', []):
                try:
                    p.wait_for_load_state('load', timeout=8000); html = p.content().encode('utf-8')
                    if aceita(html, 'text/html'): dados, dg['via'], dg['url'] = html, 'aba', p.url; break
                except Exception: pass
        if not dados and segundo and 'download' not in pego:              # página de visualização com botão de PDF/imprimir
            if rede:
                try: ctx.unroute('**/*', na_rede); rede = False
                except Exception: pass
            for p in pego.get('abas', []) or [page]:
                try:
                    if p is not page: p.wait_for_load_state('load', timeout=8000)
                    if p is page and reds_da_pagina(page): continue        # ainda é a lista: não há 2º passo
                    if not em_frames(p, JS_SEGUNDO)[1]: continue
                    d2, g2 = capturar(p, JS_SEGUNDO, None, aceita, max(10, espera_s // 2), '[data-robo-seg]', 'segundo', False)
                    dg['segundo'] = {k: v for k, v in g2.items() if k in ('via', 'erro', 'respostas', 'abas', 'clique')}
                    if d2: dados, dg['via'] = d2, 'segundo-' + g2.get('via', '?'); dg['url'], dg['metodo'], dg['post'] = g2.get('url', ''), g2.get('metodo', 'GET'), g2.get('post', ''); break
                except Exception as e: dg['segundo'] = {'erro': str(e).splitlines()[0][:120]}
    except Exception as e:
        dg['erro'] = 'clique: ' + str(e).splitlines()[0][:160]
    finally:
        if rede:
            try: ctx.unroute('**/*', na_rede)
            except Exception: pass
        for p in pego.get('abas', []):
            try: p.close()
            except Exception: pass
        for ev, fn, onde in (('download', ao_baixar, page), ('dialog', ao_dialogo, page), ('response', ao_responder, ctx), ('page', ao_abrir, ctx), ('request', ao_pedir, ctx)):
            try: onde.remove_listener(ev, fn)
            except Exception: pass
    if dados and aceita(dados, ''): return dados, dg
    dg.setdefault('erro', 'o clique não trouxe o arquivo em %d s' % espera_s)
    return None, dg


def capturar_pdf(page, reds, espera_s):
    """Clica em "Ver" do registro e pega o PDF (download, resposta application/pdf ou outra aba)."""
    return capturar(page, JS_VER, reds, lambda b, ct: (b[:5] == b'%PDF-') if b is not None else ('pdf' in ct), espera_s)



JS_EXPORTAR = r"""() => {
  const vis = e => !!(e.offsetParent || e.getClientRects().length);
  const txt = e => [e.title, e.alt, e.value, e.className, e.getAttribute('src'), e.getAttribute('href'), e.getAttribute('onclick'), e.children.length ? '' : e.innerText].join(' ');
  const c = Array.from(document.querySelectorAll('a,img,input,button,span')).map(e => [e, txt(e)])
    .filter(([e, t]) => /csv|excel|xls|planilha/i.test(t) && !/\bpdf\b/i.test(t))
    .map(([e, t]) => [e, (/csv/i.test(t) ? 0 : /xls|excel/i.test(t) ? 1 : 2) + (vis(e) ? 0 : 5)]);
  if (!c.length) return false;
  c.sort((a, b) => a[1] - b[1]);
  let e = c[0][0];
  if ((e.tagName === 'IMG' || e.tagName === 'SPAN') && e.closest('a')) e = e.closest('a');
  document.querySelectorAll('[data-robo-exp]').forEach(x => x.removeAttribute('data-robo-exp'));
  e.setAttribute('data-robo-exp', '1'); return true;
}"""


def _linhas_html(txt):
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(); self.linhas, self.linha, self.cel = [], None, None
        def handle_starttag(self, tag, a):
            if tag == 'tr': self.linha = []
            elif tag in ('td', 'th') and self.linha is not None: self.cel = []
        def handle_endtag(self, tag):
            if tag in ('td', 'th') and self.cel is not None and self.linha is not None: self.linha.append(re.sub(r'\s+', ' ', ''.join(self.cel)).strip()); self.cel = None
            elif tag == 'tr' and self.linha is not None: self.linhas.append(self.linha); self.linha = None
        def handle_data(self, d):
            if self.cel is not None: self.cel.append(d)
    p = P(); p.feed(txt); return p.linhas


def ler_exportacao(dados, cab_tela=None):
    """Arquivo exportado pelo REDS (CSV, xlsx, xls ou HTML "xls") -> registros no mesmo formato da lista. Sem linha de
    cabeçalho no arquivo, alinha as colunas pelo cabeçalho da tela (a coluna do nº do REDS como âncora)."""
    linhas = []
    if dados[:2] == b'PK':
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(dados), read_only=True, data_only=True)
        for ws in wb.worksheets: linhas += [['' if v is None else str(v) for v in r] for r in ws.iter_rows(values_only=True)]
    elif dados[:4] == b'\xd0\xcf\x11\xe0':
        import xlrd
        wb = xlrd.open_workbook(file_contents=dados)
        for sh in wb.sheets(): linhas += [[str(sh.cell_value(i, j)) for j in range(sh.ncols)] for i in range(sh.nrows)]
    else:
        txt = None
        for cod in ('utf-8', 'cp1252', 'latin-1'):
            try: txt = dados.decode(cod); break
            except UnicodeDecodeError: pass
        if '<t' in txt.lower(): linhas = _linhas_html(txt)
        else:
            import csv
            sep = ';' if txt.count(';') > txt.count(',') else ('\t' if txt.count('\t') > txt.count(',') else ',')
            linhas = list(csv.reader(io.StringIO(txt), delimiter=sep))
    cab, out = None, []
    for l in linhas:
        if any('REDS' in chave(x) for x in l) and any('NATUREZA' in chave(x) for x in l): cab = l; continue
        k = next((i for i, x in enumerate(l) if RE_REDS.fullmatch((x or '').strip())), None)
        if k is None: continue
        c_ = cab
        if c_ is None and cab_tela:
            j = next((i for i, h in enumerate(cab_tela) if 'NUMERODOREDS' in chave(h) or 'REDS' in chave(h)), None)
            if j is not None: c_ = ([''] * max(0, k - j)) + list(cab_tela[max(0, j - k):])
        it = interpretar({'cab': c_ or [], 'cells': [str(x) for x in l], 'extras': []})
        if it: out.append(it)
    return out


def exportar_lista(page, cab_tela=None, espera_s=45):
    """Clica em exportar (CSV/Excel) na página de resultado e lê o arquivo. Devolve (registros, detalhes)."""
    def aceita(b, ct):
        if b is None: return any(x in ct for x in ('excel', 'spreadsheet', 'csv', 'octet-stream', 'text/plain'))
        return bool(re.search(rb'\d{4}-\d{9}-\d{3}', b[:2000000])) and b[:5] != b'%PDF-'
    dados, dg = capturar(page, JS_EXPORTAR, None, aceita, espera_s, '[data-robo-exp]', 'exportar', False)
    try:
        if not reds_da_pagina(page): page.go_back(wait_until='networkidle', timeout=30000)   # o arquivo abriu na própria aba
    except Exception: pass
    if not dados: return [], {k: v for k, v in dg.items() if k not in ('url', 'post')}
    try: regs = ler_exportacao(dados, cab_tela)
    except Exception as e: return [], {'erro': 'leitura: ' + str(e)[:120], 'via': dg.get('via')}
    return regs, {'via': dg.get('via'), 'formato': dados[:4].hex(), 'lidos': len(regs)}


class Atalho:
    """Aprende, no 1º "Ver", o endereço que gera o PDF (com o nº do REDS ou com o identificador que está no botão) e baixa
    os outros direto, vários ao mesmo tempo, com a mesma sessão do navegador. Cada PDF é conferido: o nº do REDS tem de
    estar dentro dele; se não estiver, o atalho é desligado e o robô volta ao clique."""
    def __init__(self):
        self.molde, self.sessao = None, None

    def aprender(self, reds, dg, atributos):
        url, met, post = dg.get('url') or '', (dg.get('metodo') or 'GET').upper(), dg.get('post') or ''
        if not url.startswith('http') or met not in ('GET', 'POST'): return False
        for alvo, marca in ((reds, '{reds}'), (reds.replace('-', ''), '{digitos}')):
            if alvo in url or alvo in post:
                self.molde = {'metodo': met, 'url': url.replace(alvo, marca), 'post': post.replace(alvo, marca), 'tipo': marca}
                return True
        args = re.findall(r'[A-Za-z0-9_.\-]+', atributos.get(reds) or '')
        for tok in sorted(set(re.findall(r'[A-Za-z0-9_.\-]{3,}', url.split('//', 1)[-1] + ' ' + post)), key=len, reverse=True):
            if tok in args and re.search(r'\d', tok):
                self.molde = {'metodo': met, 'url': url.replace(tok, '{tok}'), 'post': post.replace(tok, '{tok}'), 'tipo': '{tok}', 'pos': args.index(tok), 'n': len(args)}
                return True
        return False

    def pedido(self, reds, atributos):
        m = self.molde
        if not m: return None
        if m['tipo'] == '{reds}': v = reds
        elif m['tipo'] == '{digitos}': v = reds.replace('-', '')
        else:
            args = re.findall(r'[A-Za-z0-9_.\-]+', atributos.get(reds) or '')
            if len(args) != m['n']: return None
            v = args[m['pos']]
        return m['metodo'], m['url'].replace(m['tipo'], v), m['post'].replace(m['tipo'], v)

    def preparar(self, page):
        s = requests.Session()
        for ck in page.context.cookies():
            s.cookies.set(ck['name'], ck['value'], domain=ck.get('domain') or None, path=ck.get('path') or '/')
        try: ua = page.evaluate('navigator.userAgent').replace('HeadlessChrome', 'Chrome')
        except Exception: ua = 'Mozilla/5.0'
        s.headers.update({'User-Agent': ua, 'Referer': page.url, 'Accept': 'application/pdf,*/*;q=0.8'})
        self.sessao = s

    def baixar_ctx(self, ctx, reds, atributos):
        """Baixa direto pelo endereço aprendido, com a própria sessão do navegador (cookies sempre atuais)."""
        q = self.pedido(reds, atributos)
        if not q: return None
        met, url, post = q
        r = ctx.request.fetch(url, method=met, data=post.encode('utf-8') if met == 'POST' else None, timeout=60000,
                              headers={'Content-Type': 'application/x-www-form-urlencoded'} if met == 'POST' else None)
        corpo = r.body()
        return corpo if r.ok and corpo[:5] == b'%PDF-' else None

    def baixar(self, reds, atributos):
        q = self.pedido(reds, atributos)
        if not q or not self.sessao: return None
        met, url, post = q
        r = self.sessao.request(met, url, data=post.encode('utf-8') if met == 'POST' else None, timeout=60,
                                headers={'Content-Type': 'application/x-www-form-urlencoded'} if met == 'POST' else None)
        return r.content if r.status_code == 200 and r.content[:5] == b'%PDF-' else None


def abrir_navegador(pw, a, c):
    """Chrome que já vem no GitHub (CHROME_PATH) — sem baixar nada; se não abrir, baixa o Chromium do Playwright."""
    try:
        nav, page = RR.abrir(pw, a)
    except Exception as e:
        if not os.environ.get('CHROME_PATH'): raise
        log('  (Chrome do sistema não abriu: %s; baixando o Chromium)' % str(e).splitlines()[0][:120])
        os.environ.pop('CHROME_PATH', None)
        subprocess.run([sys.executable, '-m', 'playwright', 'install', 'chromium'], check=False)
        nav, page = RR.abrir(pw, a)
    if c.get('bloquear'):                                               # só os endereços de imagem/fonte passam pelo filtro
        page.context.route(re.compile(r'\.(png|jpe?g|gif|ico|svg|bmp|webp|woff2?|ttf|otf|eot)(\?.*)?$', re.I), lambda rota: rota.abort())
    return nav, page


# ----------------------------------------------------------------------------------------------- consulta completa
def usuario_conectado(page):
    try:
        t = page.inner_text('body', timeout=5000)
        m = re.search(r'Usu[aá]rio conectado:\s*\(([^)]*)\)\s*-\s*([^\n\r]{3,80})', t)
        if m: return m.group(1).strip(), m.group(2).strip()
    except Exception: pass
    return '', ''


def publico(it):
    """O que vai para o app (sem campos internos)."""
    return {k: v for k, v in it.items() if not k.startswith('_') and k not in ('codBusca', 'semResp')}


DO_PDF = ('pdf', 'orgaoPdf', 'unidadeResp', 'unidadeArea', 'natCod', 'natDesc', 'natStatus', 'natSec', 'secundarias', 'vitimas', 'agenteServico',
          'equipe', 'relator', 'digitador', 'ind', 'naArea')
DO_GRUPO = ('grupo', 'orgaoFinal')


def curto(e):
    return str(e).splitlines()[0][:160] if str(e) else e.__class__.__name__


def resumo_busca(info):
    return {k: info[k] for k in ('total', 'lidos', 'paginas', 'parouCedo', 'semProxima', 'controle', 'incompleta', 'suspeita', 'limite', 'viaExportacao', 'exportacao', 's') if info.get(k) not in (None, False, '', {})}


def ler_tudo(robo, page, c, jan, regras, nome, orgao=None, ao_ler=None, atributos=None, diag=None, abrir=None, prazo=None):
    """Uma pesquisa lida até o fim: páginas; se não chegar ao total do REDS, a exportação (CSV/Excel)."""
    t0 = time.time()
    page, usados = buscar(robo, page, c, jan, regras, orgao=orgao, nome=nome, abrir=abrir)
    if diag is not None and nome == 'area':
        try: diag['estrutura'] = estrutura(page)
        except Exception: pass
    info = {}
    lista = percorrer(robo, page, c, ao_ler=ao_ler, atributos=atributos, jan=jan, info=info)
    if c.get('exportar') and (info.get('incompleta') or info.get('suspeita')) and (not prazo or time.time() < prazo):
        try:
            regs0 = robo.registros_da_pagina(page)
            regs, dx = exportar_lista(page, (regs0[0].get('cab') if regs0 else None))
            info['exportacao'] = dx
            vistos = {x['reds'] for x in lista}
            novos = [x for x in regs if x['reds'] not in vistos]
            lista += novos; info['viaExportacao'] = len(novos)
            if info.get('total'): info['incompleta'] = len(lista) < info['total']
            elif regs: info['incompleta'] = info['suspeita'] = False
            else: info['incompleta'] = True
        except Parar: raise
        except Exception as e: info['exportacao'] = {'erro': curto(e)}; info['incompleta'] = True
    info['s'] = round(time.time() - t0, 1)
    if diag is not None: diag.setdefault('buscas', {})[nome] = resumo_busca(info)
    return page, lista, info, usados


def voltar_lista(robo, pg):
    """Se o "Ver" trocou a própria página, volta para a lista."""
    try:
        if not robo.registros_da_pagina(pg): pg.go_back(wait_until='load', timeout=30000)
    except Exception: pass


ORDEM_GRUPO = {'BPM': 0, 'PC': 1, 'MAMB': 2, 'BM': 3, 'PMOUTRA': 4, 'OUTROS': 5, 'AREA': 6}


class Sessao:
    """Uma sessão do robô no REDS com o login do militar. Faz o primeiro trabalho (a consulta, ou PDFs pedidos depois) e fica
    conectada atendendo os próximos trabalhos desse militar (PDF sob demanda, nova consulta) enquanto o app mandar: o app
    encerra quando a página fecha, o militar clica em "Sair" ou passa o tempo máximo. A senha fica só na memória deste
    processo (para entrar de novo se o REDS derrubar a sessão) e some quando o robô termina."""

    def __init__(self, a, c, app, p, cred, inicio):
        self.a, self.c, self.app, self.p, self.cred, self.inicio = a, c, app, p, cred, inicio
        self.uni, self.unome = p.get('unidade') or 'M3853', p.get('unidadeNome') or '65º BPM'
        self.municipios = {norm_mun(x) for x in p.get('municipios') or [] if norm_mun(x)}
        self.sid = p.get('sid') or ''
        self.limite = inicio + int((p.get('sessao') or {}).get('maxS') or c.get('sessao_max_s', 2700))
        self.atalho, self.atributos, self.trava = Atalho(), {}, threading.RLock()
        self.por = {}                                                   # pedido -> {reds: item}
        self.nav = self.page = self.robo = None
        self.nome_reds, self.diag = '', {}
        self.dsessao = {'versao': VERSAO, 'trabalhos': 0, 'relogin': 0, 'pdf': {'pedidos': 0, 'ok': 0, 'direto': 0, 'clique': 0, 'falhas': 0, 'pc': 0, 'atalho': '', 'exemplos': []}}

    # ------------------------------------------------------------------ navegador e login
    def entrar(self):
        self.page.goto(self.c['url'], wait_until='domcontentloaded', timeout=60000)
        self.page = self.robo.executar(self.page, self.c['login'], {'usuario': self.cred['u'], 'senha': self.cred['s']}, 'login')
        RR.conferir_login(self.page)

    def formulario(self):
        """Abre a pesquisa; se a sessão do REDS caiu, entra de novo (uma vez)."""
        try: abrir_formulario(self.page, self.c)
        except SessaoCaiu:
            log('  (a sessão do REDS caiu; entrando de novo)'); self.dsessao['relogin'] += 1
            self.entrar(); abrir_formulario(self.page, self.c)

    def ler(self, jan, regras, nome, **kw):
        page, lista, info, usados = ler_tudo(self.robo, self.page, self.c, jan, regras, nome, abrir=self.formulario, **kw)
        self.page = page
        return lista, info, usados

    def rodar(self):
        motivo, saida = 'fim', 'ok'
        trabalhos = self.p.get('trabalhos') or [{'tipo': 'consulta', 'pedido': self.app.pedido, 'ini': self.p.get('ini'), 'fim': self.p.get('fim'), 'base': self.p.get('base')}]
        ped0 = trabalhos[0].get('pedido') or self.app.pedido
        with sync_playwright() as pw:
            self.nav, self.page = abrir_navegador(pw, self.a, self.c)
            self.robo = RR.Robo(self.c, self.a.calibrar)
            try:
                if trabalhos[0]['tipo'] == 'consulta': self.app.progresso('LOGIN', 'Entrando no REDS com o seu usuário', 18, pedido=ped0)
                try: self.entrar()
                except RuntimeError as e:
                    recusou = 'recusou' in str(e)
                    self.app.esvaziar(30)
                    self.app.chamar('erro', pedido=ped0, tipo='LOGIN', recusado=recusou, sid=self.sid,
                                    msg='O REDS recusou o usuário ou a senha. O robô parou sem tentar de novo, para não bloquear o seu acesso.' if recusou
                                    else 'O login no REDS não passou da tela de usuário e senha (o SISP pode estar fora do ar).')
                    log('Login recusado' if recusou else 'Login não concluído')
                    motivo, saida = 'login', 'login'
                    return saida
                self.nome_reds = usuario_conectado(self.page)[1]
                while True:
                    for t in trabalhos: self.fazer(t)
                    if not self.sid: break                                # app antigo: uma consulta só
                    if time.time() > self.limite: motivo = 'tempo máximo da sessão'; break
                    r = self.aguardar()
                    if r is None: motivo = 'o app parou de responder'; break
                    if r.get('encerrar'): motivo = r.get('motivo') or 'encerrada pelo app'; break
                    trabalhos = r.get('trabalhos') or []
                    if not trabalhos: time.sleep(2)
                return saida
            except Exception as e:
                if self.a.calibrar:
                    try: RR.diag_formulario(self.page); self.robo.foto(self.page, 'erro')
                    except Exception: pass
                log('ERRO na sessão: ' + curto(e)); motivo, saida = 'erro: ' + curto(e), 'erro'
                return saida
            finally:
                try: self.app.esvaziar(60)
                except Exception: pass
                if self.sid:
                    try: self.app.chamar('sessaoFim', pedido=ped0, sid=self.sid, motivo=motivo[:160], diag=self.dsessao)
                    except Exception: pass
                log('Sessão encerrada (%s): %d trabalho(s), %d PDF(s)' % (motivo, self.dsessao['trabalhos'], self.dsessao['pdf']['ok']))
                try: self.nav.close()
                except Exception: pass

    def aguardar(self):
        """Pergunta ao app se há trabalho novo (e se a sessão continua). None se o app parou de responder."""
        for i in range(20):
            try: return self.app.chamar('trabalho', tentativas=2, sid=self.sid)
            except Parar: return {'encerrar': True, 'motivo': 'consulta apagada'}
            except Exception as e:
                log('  (app sem resposta: %s)' % curto(e)); time.sleep(3)
        return None

    def fazer(self, t):
        ped, tipo = t.get('pedido'), t.get('tipo')
        self.dsessao['trabalhos'] += 1
        try:
            if tipo == 'consulta': self.consulta(t)
            elif tipo == 'pdf': self.pdfs(t)
        except Parar as e:
            log('  (pedido parado pelo app: %s)' % e)
        except Exception as e:
            log('ERRO (%s): %s' % (tipo, curto(e)))
            try:
                self.app.esvaziar(60)
                if tipo == 'consulta': self.app.chamar('fim', pedido=ped, ok=False, msg='O robô parou: ' + str(e).splitlines()[0][:220], diag=self.diag)
                else:
                    for it in t.get('itens') or []: self.app.mandar('pdfErro', pedido=ped, reds=it.get('reds'), msg='O robô não conseguiu pegar o PDF: ' + curto(e))
            except Exception: pass

    # ------------------------------------------------------------------ consulta
    def consulta(self, t):
        ped, c, app = t['pedido'], self.c, self.app
        jan = Janela(t['ini'], t['fim'], t.get('base'))
        prazo = time.time() + int(c.get('tempo_total_s', 1200))
        self.diag = diag = {'versao': VERSAO, 'base': jan.base, 'buscas': {}, 'fases': {}, 'tempos': {}, 'estrutura': {}, 'extras': []}
        t0 = time.time()
        marca = lambda k: diag['tempos'].__setitem__(k, round(time.time() - t0, 1))
        prog = lambda etapa, msg, pct, **kw: app.progresso(etapa, msg, pct, pedido=ped, **kw)
        por = self.por.setdefault(ped, {})
        uni, unome = self.uni, self.unome

        def fase(nome, fn, *args):
            try: return fn(*args)
            except Parar: raise
            except Exception as e:
                diag['fases'][nome] = curto(e); log('  (etapa "%s" falhou: %s)' % (nome, curto(e)))
                return None

        prog('AREA', 'Entrou no REDS; buscando os registros da área do %s' % unome, 26, nome=self.nome_reds, logado=True)
        # 1) todos os registros da área, até o fim da lista (páginas; se faltar, exportação)
        extras = list(c.get('campos_extras') or [])
        brutos, info, usados = self.ler(jan, [(uni, [c['campo_area']] + extras)], 'area', atributos=self.atributos, diag=diag, prazo=prazo)
        if not brutos and len(usados[0]) > 1:
            brutos, info, usados = self.ler(jan, [(uni, [c['campo_area']])], 'area2', atributos=self.atributos, diag=diag, prazo=prazo)
            if brutos: diag['extrasMudaramARegra'] = True
        diag['camposArea'] = usados[0]
        diag['extras'] = sorted({x[1] for b in brutos for x in b.get('_extras') or []})[:20]
        area = {b['reds'] for b in brutos}
        cidades_area = {municipio_bairro(b['endereco'])[0] for b in brutos} - {''}
        fora = 0

        def novo_item(b, na_area, por_cidade=False):
            mun, bairro = municipio_bairro(b['endereco'])
            it = dict(b, municipio=mun, bairro=bairro, grupoTipo=grupo_tipo(b['tipo']), naArea=na_area, pdf='', orgao='', doBpm=False)
            if por_cidade: it['areaPor'] = 'cidade'
            it.update(dados_das_extras(b)); regrupar(it, False); avaliar_indicadores(it); secundarias(it)
            return it

        for b in brutos:
            if jan.dentro(b): por[b['reds']] = novo_item(b, True)
            else: fora += 1
        itens = sorted(por.values(), key=lambda x: jan.quando(x) or dt.datetime.min)
        resumo = {'total': len(itens), 'foraDoHorario': fora, 'parouCedo': bool(info.get('parouCedo')), 'totalReds': info.get('total'), 'lidos': len(brutos),
                  'paginas': info.get('paginas'), 'incompleta': bool(info.get('incompleta') or info.get('suspeita')), 'viaExportacao': info.get('viaExportacao', 0)}
        app.mandar('lista', pedido=ped, itens=[publico(x) for x in itens], resumo=resumo, orgaos=False)
        marca('lista')
        aviso = (' — atenção: o REDS mostrou %d e o robô leu %d' % (info['total'], len(brutos))) if resumo['incompleta'] and info.get('total') else (' — atenção: a lista pode estar incompleta' if resumo['incompleta'] else '')
        prog('LISTA', '%d registro(s) no período%s' % (len(itens), (' (%d do dia fora do horário)' % fora) if fora and not info.get('parouCedo') else '') + aviso + ('; separando por órgão' if itens else ''), 40)

        # 2) órgãos: Polícia Civil e Bombeiros na área; e o que foi registrado por unidades do 65º BPM (dentro ou fora da área)
        conj, orgaos_ok = {}, True
        n_org = len(c['orgaos']) + 1
        for k, (sig, cod) in enumerate(c['orgaos'].items()):
            prog('ORGAOS', 'Separando por órgão (%s)' % {'PC': 'Polícia Civil', 'BM': 'Bombeiros', 'PM': 'Polícia Militar'}.get(sig, sig), 42 + 24 * k / n_org)
            r = fase('orgao-' + sig, lambda: self.ler(jan, [(uni, [c['campo_area']])], 'orgao-' + sig, orgao=cod, diag=diag, prazo=prazo))
            if r: l_, i_, _ = r; conj[sig] = {b['reds'] for b in l_}; orgaos_ok = orgaos_ok and not i_.get('incompleta')
            else: orgaos_ok = False
        prog('ORGAOS', 'Conferindo o que é do %s' % unome, 42 + 24 * (n_org - 1) / n_org)
        resp, resp_ok = None, False
        r = fase('resp', lambda: self.ler(jan, [(uni, [c['campo_resp']])], 'resp', atributos=self.atributos, diag=diag, prazo=prazo))
        if r: resp, i_, _ = r; resp_ok = not i_.get('incompleta')
        lista_cidades = self.municipios | {norm_mun(x) for x in cidades_area}
        with self.trava:
            for b in resp or []:
                if b['reds'] not in area and b['reds'] not in por and jan.dentro(b):
                    mun = municipio_bairro(b['endereco'])[0]               # registrado pelo 65º, sem vir na pesquisa da área
                    if not mun: por[b['reds']] = novo_item(b, None)
                    elif norm_mun(mun) in lista_cidades: por[b['reds']] = novo_item(b, True, por_cidade=True)
                    else: por[b['reds']] = novo_item(b, False)
            doR = {b['reds'] for b in resp or []}
            for r_, it in por.items():
                it['orgao'] = 'PC' if r_ in conj.get('PC', ()) else 'BM' if r_ in conj.get('BM', ()) else 'PM' if (r_ in doR or r_ in conj.get('PM', ())) else ''
                it['doBpm'] = r_ in doR
                if resp is None: it['semResp'] = True
                regrupar(it, True); avaliar_indicadores(it); secundarias(it)
            atual = [publico(it) for it in sorted(por.values(), key=lambda x: (ORDEM_GRUPO.get(x['grupo'], 9), jan.quando(x) or dt.datetime.min))]
            resumo.update(total=len(por), orgaosIncompletos=not (orgaos_ok and resp_ok))
        app.mandar('atualizar', pedido=ped, itens=atual, orgaos=True, resumo=resumo)
        marca('orgaos')

        # 3) naturezas dos indicadores também como secundária/terciária
        if c.get('buscar_naturezas', True) and por:
            achou_nat = 0
            for k, cod in enumerate(COD_BUSCA):
                if time.time() > prazo: diag['fases']['naturezas'] = 'tempo esgotado em %s' % cod; break
                prog('INDICADORES', 'Naturezas dos indicadores, também as secundárias (%s, %d de %d)' % (NOME_COD[cod], k + 1, len(COD_BUSCA)), 68 + 28 * k / len(COD_BUSCA))
                r = fase('nat-' + cod, lambda: self.ler(jan, [(uni, [c['campo_area']]), (cod, [c['campo_natureza']])], 'nat-' + cod, diag=diag, prazo=prazo))
                if not r: continue
                with self.trava:
                    for b in r[0]:
                        it = por.get(b['reds'])
                        if it: it['codBusca'] = sorted(set(it.get('codBusca') or []) | {cod}); avaliar_indicadores(it); secundarias(it); achou_nat += 1
            diag['buscas']['naturezas'] = achou_nat
            with self.trava: app.mandar('atualizar', pedido=ped, itens=[{'reds': r_, 'ind': it['ind'], 'secundarias': it.get('secundarias') or []} for r_, it in por.items()])
            marca('naturezas')

        app.esvaziar(pedido=ped)
        msg = '%d registro(s)' % len(por)
        if resumo['incompleta']: msg += aviso
        if resumo.get('orgaosIncompletos'): msg += ' — separação por órgão incompleta'
        marca('fim')
        diag['envioFalhas'] = app.falhas
        app.chamar('fim', pedido=ped, ok=True, msg=msg, diag=diag, municipiosArea=sorted(cidades_area)[:60])
        log('Pronto: ' + msg + ' — tempos: ' + json.dumps(diag['tempos']))

    # ------------------------------------------------------------------ PDF sob demanda
    def processar_pdf(self, ped, it, dados, via, dg=None):
        reds = it['reds']
        info = ler_pdf_reds(dados, reds)
        if not info.get('_confere') and (via == 'direto' or pdf_de_outro(dados, reds)):           # PDF de outro registro
            if via == 'direto': self.dsessao['pdf']['recusadosDireto'] = self.dsessao['pdf'].get('recusadosDireto', 0) + 1
            return False
        info.pop('_achou', None); info.pop('_confere', None)
        with self.trava:
            base = self.por.get(ped, {}).get(reds) or it
            if not base.get('codBusca'): base['codBusca'] = [x['cod'] for x in base.get('secundarias') or [] if x.get('fonte') == 'pesquisa' and x.get('cod')]
            for k, v in info.items():
                if v in (None, '', [], {}) and k != 'vitimas': continue
                if k in ('relator', 'digitador') and (base.get(k) or {}).get('nome') and not (v or {}).get('nome'): continue
                base[k] = v
            if base.get('naArea') is not True or base.get('areaPor'):        # a unidade de área do PDF confirma (ou não)
                na = area_do_pdf(base.get('unidadeArea'), self.unome)
                if na is not None: base['naArea'] = na; base.pop('areaPor', None)
            regrupar(base, True); avaliar_indicadores(base); secundarias(base); base['pdf'] = 'ok'
            carga = {k: base[k] for k in DO_PDF + DO_GRUPO if k in base}
        self.app.pdf(reds, dados, carga, pedido=ped, ja=True)
        st = self.dsessao['pdf']; st['ok'] += 1; st[via] = st.get(via, 0) + 1
        if via == 'clique' and dg and not self.atalho.molde and self.atalho.aprender(reds, dg, self.atributos):
            st['atalho'] = self.atalho.molde['tipo'] + ' ' + self.atalho.molde['metodo'] + ' ' + caminho(dg.get('url', ''))
        return True

    def pdfs(self, t):
        """Pega o PDF de cada registro pedido: direto pelo endereço aprendido; senão, acha o registro (pesquisa do dia da
        criação, na área e depois em "quem registrou") e clica em "Ver"."""
        ped, c = t['pedido'], self.c
        st = self.dsessao['pdf']
        itens = [x for x in t.get('itens') or [] if RE_REDS.fullmatch(x.get('reds') or '')]
        st['pedidos'] += len(itens)
        espera = min(int(c['pdf'].get('espera_s', 30)), 30)
        pend, erros = {}, {}
        for it in itens:
            if it.get('orgao') == 'PC' or it.get('grupo') == 'PC':
                st['pc'] += 1
                self.app.mandar('pdfErro', pedido=ped, reds=it['reds'], pc=True, msg='Registro da Polícia Civil: o REDS não libera o PDF.'); continue
            dados = None
            if self.atalho.molde:
                try: dados = self.atalho.baixar_ctx(self.page.context, it['reds'], self.atributos)
                except Exception: dados = None
            if dados and self.processar_pdf(ped, it, dados, 'direto'): continue
            pend[it['reds']] = it
        if not pend: return
        por_dia = collections.OrderedDict()
        for r_, it in pend.items():
            d = data_hora(it.get('criacao') or it.get('fato'))
            por_dia.setdefault(d.strftime('%Y-%m-%d') if d else '', {})[r_] = it
        for dia, grupo in por_dia.items():
            if not dia:
                for r_ in grupo: erros[r_] = 'sem data do registro'
                continue
            jan = Janela(dia + 'T00:00', dia + 'T23:59', 'dataInclusao')
            faltam = set(grupo)
            for campo in (c['campo_area'], c['campo_resp']):
                if not faltam: break

                def ao_ler(pg, novos):
                    for x in novos:
                        if x['reds'] not in faltam: continue
                        ok_ = False
                        for tentativa in (1, 2):                    # o PDF de outro registro (lista ainda trocando): clica de novo
                            try: dados, dg = capturar_pdf(pg, x['reds'], espera)
                            except Exception as e: dados, dg = None, {'erro': 'captura: ' + curto(e)}
                            voltar_lista(self.robo, pg)
                            if dados and self.processar_pdf(ped, grupo[x['reds']], dados, 'clique', dg): ok_ = True; break
                            if not dados: break
                            dg['erro'] = 'o REDS mostrou o PDF de outro registro'; pg.wait_for_timeout(1500)
                        if ok_: faltam.discard(x['reds']); erros.pop(x['reds'], None)
                        else:
                            erros[x['reds']] = dg.get('erro') or 'o REDS não entregou o PDF'; st['falhas'] += 1; faltam.discard(x['reds'])
                            if len(st['exemplos']) < 3: st['exemplos'].append({k: v for k, v in dg.items() if k not in ('url', 'post')})
                    if not faltam: raise Achou()
                try:
                    pg, _ = buscar(self.robo, self.page, c, jan, [(self.uni, [campo])], nome='pdf', abrir=self.formulario)
                    self.page = pg
                    percorrer(self.robo, pg, c, ao_ler=ao_ler, atributos=self.atributos, info={})
                except Achou: pass
            for r_ in faltam: erros.setdefault(r_, 'não achei o registro na pesquisa do REDS')
        for r_, msg in erros.items():
            self.app.mandar('pdfErro', pedido=ped, reds=r_, msg=msg[:200])


def main():
    RR.carregar_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--pedido', default=os.environ.get('PEDIDO', ''))
    ap.add_argument('--ver', action='store_true'); ap.add_argument('--calibrar', action='store_true')
    a = ap.parse_args()
    c = cfg()
    inicio = time.time()
    app = App(os.environ.get('CONSULTA_URL'), os.environ.get('CONSULTA_CHAVE'), a.pedido)
    app.lote_n, app.lote_max = int(c['pdf'].get('lote_n', 8)), int(c['pdf'].get('lote_bytes', 4000000))
    try:
        p = app.chamar('pegar')
        try:
            cred = decifrar(p['cripto'], os.environ.get('CONSULTA_PRIVADA', ''), p.get('n'), p.get('m'))
        except Exception as e:
            app.chamar('fim', ok=False, msg='Não consegui abrir os dados cifrados do pedido (%s). Confira a chave privada no GitHub (CONSULTA65_PRIVADA) e gere de novo o par de chaves no app.' % str(e)[:120])
            log('ERRO: dados cifrados: ' + str(e)[:160]); sys.exit(1)
        mascarar(cred.get('u')); mascarar(cred.get('s'))
        if cred.get('teste'):
            app.chamar('fim', ok=True, msg='Teste OK: o robô recebeu o pedido e abriu os dados cifrados (versão %s).' % VERSAO)
            log('Teste de configuração OK'); return
        if (p.get('trabalhos') or [{}])[0].get('tipo', 'consulta') == 'consulta': app.progresso('ROBO', 'Robô pronto; abrindo o navegador', 14)
        r = Sessao(a, c, app, p, cred, inicio).rodar()
        cred.clear()
        if r == 'erro': sys.exit(1)
    except Parar as e:
        log('Parado pelo app: %s' % e)
    finally:
        app.encerrar()


if __name__ == '__main__':
    main()
