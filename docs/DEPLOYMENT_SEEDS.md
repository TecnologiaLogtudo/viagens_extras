# Integração de Seeds com CI/CD e Deployment

## 🚀 Fluxo de Deployment

### 1. Desenvolvimento Local

```bash
# Clonar repositório e instalar dependências
git clone <repo>
cd Viagens_Extras
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# Importar motoristas
python migrate_motoristas.py

# Verificar status
python manage_seeds.py show
```

### 2. Testes

```bash
# Executar testes (que irão chamar init_db_with_seeds)
pytest tests/

# Validar persistência específica
python test_seed_persistence.py
```

### 3. Deployment em Produção

```bash
# A aplicação inicia automaticamente com seeds garantidos
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

A inicialização garante:
- ✅ Banco de dados criado
- ✅ Schema atualizado
- ✅ Motoristas importados (idempotente)

## 🔄 Cenários de Migração

### Scenario A: Alteração de Schema

Se você alterar o schema do banco (adicionar coluna, etc):

```python
# app/db.py - init_db()
# Adicione a verificação de coluna existente
if "new_column" not in col_names:
    conn.execute(text("ALTER TABLE driver ADD COLUMN new_column TEXT"))
```

Ao executar `init_db_with_seeds()`:
1. ✅ Schema é atualizado
2. ✅ Seeds verificam se já foram aplicados
3. ✅ Dados existentes são preservados
4. ✅ Novo seed aplicado conforme necessário

### Scenario B: Novo Seed (ex: adicionar mais motoristas)

Criar novo seed em `app/seeds/seed_xxxx.py` com versão incrementada e registrar em `__init__.py`.

```bash
# Verificar seeds aplicados
python manage_seeds.py show

# Executar novo seed automaticamente
python migrate_motoristas.py
```

### Scenario C: Resetar Todos os Dados

Se precisar reimportar tudo do zero:

```bash
# Resetar seeds
python manage_seeds.py reset-all

# Reimportar
python migrate_motoristas.py

# Confirmar
python manage_seeds.py show
```

## 🐳 Docker/Container

### Dockerfile

```dockerfile
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Executar seeds e iniciar aplicação
CMD ["sh", "-c", "python migrate_motoristas.py && \
     python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
```

### Docker Compose

```yaml
version: '3.8'
services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./app/data:/app/app/data
    environment:
      - DATABASE_URL=sqlite:///./app/data/mvp.db
```

## 📊 Monitoramento

### Health Check com Seeds

```bash
# Ver se seeds foram aplicados
curl http://localhost:8000/health

# Verificar aplicação
python -c "
from app.db import engine
from app.seeds.seed_tracker import get_applied_seeds

with engine.connect() as conn:
    seeds = get_applied_seeds(conn)
    print(f'Seeds aplicadas: {len(seeds)}')
"
```

## 🔐 Segurança

1. **Arquivo Excel**: Manter `cadastro_veiculos_tratado.xlsx` no repositório
2. **Backups**: Fazer backup de `app/data/mvp.db` antes de grandes alterações
3. **Versioning**: Sempre incrementar versão de seeds ao modificar

## ⚠️ Troubleshooting

### Seeds não aplicados

```bash
# Verificar histórico
python manage_seeds.py show

# Se vazio, reimportar
python manage_seeds.py reset-all
python migrate_motoristas.py
```

### Dados desapareceram

1. Restaure de backup se disponível
2. Execute `python manage_seeds.py reset-all`
3. Execute `python migrate_motoristas.py`

### Performance

- Seeds são executados apenas uma vez (idempotentes)
- Nenhum overhead após primeira execução
- `init_db_with_seeds()` é seguro chamar múltiplas vezes

## 📝 Checklist de Deployment

- [ ] Seeds foram aplicados no ambiente de produção
- [ ] `python manage_seeds.py show` mostra seeds esperados
- [ ] `python test_seed_persistence.py` passa
- [ ] Backup do banco foi feito
- [ ] Arquivo Excel está no repositório
- [ ] Documentação de seeds está atualizada
