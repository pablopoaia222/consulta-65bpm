"""
Monitor 65º BPM — robô de alertas de MV, CVPe e CVPa (roda no GitHub Actions, workflow monitor-65.yml)

O app (Apps Script) chama uma rodada a cada intervalo (60 min, por padrão). A rodada:
 1. pergunta ao app a configuração e o que já foi avisado (a planilha "Alertas" do app é a memória: o mesmo REDS não sai
    duas vezes; se um registro muda de indicador — CVPe que vira MV — sai um e-mail de atualização);
 2. entra no REDS com o login do dono (GitHub Secrets REDS_USUARIO e REDS_SENHA, os mesmos do robô do pelotão). Se o SISP
    recusar a senha, para na hora, avisa o app (que pausa o monitor e manda e-mail ao dono) e não tenta de novo;
 3. pesquisa, na área do 65º BPM (todos os órgãos), os registros criados de ontem para hoje: 1 pesquisa da Polícia Civil
    (cujo PDF o REDS não libera) e 1 pesquisa por natureza de indicador (8), que acha a natureza também como secundária;
 4. de cada registro novo (fora da PC) pega o PDF — clicando em "Ver" e lendo o arquivo na rede, ou direto pelo endereço
    aprendido — e confirma pelo PDF: natureza do envolvido, tentado/consumado, grau da lesão (MV x CVPe), órgão;
 5. manda cada alerta ao app, que envia o e-mail (dados principais + PDF anexo; PC: aviso de PDF indisponível).
O log do GitHub só mostra contagens: nenhum número de REDS nem dado de pessoa.

Variáveis: CONSULTA_URL, CONSULTA_CHAVE (as mesmas da consulta), REDS_USUARIO, REDS_SENHA, RODADA (vem do app).
Opcionais: CHROME_PATH, CONSULTA_CONFIG (teste).
"""
import base64, datetime as dt, json, os, sys, time
from playwright.sync_api import sync_playwright
import robo_reds as RR
import consulta65 as C

VERSAO = '1.0 (23/11/2026)'
log = C.log


class Args:
    ver = False
    calibrar = False


def entrar(robo, page, c, usuario, senha, tentativas=3):
    """Login; refaz só se a rede falhar (nunca depois de o SISP recusar a senha)."""
    for i in range(tentativas):
        try:
            page.goto(c['url'], wait_until='domcontentloaded', timeout=60000)
            page = robo.executar(page, c['login'], {'usuario': usuario, 'senha': senha}, 'login')
            RR.conferir_login(page)
            return page
        except RuntimeError as e:
            if 'recusou' in str(e) or i == tentativas - 1: raise
            log('  (login não concluído: %s; tentando de novo)' % C.curto(e)); time.sleep(20)
        except Exception as e:
            if i == tentativas - 1: raise RuntimeError('o REDS não abriu (%s)' % C.curto(e))
            log('  (o REDS não abriu: %s; tentando de novo)' % C.curto(e)); time.sleep(20)
    return page


def rodar(app, c, ini):
    agora = dt.datetime.now()
    dias = max(1, min(3, int(ini.get('dias') or 2)))
    jan = C.Janela((agora - dt.timedelta(days=dias - 1)).strftime('%Y-%m-%dT00:00'), (agora + dt.timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M'), 'dataInclusao')
    uni = ini.get('unidade') or 'M3853'
    enviados = ini.get('enviados') or {}                     # reds -> {'ind': [...], 'pdf': 'sim'|'nao'|'pc'}
    verificados = set(ini.get('verificados') or [])          # lesão corporal já vista no PDF (sem morte)
    diag = {'versao': VERSAO, 'janela': [jan.ini.strftime('%d/%m %H:%M'), jan.fim.strftime('%d/%m %H:%M')], 'buscas': {}, 'fases': {}, 'tempos': {},
            'pdf': {'clique': 0, 'direto': 0, 'falhas': 0, 'atalho': '', 'exemplos': []}}
    t0 = time.time()
    marca = lambda k: diag['tempos'].__setitem__(k, round(time.time() - t0, 1))
    usuario, senha = (os.environ.get('REDS_USUARIO') or '').strip(), os.environ.get('REDS_SENHA') or ''
    if not usuario or not senha:
        app.chamar('monitor.fim', ok=False, msg='Faltam os secrets REDS_USUARIO e REDS_SENHA no GitHub.')
        return 'erro'
    C.mascarar(usuario); C.mascarar(senha)
    espera = min(int(c['pdf'].get('espera_s', 30)), 30)
    with sync_playwright() as pw:
        nav, page = C.abrir_navegador(pw, Args, c)
        robo = RR.Robo(c, False)
        try:
            try: page = entrar(robo, page, c, usuario, senha)
            except RuntimeError as e:
                recusou = 'recusou' in str(e)
                app.chamar('monitor.erro', tipo='LOGIN', recusado=recusou, msg=('O REDS recusou o usuário ou a senha do robô.' if recusou else 'O login no REDS não concluiu: ' + C.curto(e)))
                log('Login recusado: monitor pausado' if recusou else 'Login não concluído')
                return 'login'
            marca('login')
            senha = ''
            estado = {'page': page}
            atributos, atalho = {}, C.Atalho()

            relogin = {'n': 0}

            def abrir():
                """Abre a pesquisa; se a sessão do REDS caiu (outro login com o mesmo usuário, tempo do SISP), entra de novo."""
                try: C.abrir_formulario(estado['page'], c)
                except C.SessaoCaiu:
                    if relogin['n'] >= 2: raise
                    relogin['n'] += 1; diag['relogin'] = relogin['n']
                    log('  (a sessão do REDS caiu; entrando de novo)')
                    estado['page'] = entrar(robo, estado['page'], c, usuario, os.environ.get('REDS_SENHA') or '', tentativas=2)
                    C.abrir_formulario(estado['page'], c)

            def ler(regras, nome, **kw):
                pg, lista, info, _ = C.ler_tudo(robo, estado['page'], c, jan, regras, nome, abrir=abrir, diag=diag, **kw)
                estado['page'] = pg
                return lista

            # 1) Polícia Civil na área (o PDF dela não vem)
            pc = set()
            try: pc = {b['reds'] for b in ler([(uni, [c['campo_area']])], 'orgao-PC', orgao=c['orgaos'].get('PC', '1'))}
            except C.Parar: raise
            except Exception as e: diag['fases']['orgao-PC'] = C.curto(e); log('  (pesquisa da Polícia Civil falhou: %s)' % C.curto(e))
            marca('pc')

            # 2) uma pesquisa por natureza de indicador; o PDF de cada registro novo sai na hora, da própria lista
            cand, pdfs = {}, {}

            def precisa_pdf(r, cod):
                """Todo candidato da janela (fora da PC) tem o PDF lido de novo: um homicídio tentado que vira consumado não
                muda o código da natureza, só o PDF mostra (e sai a atualização). Só a lesão corporal já vista sem morte fica."""
                if r in pc or r in pdfs: return False
                if cod == 'B01129' and r in verificados and r not in enviados: return False
                return True

            def pegar_pdf(pg, x):
                r = x['reds']
                dados = None
                if atalho.molde:
                    try: dados = atalho.baixar_ctx(pg.context, r, atributos)
                    except Exception: dados = None
                    if dados and C.ler_pdf_reds(dados, r).get('_confere'): pdfs[r] = dados; diag['pdf']['direto'] += 1; return
                for tentativa in (1, 2):                            # PDF de outro registro: clica de novo uma vez
                    try: dados, dg = C.capturar_pdf(pg, r, espera)
                    except Exception as e: dados, dg = None, {'erro': 'captura: ' + C.curto(e)}
                    C.voltar_lista(robo, pg)
                    if not dados or not C.pdf_de_outro(dados, r): break
                    dados, dg = None, {'erro': 'o REDS mostrou o PDF de outro registro'}; pg.wait_for_timeout(1500)
                if dados:
                    pdfs[r] = dados; diag['pdf']['clique'] += 1
                    if not atalho.molde and atalho.aprender(r, dg, atributos):
                        diag['pdf']['atalho'] = atalho.molde['tipo'] + ' ' + atalho.molde['metodo'] + ' ' + C.caminho(dg.get('url', ''))
                else:
                    pdfs[r] = None; diag['pdf']['falhas'] += 1
                    if len(diag['pdf']['exemplos']) < 3: diag['pdf']['exemplos'].append({k: v for k, v in dg.items() if k not in ('url', 'post')})

            for cod in C.COD_IND:
                def ao_ler(pg, novos, cod=cod):
                    for x in novos:
                        if jan.dentro(x) and precisa_pdf(x['reds'], cod): pegar_pdf(pg, x)
                try: lista = ler([(uni, [c['campo_area']]), (cod, [c['campo_natureza']])], 'nat-' + cod, ao_ler=ao_ler, atributos=atributos)
                except C.Parar: raise
                except Exception as e:
                    diag['fases']['nat-' + cod] = C.curto(e); log('  (pesquisa de natureza falhou: %s)' % C.curto(e)); continue
                for b in lista:
                    if not jan.dentro(b): continue
                    x = cand.setdefault(b['reds'], dict(b, codBusca=[]))
                    if cod not in x['codBusca']: x['codBusca'].append(cod)
            marca('naturezas')

            # 3) indicadores de cada registro (pelo PDF quando veio) e os alertas
            alertas, verif, falhas_envio = 0, [], 0
            for r, b in sorted(cand.items(), key=lambda kv: C.data_hora(kv[1].get('criacao')) or dt.datetime.min):
                mun, bairro = C.municipio_bairro(b['endereco'])
                it = dict(b, municipio=mun, bairro=bairro, grupoTipo=C.grupo_tipo(b['tipo']), naArea=True, orgao='PC' if r in pc else '', doBpm=False, pdf='')
                dados = pdfs.get(r)
                if dados:
                    info = C.ler_pdf_reds(dados, r); info.pop('_achou', None); info.pop('_confere', None)
                    for k, v in info.items():
                        if v not in (None, '', [], {}) or k == 'vitimas': it[k] = v
                    it['pdf'] = 'ok'
                C.regrupar(it, True); C.avaliar_indicadores(it); C.secundarias(it)
                ind = it['ind']
                tags = list(ind.get('tags') or []) + [t for t in ind.get('duvida') or [] if t not in (ind.get('tags') or [])]
                if not tags:
                    if set(it['codBusca']) <= {'B01129'} and (dados or r in pc): verif.append(r)    # lesão corporal sem morte
                    continue
                ja = enviados.get(r)
                if ja and set(tags) <= set(ja.get('ind') or []) and not (dados and ja.get('pdf') == 'nao'): continue
                motivo = '' if dados else ('pc' if r in pc else (pdfs.get(r, 0) is None and 'o REDS não entregou o PDF' or 'PDF não pedido'))
                try:
                    res = app.chamar('monitor.alerta', item=C.publico(it), pdf=base64.b64encode(dados).decode() if dados else '', pdfMotivo=motivo)
                    if res.get('enviado'): alertas += 1
                except C.Parar: raise
                except Exception as e:
                    falhas_envio += 1; log('  (alerta não chegou ao app: %s)' % C.curto(e))
            if verif:
                try: app.chamar('monitor.verificados', reds=verif)
                except Exception as e: log('  (verificados: %s)' % C.curto(e))
            marca('fim')
            falhas_busca = len(diag['fases'])                       # pesquisa que falhou = rodada incompleta: o app chama de novo em 10 min
            diag.update(candidatos=len(cand), pc=len(pc), alertas=alertas, verificados=len(verif), falhasEnvio=falhas_envio, falhasBusca=falhas_busca)
            msg = '%d registro(s) com natureza de indicador; %d alerta(s) enviado(s)' % (len(cand), alertas) + (('; %d alerta(s) não chegaram ao app' % falhas_envio) if falhas_envio else '') \
                + (('; %d pesquisa(s) falharam (o app repete a rodada em 10 min)' % falhas_busca) if falhas_busca else '')
            app.chamar('monitor.fim', ok=not falhas_envio and not falhas_busca, msg=msg, diag=diag)
            log('Rodada pronta: %s — tempos: %s' % (msg, json.dumps(diag['tempos'])))
            return 'ok'
        except C.Parar as e:
            log('Parado pelo app: %s' % e); return 'parado'
        except Exception as e:
            log('ERRO: ' + C.curto(e))
            try: app.chamar('monitor.fim', ok=False, msg='O robô parou: ' + C.curto(e), diag=diag)
            except Exception: pass
            return 'erro'
        finally:
            try: nav.close()
            except Exception: pass


def main():
    RR.carregar_env()
    c = C.cfg()
    rodada = (os.environ.get('RODADA') or '').strip()
    app = C.App(os.environ.get('CONSULTA_URL'), os.environ.get('CONSULTA_CHAVE'), rodada)
    try:
        try: ini = app.chamar('monitor.inicio')
        except C.Parar as e:
            log('Monitor pausado no app: %s' % e); return
        r = rodar(app, c, ini)
        if r == 'erro': sys.exit(1)
    finally:
        app.encerrar()


if __name__ == '__main__':
    main()
