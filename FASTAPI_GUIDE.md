# Guia de implementação com FastAPI

Este guia mostra como iniciar a ChocoMarket em FastAPI e implementar uma rota
completa. O exemplo usa:

```http
POST /v1/users
```

O objetivo é aprender o fluxo inteiro:

```text
request
  -> schema
  -> router
  -> service
  -> persistência
  -> response
```

O contrato de referência continua sendo o [openapi.yaml](./openapi.yaml).

## 1. Pré-requisitos

Verifique se o Python está instalado:

```bash
python --version
```

Recomenda-se usar Python 3.11 ou superior.

## 2. Criar o projeto

Na raiz do projeto:

```bash
mkdir -p app/core app/users tests
touch app/__init__.py
touch app/core/__init__.py
touch app/users/__init__.py
touch tests/__init__.py
```

Crie e ative um ambiente virtual:

### macOS/Linux

```bash
python -m venv .venv
source .venv/bin/activate
```

### Windows PowerShell

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

Instale as dependências:

```bash
python -m pip install fastapi "uvicorn[standard]" "pydantic[email]" pytest httpx
```

Salve as dependências:

```bash
python -m pip freeze > requirements.txt
```

O arquivo `.gitignore` deve conter:

```gitignore
.venv/
__pycache__/
.pytest_cache/
*.pyc
.env
```

## 3. Estrutura mínima

Ao final deste exemplo:

```text
app/
  __init__.py
  main.py
  core/
    __init__.py
    errors.py
  users/
    __init__.py
    router.py
    schemas.py
    service.py
tests/
  __init__.py
  test_users.py
```

Neste primeiro exercício, a persistência será em memória. Isso permite focar
no HTTP, na validação e na separação de responsabilidades. Depois, o
`service.py` poderá ser conectado ao banco sem mudar o contrato da rota.

## 4. Criar os schemas Pydantic

Crie `app/users/schemas.py`:

```python
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class CreateCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    name: str
    email: EmailStr
    role: str
    status: str
    created_at: str
    updated_at: str
```

### Por que existem dois schemas?

`CreateCustomerRequest` representa dados recebidos do cliente. Ele contém a
senha porque a senha é necessária no cadastro.

`UserResponse` representa dados enviados pela API. Ele não contém a senha nem
`password_hash`.

Essa separação evita expor dados sensíveis por acidente.

`extra="forbid"` também rejeita campos que o cliente não pode controlar, como:

```json
{
  "role": "admin"
}
```

## 5. Criar o serviço

Crie `app/users/service.py`:

```python
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException, status

from app.users.schemas import CreateCustomerRequest, UserResponse


users_by_email: dict[str, UserResponse] = {}


def create_customer(payload: CreateCustomerRequest) -> UserResponse:
    normalized_email = str(payload.email).lower()

    if normalized_email in users_by_email:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    now = datetime.now(UTC).isoformat()
    user = UserResponse(
        id=uuid4(),
        name=payload.name.strip(),
        email=normalized_email,
        role="customer",
        status="active",
        created_at=now,
        updated_at=now,
    )

    users_by_email[normalized_email] = user
    return user
```

### Responsabilidades do serviço

O serviço concentra regras de negócio:

- normalizar o e-mail;
- verificar duplicidade;
- definir `role=customer`;
- definir `status=active`;
- gerar o identificador;
- criar timestamps;
- persistir o usuário.

O cliente não escolhe `role` nem `status`.

> Neste exemplo, a senha não é armazenada. Antes de usar essa rota em
> produção, substitua a persistência em memória por banco de dados e use um
> algoritmo adequado de hash de senha. Nunca armazene a senha em texto puro.

## 6. Criar o router

Crie `app/users/router.py`:

```python
from fastapi import APIRouter, status

from app.users.schemas import CreateCustomerRequest, UserResponse
from app.users.service import create_customer as create_customer_service


router = APIRouter(prefix="/v1/users", tags=["Users"])


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_customer(payload: CreateCustomerRequest) -> UserResponse:
    return create_customer_service(payload)
```

### O que acontece nessa rota?

1. FastAPI recebe o JSON.
2. Pydantic valida o corpo.
3. Campos desconhecidos são rejeitados.
4. O router chama o serviço.
5. O serviço aplica as regras de negócio.
6. FastAPI serializa o resultado usando `UserResponse`.
7. A resposta recebe status `201 Created`.

O router coordena HTTP. Ele não deve conter toda a regra de negócio.

## 7. Criar a aplicação

Crie `app/main.py`:

```python
from fastapi import FastAPI

from app.users.router import router as users_router


app = FastAPI(
    title="ChocoMarket API",
    version="1.0.0",
    description="API da loja online ChocoMarket.",
)

app.include_router(users_router)


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
```

## 8. Executar a API

Com o ambiente virtual ativado:

```bash
uvicorn app.main:app --reload
```

URLs úteis:

```text
API:       http://127.0.0.1:8000
Swagger:   http://127.0.0.1:8000/docs
ReDoc:     http://127.0.0.1:8000/redoc
OpenAPI:   http://127.0.0.1:8000/openapi.json
```

Teste com `curl`:

```bash
curl -i -X POST http://127.0.0.1:8000/v1/users \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Ana Souza",
    "email": "ana@example.com",
    "password": "UmaSenhaSeguraCom12Caracteres"
  }'
```

Resposta esperada:

```http
HTTP/1.1 201 Created
```

O JSON deve conter `id`, `name`, `email`, `role`, `status`, `created_at` e
`updated_at`, mas não deve conter `password` nem `password_hash`.

## 9. Criar testes

Crie `tests/test_users.py`:

```python
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.users.service import users_by_email


client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_users() -> None:
    users_by_email.clear()


def test_create_customer_returns_201_without_password() -> None:
    response = client.post(
        "/v1/users",
        json={
            "name": "Ana Souza",
            "email": "ana@example.com",
            "password": "UmaSenhaSeguraCom12Caracteres",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["role"] == "customer"
    assert body["status"] == "active"
    assert "password" not in body
    assert "password_hash" not in body


def test_create_customer_rejects_admin_role() -> None:
    response = client.post(
        "/v1/users",
        json={
            "name": "Ana Souza",
            "email": "ana@example.com",
            "password": "UmaSenhaSeguraCom12Caracteres",
            "role": "admin",
        },
    )

    assert response.status_code == 422


def test_create_customer_rejects_duplicate_email() -> None:
    payload = {
        "name": "Ana Souza",
        "email": "ana@example.com",
        "password": "UmaSenhaSeguraCom12Caracteres",
    }

    first_response = client.post("/v1/users", json=payload)
    second_response = client.post("/v1/users", json=payload)

    assert first_response.status_code == 201
    assert second_response.status_code == 409
```

Execute:

```bash
pytest
```

## 10. Comparar a implementação com o contrato

Para cada endpoint, confira:

| Pergunta | Verificação |
|---|---|
| A rota existe? | URL e método HTTP |
| O request é validado? | Campos obrigatórios, tipos e limites |
| O status está correto? | `201`, `400`, `409`, `422`, etc. |
| A resposta está correta? | Campos e tipos do schema |
| Dados sensíveis estão protegidos? | Nunca retornar senha ou hash |
| A autorização está correta? | Cliente versus administrador |
| O erro segue o padrão? | `application/problem+json` |

O OpenAPI gerado pelo FastAPI pode ser consultado em:

```text
http://127.0.0.1:8000/openapi.json
```

Ele é útil para verificar o que a implementação está documentando. Ainda
assim, o contrato manual em [openapi.yaml](./openapi.yaml) continua sendo a
referência de design até que você decida adotar geração code-first.

## 11. Como repetir o processo para outro endpoint

Para implementar `products`, por exemplo:

### 1. Comece pelo contrato

Leia no OpenAPI:

```http
POST /v1/products
```

Identifique:

- request schema;
- response schema;
- status codes;
- autenticação;
- permissões;
- regras de negócio.

### 2. Crie os schemas

Separe:

```text
CreateProductRequest
UpdateProductRequest
ProductResponse
```

### 3. Crie o serviço

Coloque nele:

- validação de regras de negócio;
- cálculo de valores derivados;
- persistência;
- tratamento de conflitos.

### 4. Crie o router

O router deve:

- declarar método e caminho;
- declarar `response_model`;
- declarar status code;
- receber dependências;
- chamar o serviço.

### 5. Crie os testes

Cubra:

- caso de sucesso;
- request inválido;
- recurso inexistente;
- conflito;
- permissão insuficiente;
- formato da resposta.

### 6. Só depois conecte ao próximo recurso

Implemente uma fatia completa antes de criar muitos endpoints novos:

```text
schema -> router -> service -> banco -> teste
```

## 12. Próxima evolução do exemplo

Este exemplo é propositalmente pequeno. A ordem recomendada de evolução é:

1. adicionar hash de senha;
2. substituir o dicionário por banco de dados;
3. adicionar migrations com Alembic;
4. criar autenticação Bearer;
5. separar autenticação e autorização;
6. implementar `GET`, `PATCH` e `DELETE` de users;
7. implementar products;
8. implementar orders com transação;
9. adicionar idempotência persistida;
10. testar concorrência e desempenho.

Não pule diretamente para concorrência ou benchmark. Primeiro faça o fluxo
correto, teste-o e só então meça e otimize.
