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
- Notificações por e-mail são persistidas em tabela `notification` e enviadas via SMTP.
- Comprovantes PDF são salvos em `app/data/documents/`.
- Convenção ORM: este projeto usa SQLModel com `Relationship` tipado como `list["Model"]` (evitar `Mapped[...]` enquanto não houver migração completa para estilo declarativo SQLAlchemy 2.0).

## Configuração de e-mail SMTP

Defina as variáveis de ambiente antes de iniciar a aplicação:

```env
SMTP_HOST=smtp.seuprovedor.com
SMTP_PORT=587
SMTP_USERNAME=seu-usuario
SMTP_PASSWORD=sua-senha-ou-app-password
SMTP_FROM_EMAIL=naoresponda@suaempresa.com
SMTP_USE_TLS=true
```

### Troubleshooting

- OTP não chegou: verifique spam/lixo eletrônico.
- Erro de autenticação SMTP: valide usuário/senha e se o provedor exige senha de app.
- Erro de conexão: confira host/porta e liberação de firewall.
- TLS: se seu provedor não usa TLS na conexão SMTP, ajuste `SMTP_USE_TLS=false`.
