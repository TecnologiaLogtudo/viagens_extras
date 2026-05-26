# Central de Viagens Extras MVP

## Rodar local

1. `python -m venv .venv`
2. `.venv\\Scripts\\Activate.ps1`
3. `pip install -r requirements.txt`
4. `uvicorn app.main:app --reload`

Acesse `http://127.0.0.1:8000/login`.

## Credenciais seed

- Parceiro: `parceiro@logtudo.local` / `parceiro123`
- Supervisor: `supervisor@logtudo.local` / `supervisor123`
- Gerente: `gerente@logtudo.local` / `gerente123`
- Financeiro: `financeiro@logtudo.local` / `financeiro123`

## Observações MVP

- Autenticação baseada em sessão com cookie HTTP-only.
- Portais segregados por rota: `/partner/*` e `/empresa/*`.
- Notificações por e-mail são persistidas em tabela `notification`.
- Comprovantes PDF são salvos em `app/data/documents/`.
