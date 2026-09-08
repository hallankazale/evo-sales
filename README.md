# EVO Sales

EVO Sales é um funcionário virtual focado em prospecção comercial. A meta do MVP é simples: encontrar, qualificar e organizar oportunidades até ajudar a conquistar o primeiro cliente pagante.

## MVP atual

- Dashboard responsivo para acompanhar métricas e atividade do EVO-01.
- Cadastro manual de leads para validar o fluxo antes de automatizar buscas externas.
- Score inicial de qualificação.
- Geração automática de uma abordagem comercial.
- Persistência local em SQLite.
- API FastAPI desacoplada da interface.
- Testes básicos de saúde e validação.

## Arquitetura

```text
app/
  main.py       # API e rotas HTTP
  database.py   # persistência SQLite
  schemas.py    # contratos e validação
  services.py   # regras de negócio
static/
  index.html    # dashboard
  styles.css    # design responsivo
  app.js        # integração UI/API
tests/
  test_app.py   # smoke tests da API
```

A primeira versão evita frameworks front-end pesados para consumir pouca RAM e CPU. A lógica comercial fica separada da UI para permitir trocar o painel, banco ou mecanismo de IA sem reescrever o sistema inteiro.

## Rodar no Windows

No PowerShell:

```powershell
git clone https://github.com/hallankazale/evo-sales.git
cd evo-sales
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Abra no navegador:

```text
http://127.0.0.1:8000
```

## Testes

```powershell
pytest -q
```

## Próximas etapas

1. Motor de pesquisa de empresas com fontes públicas permitidas.
2. Deduplicação e enriquecimento de leads.
3. Pipeline de status e follow-up.
4. Conector de IA configurável sem expor chaves no navegador.
5. Histórico/auditoria de decisões do agente.
6. Automação assistida de contato, mantendo aprovação humana onde necessário.

## Segurança

- Entradas são validadas pelo Pydantic.
- Consultas SQLite usam parâmetros em vez de concatenação SQL.
- Dados retornados pelo backend são inseridos na interface com `textContent`, evitando HTML arbitrário vindo de leads.
- Chaves de API futuras deverão ficar apenas no backend via variáveis de ambiente e nunca dentro de `static/`.
