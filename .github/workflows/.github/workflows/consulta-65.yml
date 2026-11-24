name: Consulta 65º BPM
# Chamado pelo app "Consulta de Registros — 65º BPM" (Apps Script). A entrada é só o número do pedido: usuário e senha do
# REDS vêm cifrados do app e só o robô abre (secret CONSULTA65_PRIVADA). Várias consultas podem rodar ao mesmo tempo.
# Depois da consulta o robô fica conectado (sessão) enquanto a página do militar estiver aberta, para os PDFs sob demanda
# e novas consultas dele; o app encerra a sessão (Sair, página fechada, sem uso ou 40 min).
on:
  workflow_dispatch:
    inputs:
      pedido:
        description: 'Número do pedido feito no app (o resto vem cifrado do app)'
        required: true
run-name: 'Consulta 65 BPM · ${{ inputs.pedido }}'
permissions:
  contents: read
jobs:
  consultar:
    runs-on: ubuntu-24.04
    timeout-minutes: 50
    env:
      TZ: America/Sao_Paulo
      CONSULTA_URL: ${{ secrets.CONSULTA65_URL }}
      CONSULTA_CHAVE: ${{ secrets.CONSULTA65_CHAVE }}
      PEDIDO: ${{ inputs.pedido }}
    steps:
      - name: Avisar o app que o robô ligou
        run: |
          python3 - <<'PY' || true
          import json, os, urllib.request
          d = json.dumps({'acao': 'robo.progresso', 'chave': os.environ['CONSULTA_CHAVE'].strip(), 'pedido': os.environ['PEDIDO'],
                          'params': {'etapa': 'INICIOU', 'msg': 'Robô ligado no GitHub; preparando o navegador', 'pct': 8}}).encode()
          urllib.request.urlopen(urllib.request.Request(os.environ['CONSULTA_URL'].strip(), d, {'Content-Type': 'text/plain;charset=utf-8'}), timeout=30).read()
          PY
      - uses: actions/checkout@v6
      - uses: actions/setup-python@v6
        id: py
        with:
          python-version: '3.12'
      - name: Ambiente Python guardado (instala só na primeira vez)
        id: venv
        uses: actions/cache@v5
        with:
          path: ~/venv-consulta
          key: venv-consulta-${{ runner.os }}-${{ steps.py.outputs.python-version }}-${{ hashFiles('requirements_consulta.txt', 'requirements.txt') }}
      - name: Instalar
        if: steps.venv.outputs.cache-hit != 'true'
        run: python -m venv ~/venv-consulta && ~/venv-consulta/bin/pip install -q -r requirements_consulta.txt
      - name: Navegador (o Google Chrome que já vem no GitHub; sem ele, baixa o Chromium)
        run: |
          if [ -x /usr/bin/google-chrome ]; then echo "CHROME_PATH=/usr/bin/google-chrome" >> "$GITHUB_ENV"; else ~/venv-consulta/bin/python -m playwright install --with-deps chromium; fi
      - name: Consultar no REDS
        env:
          CONSULTA_PRIVADA: ${{ secrets.CONSULTA65_PRIVADA }}
        run: ~/venv-consulta/bin/python consulta65.py
      - name: Avisar o app se o robô parou no meio
        if: failure() || cancelled()
        run: |
          python3 - <<'PY' || true
          import json, os, urllib.request
          for acao, params in (('robo.fim', {'ok': False, 'msg': 'O robô parou no GitHub antes de terminar. Tente de novo; se repetir, avise o administrador.'}),
                               ('robo.sessaoFim', {'motivo': 'o robô parou no GitHub'})):
              d = json.dumps({'acao': acao, 'chave': os.environ['CONSULTA_CHAVE'].strip(), 'pedido': os.environ['PEDIDO'], 'exec': os.environ.get('GITHUB_RUN_ID', ''), 'params': params}).encode()
              try: urllib.request.urlopen(urllib.request.Request(os.environ['CONSULTA_URL'].strip(), d, {'Content-Type': 'text/plain;charset=utf-8'}), timeout=30).read()
              except Exception: pass
          PY
