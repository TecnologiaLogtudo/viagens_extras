# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - 2026-06-15

### Changed
- **Lista Suspensa para Tipo de Viagem:**
  - O campo de texto livre para Tipo de Viagem no formulário do Parceiro (tanto no portal do parceiro quanto no dashboard) foi substituído por uma lista suspensa (select) com as opções: "Viagem extra NILO", "Carro dedicado", "Carro extra rota D2D" e "Cotação de preço".
- **Simplificação do Fluxo de Viagens Extras:**
  - Remoção completa da etapa de validação via código OTP pelo Parceiro.
  - Remoção completa da etapa de Despacho (dispatch) e Conclusão (complete) manual do Supervisor.
  - Triagem pelo Supervisor agora é a última etapa ativa do fluxo operacional. Ao confirmar uma viagem na triagem, ela é movida diretamente para o status final `COMPLETED`.
  - Edição de solicitações confirmadas (triadas) agora é permitida diretamente no status `COMPLETED`.
- **Disponibilização do Comprovante (PDF):**
  - O comprovante PDF da viagem (voucher) agora é gerado automaticamente e imediatamente após a confirmação/triagem do Supervisor.
  - O comprovante PDF agora exibe o método de aprovação como "Confirmação do Supervisor", tendo a assinatura preenchida com o nome do Supervisor responsável e a data em que ocorreu.
  - O botão de download do "Comprovante" foi disponibilizado em todos os perfis (Parceiro, Supervisor, Gerente, Financeiro) assim que a viagem é confirmada.
  - Adicionada a coluna de "Comprovante" na tabela do módulo **Financeiro** (`/empresa/financeiro`) do Gerente e Financeiro para acesso direto ao PDF da viagem.

### Removed
- Formulário e botões de envio/validação de código OTP no portal do Parceiro.
- Formulários e botões de "Despachar" e "Finalizar" no portal do Supervisor e nas rotas da API.
- Requisito de registro na tabela `Acceptance` (gerado na assinatura via OTP) para qualificar uma viagem no módulo financeiro.

## [1.1.0] - 2026-06-15

### Added
- **Banco de Dados PostgreSQL via Docker:**
  - Criação do arquivo `docker-compose.yml` para rodar o PostgreSQL 15-alpine em container.
  - Adicionada a dependência `psycopg2-binary` para conexão nativa ao PostgreSQL.
  - Adicionado suporte a `DATABASE_URL` no arquivo `.env` para conexão com o banco de dados PostgreSQL.
- **Processo de Carga Manual de Dados (Sementes):**
  - Criação do script utilitário `seed_all.py` para executar de forma independente a criação física das tabelas e a importação manual das sementes (motoristas, bases e usuários padrão).

### Changed
- **Configuração do Banco de Dados:**
  - Ajuste na inicialização do FastAPI no `app/main.py` para apenas garantir a estrutura física do banco (`init_db()`), removendo a carga automática de seeds na inicialização do servidor.
  - Ajuste em `app/db.py` para que a engine use PostgreSQL se `DATABASE_URL` estiver presente no ambiente, e SQLite (`mvp.db`) como fallback se ausente.
  - Isoladas as consultas `PRAGMA` específicas de migração SQLite para executar apenas quando o dialeto do banco for SQLite.
  - Modificado o arquivo `app/seeds/seed_tracker.py` para ser agnóstico quanto à sintaxe SQL (SERIAL, ON CONFLICT) entre SQLite e PostgreSQL.

