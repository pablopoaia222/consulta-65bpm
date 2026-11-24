name: Monitor 65º BPM (alertas MV, CVPe, CVPa)
# Chamado pelo app "Consulta de Registros — 65º BPM" (gatilho de 10 em 10 min no Apps Script, que decide a hora certa de
# cada rodada). Entra no REDS com o login do dono (os MESMOS secrets do robô do pelotão: REDS_USUARIO e REDS_SENHA), procura
# MV, CVPe e CVPa na área e manda os registros novos ao app, que envia os e-mails. O log só mostra contagens.
on:
  workflow_dispatch:
    inputs:
      rodada:
        description: 'Número da rodada (vem do app)'
        required: true
run-name: 'Monitor 65 BPM · ${{ inputs.rodada }}'
permissions:
  contents: read
concurrency:
  group: monitor-65              # nunca duas rodadas ao mesmo tempo (a segunda espera)
  cancel-in-progress: false
jobs:
  monitorar:
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    env:
      TZ: America/Sao_Paulo
      CONSULTA_URL: ${{ secrets.CONSULTA65_URL }}
      CONSULTA_CHAVE: ${{ secrets.CONSULTA65_CHAVE }}
      RODADA: ${{ inputs.rodada }}
    steps:
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
      - name: Procurar MV, CVPe e CVPa no REDS
        env:
          REDS_USUARIO: ${{ secrets.REDS_USUARIO }}
          REDS_SENHA: ${{ secrets.REDS_SENHA }}
        run: ~/venv-consulta/bin/python monitor65.py
      - name: Avisar o app se a rodada parou no meio
        if: failure() || cancelled()
        run: |
          python3 - <<'PY' || true
          import json, os, urllib.request
          d = json.dumps({'acao': 'robo.monitor.fim', 'chave': os.environ['CONSULTA_CHAVE'].strip(), 'pedido': os.environ['RODADA'], 'exec': os.environ.get('GITHUB_RUN_ID', ''),
                          'params': {'ok': False, 'msg': 'A rodada parou no GitHub antes de terminar (veja Actions → Monitor 65º BPM).'}}).encode()
          urllib.request.urlopen(urllib.request.Request(os.environ['CONSULTA_URL'].strip(), d, {'Content-Type': 'text/plain;charset=utf-8'}), timeout=30).read()
          PY
