"""
Robô do REDS — Painel do 3º Pel
Confere no REDS as operações previstas no painel (e os BOS das visitas pendentes) e devolve só o mínimo: nº do REDS,
natureza, data/hora do fato e do registro, município, GP e o Nº PM de quem registrou. Não abre nenhum REDS e não lê dados
de envolvidos; o endereço do fato é usado só para saber o município e é descartado. Quem registrou um RAT ou BOS pode não
estar no campo "Relator da Ocorrência": o robô testa os outros campos de policial da lista do REDS (nunca os de envolvidos).

Uso:
  python robo_reds.py                 # rotina: pega no painel as operações pendentes, pesquisa no REDS e envia o resultado
  python robo_reds.py --dias 5        # olha os últimos 5 dias
  python robo_reds.py --dia 2026-11-20 --pedido ID   # pesquisa do dia pedida pelo botão do painel (todos os REDS do pelotão)
  python robo_reds.py --calibrar      # fotos de cada passo + diagnóstico em saida/ (não envia nada ao painel)
  python robo_reds.py --consulta Y07.002 --de 20/11/2026 --ate 21/11/2026 --unidade M0871   # consulta avulsa (gera CSV)
  python robo_reds.py --ver           # mostra o navegador na tela
Variáveis (arquivo .env ou GitHub Secrets): REDS_USUARIO, REDS_SENHA, PAINEL_URL, PAINEL_CHAVE.
No GitHub, a pesquisa do dia também chega por ROBO_MODO=dia, ROBO_DATA e ROBO_PEDIDO.
"""
import argparse, csv, datetime as dt, json, os, re, sys, time, unicodedata
import requests
from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
SAIDA = os.path.join(BASE, 'saida')
RE_REDS = re.compile(r'\d{4}-\d{9}-\d{3}')
RE_NAT = re.compile(r'\b([A-Z])\s?(\d{2})\.?(\d{3})\b')
RE_DATA = re.compile(r'\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?')
RE_MATRICULA = re.compile(r'(?<!\d)(\d{7})(?!\d)')


def chave(s):
    s = unicodedata.normalize('NFD', str(s or '')).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Z0-9]', '', s.upper())


def matriculas_no_texto(v):
    """'1516343 - NOME', '151.634-3' ou 'PM1516343' -> ['1516343']"""
    return RE_MATRICULA.findall(re.sub(r'(?<=\d)[.\-](?=\d)', '', str(v or '')))


# Campos da lista do REDS (#camposDisponiveis) que podem guardar o Nº PM de quem registrou um RAT ou BOS.
# "Relator da Ocorrência" (matriculaRelator) só achou os BO do tipo POLICIAL em 22/11/2026: os RAT/BOS ficaram sem relator.
# O robô lê a lista do formulário, testa os campos de policial (nunca os de envolvido, vítima, autor etc.) e conta ao painel
# qual campo trouxe o relator; da próxima vez o painel manda esse campo primeiro.
CAMPO_PESO = [('RELATOR', 100), ('DIGITADOR', 95), ('REGISTRADOR', 92), ('CADASTRADOR', 90), ('REDATOR', 90), ('ELABORADOR', 88),
              ('CRIADOR', 88), ('USUARIO', 84), ('RESPONSAV', 80), ('RESP', 78), ('COMANDANTE', 60), ('CMT', 58), ('MATRICULA', 50),
              ('MASP', 48), ('POLICIA', 40), ('MILITAR', 40), ('PM', 40), ('AGENTE', 35), ('GUARNICAO', 30), ('COMPONENT', 30),
              ('INTEGRANT', 30), ('EQUIPE', 28), ('OPERADOR', 25), ('ATENDENTE', 25)]
CAMPO_ACAO = ['REGISTR', 'CADASTR', 'PREENCH', 'ELABOR', 'REDIG', 'CRIA', 'LAVR', 'CONFECC']   # "responsável pelo registro": +8
CAMPO_FORA = ['ENVOLV', 'VITIM', 'AUTOR', 'TESTEMUNH', 'SUSPEIT', 'CONDUTOR', 'CONDUZID', 'PROPRIET', 'PRESO', 'DETID', 'ABORDAD',
              'INFRATOR', 'ADOLESCENT', 'MENOR', 'COMUNICANT', 'SOLICITANT', 'DECLARANT', 'NOME', 'CPF', 'RG', 'IDENTIDADE', 'DOCUMENTO',
              'PLACA', 'VEICUL', 'CHASSI', 'RENAVAM', 'ARMA', 'ENDERECO', 'LOGRADOURO', 'BAIRRO', 'MUNICIPIO', 'CEP', 'TELEFONE',
              'UNIDADE', 'UNID', 'ORGAO', 'NATUREZA', 'HISTORICO', 'PROTOCOLO', 'DATA', 'HORA', 'MAE', 'PAI', 'NASCIMENTO', 'OBJETO',
              'DROGA', 'REDS', 'VIATURA', 'PREFIXO']


def palavras_do_campo(valor, texto):
    sem = unicodedata.normalize('NFD', '%s %s' % (re.sub(r'([a-z])([A-Z])', r'\1 \2', valor or ''), texto or '')).encode('ascii', 'ignore').decode()
    return [w for w in re.split(r'[^A-Z0-9]+', sem.upper()) if w]


def campos_candidatos(opcoes, ja=(), preferidos=(), maximo=8):
    """[(valor, texto)] da lista do REDS -> campos de policial a testar, do mais provável ao menos provável."""
    nota = {}
    for valor, texto in opcoes:
        if not valor or valor in ja: continue
        ws = palavras_do_campo(valor, texto)
        if any(w.startswith(f) for w in ws for f in CAMPO_FORA): continue
        n = max([p for w in ws for f, p in CAMPO_PESO if w.startswith(f)] or [0])
        if not n: continue                                                   # campo que não é de pessoa (ex.: "Tipo de registro")
        if any(w.startswith(f) for w in ws for f in CAMPO_ACAO): n += 8
        if any(w.startswith('MATRICULA') for w in ws): n += 5
        nota[valor] = (n, texto)
    pref = [v for v in preferidos if v in nota]
    resto = sorted((v for v in nota if v not in pref), key=lambda v: -nota[v][0])
    return [(v, nota[v][1]) for v in pref + resto][:maximo]


def trocar_campo(passos, campo):
    """Os passos da consulta do relator, com outro campo escolhido na lista (vale para config antiga e nova)."""
    out, trocou = json.loads(json.dumps(passos)), False
    for p in out:
        if p.get('acao') == 'selecionar' and 'camposDisponiveis' in json.dumps(p.get('alvo', {})) and p.get('valor') in ('matriculaRelator', '{campo}'):
            p['valor'] = campo; trocou = True
    return out, trocou


def carregar_env():
    p = os.path.join(BASE, '.env')
    if os.path.exists(p):
        for linha in open(p, encoding='utf-8-sig'):
            if '=' in linha and not linha.strip().startswith('#'):
                k, v = linha.strip().split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cfg():
    with open(os.path.join(BASE, os.environ.get('REDS_CONFIG', 'reds_config.json')), encoding='utf-8') as f:
        c = json.load(f)
    pend = [k for k in json.dumps(c, ensure_ascii=False).split('"') if k.startswith('AJUSTAR')]
    return c, pend


def achar(page, alvo):
    exato = alvo.get('exato', False)
    if 'testid' in alvo: return page.get_by_test_id(alvo['testid']).first
    if 'label' in alvo: return page.get_by_label(alvo['label'], exact=exato).first
    if 'role' in alvo: return page.get_by_role(alvo['role'], name=alvo.get('name'), exact=exato).first
    if 'placeholder' in alvo: return page.get_by_placeholder(alvo['placeholder'], exact=exato).first
    if 'text' in alvo: return page.get_by_text(alvo['text'], exact=exato).first
    if 'css' in alvo: return page.locator(alvo['css']).first
    raise ValueError('alvo sem testid/label/role/placeholder/text/css: %s' % alvo)


# Resultado da pesquisa (visto no REDS em 22/11/2026): cada REDS vem numa tabela própria, dentro de tabelas de moldura,
# e a linha do campo que casou com a regra ("Relator da Ocorrência", "Código da unidade responsável...") fica na
# tabela-mãe. Lê só as linhas e células de cada tabela (t.rows / r.cells), para não misturar tabelas de dentro e de fora.
# Se o layout for uma tabela só com vários REDS, as linhas sem nº logo abaixo de um REDS contam como linhas extras dele.
JS_REGISTROS = r"""() => {
  const rx = /^\s*\d{4}-\d{9}-\d{3}\s*$/, tx = c => (c.innerText || '').replace(/\s+/g, ' ').trim();
  let cabPagina = [];
  for (const t of document.querySelectorAll('table')) { for (const r of t.rows) { const cs = Array.from(r.cells); if (cs.some(c => c.tagName === 'TH' && /REDS/i.test(c.innerText))) { cabPagina = cs.map(tx); break; } } if (cabPagina.length) break; }
  const out = [];
  for (const t of document.querySelectorAll('table')) {
    let cab = null, atual = null; const deste = [];
    for (const r of t.rows) {
      const cs = Array.from(r.cells); if (!cs.length) continue;
      if (cs.some(c => c.tagName === 'TH')) { if (!cab) cab = cs.map(tx); continue; }
      if (cs.some(c => rx.test(c.innerText))) { atual = { cab: null, cells: cs.map(tx), extras: [] }; deste.push(atual); continue; }
      if (atual) { const v = cs.map(tx); if (v.some(Boolean)) atual.extras.push(v); }
    }
    if (!deste.length) continue;
    if (deste.length === 1) {
      const mae = t.parentElement && t.parentElement.closest('table');
      if (mae) for (const r of mae.rows) { if (r.contains(t)) continue; const v = Array.from(r.cells).map(tx); if (v.some(Boolean)) deste[0].extras.push(v); }
    }
    deste.forEach(d => { d.cab = cab || cabPagina; out.push(d); });
  }
  return out;
}"""

JS_DEFINIR = """(e, v) => { e.removeAttribute('readonly'); e.removeAttribute('disabled'); e.value = v;
  ['input', 'change', 'blur'].forEach(t => e.dispatchEvent(new Event(t, { bubbles: true }))); }"""


class Robo:
    def __init__(self, c, calibrar=False):
        self.c, self.calibrar, self.n = c, calibrar, 0
        os.makedirs(SAIDA, exist_ok=True)

    def foto(self, page, nome):
        if self.calibrar:
            self.n += 1
            try: page.screenshot(path=os.path.join(SAIDA, '%02d-%s.png' % (self.n, nome)), full_page=True)
            except Exception: pass

    def executar(self, page, passos, valores, nome):
        """Executa os passos; devolve a página atual (muda se a consulta abrir outra aba)."""
        for i, p in enumerate(passos):
            if p.get('se') and not valores.get(p['se']): continue   # passo só vale quando o valor existe (ex.: natureza)
            v = p.get('valor', '')
            for k, val in valores.items():
                v = v.replace('{' + k + '}', str(val))
            a, ms = p['acao'], int(p.get('ms', 20000))
            try:
                if a == 'ir':
                    if v and not v.startswith('AJUSTAR'): page.goto(v)
                elif a == 'preencher': achar(page, p['alvo']).fill(v, timeout=ms)
                elif a == 'digitar': achar(page, p['alvo']).press_sequentially(v, delay=40, timeout=ms)
                elif a == 'definir': achar(page, p['alvo']).evaluate(JS_DEFINIR, v)
                elif a == 'selecionar':
                    el = achar(page, p['alvo'])
                    try: el.select_option(value=v, timeout=ms)
                    except Exception: el.select_option(label=v, timeout=ms)
                elif a == 'clicar':
                    el = achar(page, p['alvo'])
                    if p.get('nova_aba'):
                        try:
                            with page.context.expect_page(timeout=int(p.get('ms_aba', 8000))) as nova:
                                el.click(timeout=ms)
                            page = nova.value; page.wait_for_load_state()
                        except Exception:
                            pass  # não abriu outra aba: o resultado está na mesma página
                    else:
                        el.click(timeout=ms)
                elif a == 'marcar': achar(page, p['alvo']).check(timeout=ms)
                elif a == 'tecla': page.keyboard.press(v or 'Enter')
                elif a == 'esperar': achar(page, p['alvo']).wait_for(timeout=ms)
                elif a == 'sumir': achar(page, p['alvo']).wait_for(state='hidden', timeout=ms)
                elif a == 'esperar_rede': page.wait_for_load_state('networkidle', timeout=ms)
                elif a == 'pausa': page.wait_for_timeout(ms)
                else: raise ValueError('ação desconhecida: ' + a)
            except Exception as e:
                if p.get('opcional'): continue
                self.foto(page, '%s-%d-ERRO' % (nome, i + 1))
                raise RuntimeError('Passo %d de "%s" (%s %s) falhou: %s' % (i + 1, nome, a, json.dumps(p.get('alvo', v), ensure_ascii=False)[:120], str(e).splitlines()[0]))
            self.foto(page, '%s-%d-%s' % (nome, i + 1, a))
        return page

    def registros_da_pagina(self, page):
        regs = []
        for fr in page.frames:
            try: regs += fr.evaluate(JS_REGISTROS) or []
            except Exception: pass
        return regs

    def interpretar(self, reg):
        """Um REDS da lista -> só os campos mínimos (nada de endereço, histórico ou envolvidos)."""
        r = self.c['resultado']
        cab, cel = reg.get('cab') or [], reg.get('cells') or []
        mapa = {}
        for campo, titulos in r.get('titulos', {}).items():
            for i, h in enumerate(cab):
                if any(chave(x) and chave(x) in chave(h) for x in titulos):
                    mapa[campo] = i; break
        g = lambda k: cel[mapa[k]] if k in mapa and mapa[k] < len(cel) else ''
        m = RE_REDS.search(g('reds') or ' '.join(cel))
        if not m: return None
        nat = RE_NAT.search(g('natureza'))
        fato, criacao = RE_DATA.search(g('data') or ''), RE_DATA.search(g('criacao') or '')
        extras = {}
        for x in reg.get('extras') or []:
            if x: extras[chave(x[0])] = ' '.join(x[1:]) if len(x) > 1 else ''
        mun, gp = '', ''
        end = chave(g('municipio'))
        for trecho, info in self.c.get('municipios', {}).items():
            if chave(trecho) and chave(trecho) in end: mun, gp = info.get('nome', trecho), info.get('gp', ''); break
        for k, v in extras.items():
            if 'CODIGODAUNIDADE' in k:   # "M0001 M0575 ... M0870 M0871": a última unidade é a que registrou
                for cod in reversed(re.findall(r'\bM\d{3,5}\b', v)):
                    if cod in self.c.get('unidades_gp', {}): gp = self.c['unidades_gp'][cod]; break
        relator = ''
        for k, v in extras.items():
            if 'RELATOR' in k:
                ms = matriculas_no_texto(v)
                if ms: relator = ms[0]
        return {'reds': m.group(0), 'natureza': ('%s%s.%s' % nat.groups()) if nat else '', 'naturezaDesc': g('natureza'),
                'tipo': g('tipo'), 'dataHora': fato.group(0) if fato else '', 'criacao': criacao.group(0) if criacao else '',
                'municipio': mun, 'gp': gp, 'relator': relator, 'origem': g('origem'), 'guarnicao': g('guarnicao'),
                '_historico': g('historico').upper(),
                '_extras': [(chave(x[0]), x[0], ' '.join(x[1:])) for x in reg.get('extras') or [] if x]}

    def matriculas_do_campo(self, it, rotulo, todos):
        """Nº PM do pelotão na linha do campo consultado ("Relator da Ocorrência", "Responsável pelo registro"...).
        Só matrículas que estão no efetivo; a primeira é quem registrou, as outras vão como equipe."""
        linhas, k = it.get('_extras') or [], chave(rotulo)
        alvo = [l for l in linhas if k and l[0] and (k in l[0] or l[0] in k)]
        if not alvo: alvo = [l for l in linhas if 'CODIGODAUNIDADE' not in l[0]]   # rótulo diferente do da lista: qualquer linha que não seja a da unidade
        achadas = []
        for l in alvo:
            for mt in matriculas_no_texto(l[2]):
                if mt in todos and mt not in achadas: achadas.append(mt)
        return achadas

    def ler_resultado(self, page):
        """Lê todos os REDS de todas as páginas do resultado."""
        r, out, vistos = self.c['resultado'], [], set()
        pagina, anteriores = 1, None
        while True:
            regs = self.registros_da_pagina(page)
            if self.calibrar and pagina == 1:
                cab = regs[0].get('cab') if regs else []
                diag(['Colunas da lista de REDS: ' + ' | '.join(cab or ['(sem cabeçalho)']),
                      'REDS nesta página: %d' % len(regs),
                      'Linhas extras (só os rótulos): ' + ', '.join(sorted({(x[0] if x else '') for g in regs for x in g.get('extras') or []}))])
            atuais = []
            for reg in regs:
                it = self.interpretar(reg)
                if not it: continue
                atuais.append(it['reds'])
                if it['reds'] in vistos: continue
                vistos.add(it['reds']); out.append(it)
            if atuais == anteriores: break     # a página não mudou: para, em vez de repetir
            anteriores = atuais
            pagina += 1
            if not r.get('paginar') or pagina > 30: break
            b = page.get_by_role('link', name=str(pagina), exact=True)
            if not b.count(): break
            b.first.click(); page.wait_for_load_state('networkidle')
        return out


def diag(linhas):
    os.makedirs(SAIDA, exist_ok=True)
    with open(os.path.join(SAIDA, 'diagnostico.txt'), 'a', encoding='utf-8') as f:
        for l in linhas:
            print('  [diagnóstico] ' + l); f.write(l + '\n')


def diag_formulario(page):
    """Lista os campos de escolha do formulário (nomes técnicos, sem dados de pessoas) para ajustar a configuração."""
    try:
        for s in page.locator('select').all():
            nome = s.get_attribute('name') or s.get_attribute('id') or '?'
            ops = s.evaluate("e => Array.from(e.options).map(o => o.value + '=' + o.text.trim()).slice(0, 80)")
            diag(['Lista "%s": %s' % (nome, '; '.join(ops))])
    except Exception as e:
        diag(['(não deu para listar os campos: %s)' % e])


ERROS_LOGIN = ['Authentication Error', 'Something went wrong during the authentication', 'Usuário ou senha inválid', 'Senha inválida']


def conferir_login(page, espera_s=20):
    """Espera o resultado do login. Se o SISP recusou, para com mensagem clara e não tenta de novo, para não bloquear o usuário."""
    def erro_na_tela():
        for t in ERROS_LOGIN:
            try:
                if page.get_by_text(t).first.is_visible(): return t
            except Exception: pass
        return ''
    limite = time.time() + espera_s
    while True:
        t = erro_na_tela()
        if t: raise RuntimeError('o SISP recusou o login ("%s"). O robô parou sem tentar de novo, para não bloquear o seu usuário.' % t)
        try: senha = page.get_by_placeholder('Senha').first.is_visible()
        except Exception: senha = False
        if not senha:
            page.wait_for_timeout(1500)   # a mensagem de erro pode aparecer logo depois que o formulário some
            t = erro_na_tela()
            if t: raise RuntimeError('o SISP recusou o login ("%s"). O robô parou sem tentar de novo, para não bloquear o seu usuário.' % t)
            return
        if time.time() > limite:
            raise RuntimeError('o login não saiu da tela de usuário e senha. Confira REDS_USUARIO e REDS_SENHA.')
        page.wait_for_timeout(500)


def data_iso(txt):
    m = re.search(r'(\d{2})/(\d{2})/(\d{4})', txt or '')
    return '%s-%s-%s' % (m.group(3), m.group(2), m.group(1)) if m else ''


def painel(acao, params):
    url, chave_ = (os.environ.get('PAINEL_URL') or '').strip(), (os.environ.get('PAINEL_CHAVE') or '').strip()
    if not url or not chave_: raise SystemExit('Defina PAINEL_URL e PAINEL_CHAVE no .env.')
    params = dict(params, chave=chave_)
    r = requests.post(url, data=json.dumps({'acao': acao, 'params': params}), headers={'Content-Type': 'text/plain;charset=utf-8'}, timeout=120)
    r.raise_for_status(); j = r.json()
    if not j.get('ok'): raise SystemExit('Painel recusou: ' + str(j.get('erro')))
    return j['dados']


def abrir(pw, a):
    """Navegador + página; sem janela (nuvem), apresenta-se como um Chrome comum, sem a marca "HeadlessChrome"."""
    extra = {'executable_path': os.environ['CHROME_PATH']} if os.environ.get('CHROME_PATH') else {}
    nav = pw.chromium.launch(headless=not a.ver, args=['--disable-blink-features=AutomationControlled'], **extra)
    contexto = dict(locale='pt-BR', timezone_id='America/Sao_Paulo', accept_downloads=True)
    if not a.ver:
        try:
            p0 = nav.new_page(); contexto['user_agent'] = p0.evaluate('navigator.userAgent').replace('HeadlessChrome', 'Chrome'); p0.close()
        except Exception:
            pass
    return nav, nav.new_context(**contexto).new_page()


def entrar(robo, page, c):
    page.goto(c['url']); robo.foto(page, 'inicio')
    page = robo.executar(page, c['login'], {'usuario': (os.environ.get('REDS_USUARIO') or '').strip(), 'senha': os.environ.get('REDS_SENHA', '')}, 'login')
    conferir_login(page)
    return page


def salvar_csv(itens, campos, prefixo, calibrar):
    arq = os.path.join(SAIDA, '%s_%s.csv' % (prefixo, dt.datetime.now().strftime('%Y%m%d_%H%M')))
    with open(arq, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=';', extrasaction='ignore'); w.writeheader(); w.writerows(itens)
    print('%d REDS — lista salva em %s%s' % (len(itens), arq, ' (calibração: nada foi enviado ao painel)' if calibrar else ''))


def grupos_de_matriculas(page, c, mats):
    """O campo de pesquisa do REDS corta o texto no limite dele: divide as matrículas em consultas que cabem inteiras.
    Aproveita a página aberta para ler a lista de campos da regra (só nomes técnicos do formulário, sem dados de pessoas)."""
    limite, opcoes = 0, []
    try:
        page.goto(c['pesquisa_relator'][0]['valor'], wait_until='domcontentloaded', timeout=60000)
        page.wait_for_selector('input[name="consultaEspecifica"]', state='attached', timeout=40000)
        limite = int(page.eval_on_selector('input[name="consultaEspecifica"]', 'e => e.maxLength') or 0)
    except Exception as e:
        print('  (não consegui ler o limite do campo: %s)' % e)
    try:
        opcoes = [tuple(x) for x in page.eval_on_selector_all('#camposDisponiveis option', "os => os.map(o => [o.value, (o.text || '').replace(/\\s+/g, ' ').trim()])")]
    except Exception as e:
        print('  (não consegui ler a lista de campos: %s)' % e)
    por = int(c.get('matriculas_por_consulta', 12))                      # 12 × 8 caracteres = 95: cabe em quase todo campo
    if 0 < limite < 100000: por = max(1, min(por, (limite + 1) // 8))
    return [mats[i:i + por] for i in range(0, len(mats), por)], limite, opcoes


def buscar_relatores(robo, page, c, d, itens, mats, dados, info, avisa):
    """Quem registrou cada REDS do dia: consulta o campo "Relator da Ocorrência" com as matrículas do pelotão (em partes que
    cabem no campo) e, para os REDS que ficarem sem relator (RAT e BOS), os outros campos de policial da lista do REDS.
    Para assim que todos tiverem relator, ou no limite de tempo."""
    grupos, limite, opcoes = grupos_de_matriculas(page, c, mats)
    info['campos'] = [list(x) for x in opcoes]
    print('  campo de pesquisa do REDS: limite de %s caracteres; %d matrícula(s) em %d consulta(s) de até %d; %d campo(s) na lista'
          % (limite or 'sem', len(mats), len(grupos), len(grupos[0]), len(opcoes)))
    todos, rel = set(mats), {}
    faltam = {it['reds'] for it in itens if not it.get('relator')}
    campos = [('matriculaRelator', dict(opcoes).get('matriculaRelator') or 'Relator da Ocorrência')]
    if c.get('relator_outros_campos', True):
        aprendidos = [x for x in (dados.get('camposRelator') or []) if x]          # já trouxeram quem registrou: vêm primeiro e encurtam a busca
        maximo = int(c.get('campos_relator_max') or (len(aprendidos) + 3 if aprendidos else 8))
        campos += campos_candidatos(opcoes, ja=('matriculaRelator',), preferidos=aprendidos, maximo=maximo)
    print('  campos para quem registrou: ' + '; '.join('%s (%s)' % x for x in campos))
    fim, feitas, seguidas = time.time() + int(c.get('tempo_relator_s', 600)), 0, 0
    total = len(campos) * len(grupos)
    for campo, rotulo in campos:
        if not faltam or time.time() > fim: break
        passos, trocou = trocar_campo(c['pesquisa_relator'], campo)
        if campo != 'matriculaRelator' and not trocou: break                    # config sem o passo da lista de campos
        info['camposTestados'].append(campo)
        try:
            for k, g in enumerate(grupos, 1):
                if not faltam or time.time() > fim: break
                feitas += 1
                qual = '' if campo == 'matriculaRelator' else ' — campo "%s"' % rotulo
                avisa('CONSULTA', 'Buscando quem registrou (%d de %d)%s' % (k, len(grupos), qual), 75 + min(14, 14 * feitas / max(1, total)))
                page = robo.executar(page, passos, {'data_ini': d, 'data_fim': d, 'matriculas': ' '.join(g), 'campo': campo}, 'relator')
                for x in robo.ler_resultado(page):
                    if x['reds'] not in faltam: continue
                    ms = robo.matriculas_do_campo(x, rotulo, todos) or ([x['relator']] if x.get('relator') in todos else [])
                    if not ms: continue
                    rel[x['reds']] = (ms, '' if campo == 'matriculaRelator' else rotulo)
                    faltam.discard(x['reds']); info['camposUsados'][campo] = info['camposUsados'].get(campo, 0) + 1
            seguidas = 0
        except Exception as e:
            info['camposErro'][campo] = str(e).splitlines()[0][:200]
            print('  campo %s (%s) não funcionou: %s' % (campo, rotulo, info['camposErro'][campo]))
            seguidas += 1
            if seguidas >= 2: break                                             # dois campos seguidos com erro: o REDS caiu
    for it in itens:
        r = rel.get(it['reds'])
        if r and not it.get('relator'):
            it['relator'] = r[0][0]
            if r[1]: it['relatorCampo'] = r[1]
            if len(r[0]) > 1: it['relatorEquipe'] = r[0][1:6]
    if faltam: print('  sem quem registrou: %d REDS (%s)' % (len(faltam), ', '.join(sorted(faltam))))
    return page


def pesquisa_do_dia(a, c):
    """Botão "Pesquisar operações realizadas no dia": todos os REDS do pelotão no dia, com quem registrou e quando."""
    pedido = (a.pedido or '').strip()

    def avisa(etapa, msg='', pct=None):
        print('[%s] %s' % (etapa, msg))
        if not pedido: return
        p = {'pedido': pedido, 'etapa': etapa, 'msg': msg[:300]}
        if pct is not None: p['pct'] = int(pct)
        try: painel('robo.dia.progresso', p)
        except BaseException as e: print('  (aviso ao painel falhou: %s)' % e)

    try: dia = dt.date.fromisoformat(a.dia.strip())
    except ValueError:
        avisa('ERRO', 'Data inválida: ' + a.dia); sys.exit(2)
    d = dia.strftime(c.get('formato_data', '%d/%m/%Y'))
    avisa('ROBO', 'Robô pronto; entrando no REDS')
    try: dados = painel('robo.dia.dados', {'pedido': pedido}) if pedido else {}
    except SystemExit as e:
        avisa('ERRO', str(e)); raise
    mats = [str(m) for m in (dados.get('matriculas') or []) if re.fullmatch(r'\d{7}', str(m))]   # quem estava de serviço vem primeiro
    itens, info = [], {'campos': [], 'camposTestados': [], 'camposUsados': {}, 'camposErro': {}}
    with sync_playwright() as pw:
        nav, page = abrir(pw, a)
        robo = Robo(c, a.calibrar)
        try:
            page = entrar(robo, page, c)
            avisa('LOGIN', 'Entrou no REDS; buscando os REDS de ' + d)
            page = robo.executar(page, c['pesquisa_dia'], {'data_ini': d, 'data_fim': d, 'unidade': c.get('unidade_pelotao', 'M0870')}, 'dia')
            itens = robo.ler_resultado(page)
            avisa('CONSULTA', '%d REDS no dia; buscando quem registrou' % len(itens))
            if itens and mats and c.get('pesquisa_relator'):
                page = buscar_relatores(robo, page, c, d, itens, mats, dados, info, avisa)
            achados = sum(1 for it in itens if it.get('relator'))
            outros = sorted({it['relatorCampo'] for it in itens if it.get('relatorCampo')})
            avisa('RELATOR', 'Quem registrou: %d de %d REDS%s; enviando ao painel' % (achados, len(itens), (' (também pelo campo ' + ', '.join('"%s"' % x for x in outros) + ')') if outros else ''))
        except Exception as e:
            if a.calibrar:
                diag_formulario(page); robo.foto(page, 'erro')
            avisa('ERRO', str(e)); nav.close(); sys.exit(1)
        nav.close()
    itens = [{k: v for k, v in it.items() if not k.startswith('_')} for it in itens]
    if a.calibrar or not pedido:
        if info['campos']: diag(['Campos da regra no REDS: ' + '; '.join('%s=%s' % x for x in info['campos']),
                                 'Campos testados para quem registrou: ' + ', '.join(info['camposTestados']),
                                 'Campos que trouxeram quem registrou: ' + json.dumps(info['camposUsados'], ensure_ascii=False)])
        salvar_csv(itens, ['reds', 'tipo', 'naturezaDesc', 'dataHora', 'criacao', 'municipio', 'gp', 'relator', 'relatorCampo', 'origem'], 'dia', a.calibrar)
        return
    try: r = painel('robo.dia.resultado', dict(info, pedido=pedido, data=dia.isoformat(), itens=itens))
    except BaseException as e:
        avisa('ERRO', 'Não consegui entregar o resultado ao painel: ' + str(e)); raise
    print('Painel: %s REDS recebidos; %s operação(ões) e %s visita(s) marcada(s) como realizada(s).' % (r.get('recebidos'), r.get('marcadas'), r.get('visitas', 0)))


def main():
    carregar_env()
    ap = argparse.ArgumentParser()
    ap.add_argument('--dias', type=int, default=3); ap.add_argument('--calibrar', action='store_true'); ap.add_argument('--ver', action='store_true')
    ap.add_argument('--consulta'); ap.add_argument('--de'); ap.add_argument('--ate'); ap.add_argument('--unidade', default='')
    ap.add_argument('--dia', default=os.environ.get('ROBO_DATA', '') if os.environ.get('ROBO_MODO') == 'dia' else '')
    ap.add_argument('--pedido', default=os.environ.get('ROBO_PEDIDO', ''))
    a = ap.parse_args()
    c, pend = cfg()
    if pend:
        print('ATENÇÃO: o reds_config.json ainda tem %d item(ns) com AJUSTAR.' % len(pend)); sys.exit(2)
    if a.calibrar and os.path.exists(os.path.join(SAIDA, 'diagnostico.txt')): os.remove(os.path.join(SAIDA, 'diagnostico.txt'))
    if a.dia: return pesquisa_do_dia(a, c)
    fmt = c.get('formato_data', '%d/%m/%Y')
    unidades_cfg = c.get('unidades', {})
    hoje = dt.date.today()
    if a.consulta or a.calibrar:
        de = a.de or (hoje - dt.timedelta(days=1)).strftime(fmt); ate = a.ate or hoje.strftime(fmt)
        uni = a.unidade or c.get('unidade_pelotao') or unidades_cfg.get('1º GP', '')
        tarefas = [{'natureza': a.consulta or 'Y07.002', 'unidade': uni, 'gp': '', 'de': de, 'ate': ate}]
        termos_por_data = {}
    else:
        p = painel('robo.pendentes', {'dias': a.dias})
        extras = sorted({n for n in (p.get('naturezasExtras') or []) if n})       # BOS de visita (A20.xxx) com visita pendente na planilha
        print('Painel: %d operação(ões) pendente(s) de %s a %s%s' % (len(p['ops']), p['de'], p['ate'], ('; visitas pendentes: ' + ', '.join(extras)) if extras else ''))
        if not p['ops'] and not extras: return
        de = dt.date.fromisoformat(p['de']).strftime(fmt); ate = dt.date.fromisoformat(p['ate']).strftime(fmt)
        gps_com_op = sorted({o['gp'] for o in p['ops']} | ({u['gp'] for u in p['unidades']} if extras else set()))
        tarefas = []
        if c.get('por') == 'natureza':
            for nat in sorted({o['natureza'] for o in p['ops']} | set(extras)):
                tarefas.append({'natureza': nat, 'unidade': c.get('unidade_pelotao', 'M0870'), 'gp': '', 'de': de, 'ate': ate})
        for u in ([] if c.get('por') == 'natureza' else [u for u in p['unidades'] if u['gp'] in gps_com_op]):
            valor = unidades_cfg.get(u['gp']) or u['unidadeCad']
            nats = sorted({o['natureza'] for o in p['ops'] if o['gp'] == u['gp']} | set(extras)) if c.get('por') != 'unidade' else ['']
            previstas = sorted({o['natureza'].replace('.', '') for o in p['ops'] if o['gp'] == u['gp']} | {x.replace('.', '') for x in extras})
            for nat in nats: tarefas.append({'natureza': nat, 'unidade': valor, 'gp': u['gp'], 'de': de, 'ate': ate, 'previstas': previstas})
        termos_por_data = {}
        for o in p['ops']: termos_por_data.setdefault((o['data'], o['natureza'].replace('.', '')), []).append(o['descricao'])
    resultados = []
    with sync_playwright() as pw:
        nav, page = abrir(pw, a)
        robo = Robo(c, a.calibrar)
        try:
            page = entrar(robo, page, c)
            for t in tarefas:
                nat = t['natureza']
                valores = {'data_ini': t['de'], 'data_fim': t['ate'], 'natureza': nat, 'natureza_sem_ponto': nat.replace('.', ''), 'unidade': t['unidade']}
                page = robo.executar(page, c['pesquisa'], valores, 'pesquisa')
                if a.calibrar: diag_formulario(page)
                achados = robo.ler_resultado(page)
                print('  %s %s: %d REDS' % (t['unidade'] or '-', nat or '(todas)', len(achados)))
                for it in achados:
                    data = data_iso(it['dataHora'])
                    if nat and it['natureza'] and it['natureza'].replace('.', '') != nat.replace('.', ''): continue
                    if t.get('previstas') and (it['natureza'] or '').replace('.', '') not in t['previstas']: continue   # só REDS de natureza de operação prevista
                    termos = termos_por_data.get((data, (it['natureza'] or nat).replace('.', '')), [])
                    bate = [x for x in termos if x and x.upper() in it['_historico']]
                    resultados.append({'reds': it['reds'], 'natureza': it['natureza'] or nat, 'data': data, 'dataHora': it['dataHora'], 'municipio': it['municipio'], 'guarnicao': it['guarnicao'], 'gp': t['gp'] or it.get('gp', ''), 'descricao': bate[0] if bate else ''})
                if c.get('voltar'): page = robo.executar(page, c['voltar'], {}, 'voltar')
        except Exception as e:
            print('ERRO: ' + str(e))
            if a.calibrar:
                diag_formulario(page); robo.foto(page, 'erro')
                print('Veja as fotos na pasta saida e me mande o texto do diagnóstico (não tem dados de pessoas).')
            nav.close(); sys.exit(1)
        nav.close()
    vistos, unicos = set(), []
    for r in resultados:
        if r['reds'] in vistos: continue
        vistos.add(r['reds']); unicos.append(r)
    if a.consulta or a.calibrar:
        salvar_csv(unicos, ['reds', 'natureza', 'dataHora', 'municipio', 'gp', 'guarnicao'], 'consulta', a.calibrar)
        return
    r = painel('robo.reds', {'resultados': unicos})
    print('Painel: %s operação(ões) marcada(s) como realizada(s); %s visita(s) marcada(s); %s REDS sem operação correspondente.' % (r['marcadas'], r.get('visitas', 0), r['semOperacao']))


if __name__ == '__main__':
    main()
