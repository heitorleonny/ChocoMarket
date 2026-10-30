# ChocoMarket API

Contrato inicial da API REST da ChocoMarket, uma loja online de chocolates.

O projeto foi desenvolvido com abordagem **design-first**: primeiro foram
definidos os recursos, as regras de negócio e o contrato HTTP; a implementação
em NestJS será feita posteriormente.

## Contrato OpenAPI

O contrato está em:

```text
openapi.yaml
```

Ele pode ser aberto no [Swagger Editor](https://editor.swagger.io/) ou
validado por ferramentas compatíveis com OpenAPI 3.1.

## Recursos

### Users

Representa clientes e administradores.

- cadastro público cria somente clientes;
- administradores são criados inicialmente por seed;
- clientes podem consultar e atualizar o próprio perfil;
- administradores podem consultar e gerenciar usuários;
- a exclusão é lógica e altera `status` para `inactive`;
- usuários inativos não conseguem fazer login;
- senhas nunca aparecem nas respostas da API.

Principais endpoints:

```http
POST  /v1/users
GET   /v1/users
GET   /v1/users/{userId}
PATCH /v1/users/{userId}
PATCH /v1/users/{userId}/password
DELETE /v1/users/{userId}
```

### Products

Representa os chocolates vendidos pela loja.

- clientes visualizam produtos ativos;
- somente administradores criam e alteram produtos;
- preços são representados por `price_cents`;
- `1590` representa R$ 15,90;
- estoque é controlado por `stock_quantity`;
- estoque zero gera o status `out_of_stock`;
- a exclusão é lógica e altera o status para `inactive`.

Principais endpoints:

```http
GET    /v1/products
GET    /v1/products/{productId}
POST   /v1/products
PATCH  /v1/products/{productId}
DELETE /v1/products/{productId}
```

### Orders e order_items

Representam compras realizadas pelos clientes.

- o cliente envia somente `product_id` e `quantity`;
- preço, subtotal e total são calculados pelo backend;
- `unit_price_cents` preserva o preço no momento da compra;
- o pedido é criado com status `confirmed`;
- estoque é validado e reduzido na mesma transação;
- falta de estoque cancela a operação inteira;
- clientes consultam somente os próprios pedidos;
- administradores podem consultar todos os pedidos;
- o cancelamento devolve os itens ao estoque;
- a criação exige `Idempotency-Key`.

Principais endpoints:

```http
GET  /v1/orders
POST /v1/orders
GET  /v1/orders/{orderId}
POST /v1/orders/{orderId}/cancel
```

## Autenticação

As rotas protegidas usam Bearer Token:

```http
Authorization: Bearer <token>
```

Respostas de autenticação e autorização:

| Situação | Status |
|---|---:|
| Token ausente ou inválido | `401 Unauthorized` |
| Usuário sem permissão | `403 Forbidden` |
| Recurso inexistente ou indisponível para a consulta | `404 Not Found` |

## Paginação e filtros

As listagens usam paginação por cursor:

```http
GET /v1/products?limit=20&cursor=...
```

Uma resposta paginada possui este formato:

```json
{
  "data": [],
  "meta": {
    "limit": 20,
    "next_cursor": null,
    "has_next": false
  }
}
```

Os filtros disponíveis estão detalhados no contrato OpenAPI. Entre eles:

- usuários: nome, e-mail, papel e status;
- produtos: nome, faixa de preço e status;
- pedidos: status, período de criação e usuário, com restrições para clientes.

## Formato de erros

Os erros usam `application/problem+json`:

```json
{
  "type": "https://api.chocomarket.com/problems/validation-error",
  "title": "Validation error",
  "status": 422,
  "detail": "One or more fields are invalid.",
  "code": "VALIDATION_ERROR",
  "request_id": "req_01JZ2Y6Q3C"
}
```

## Idempotência

As operações de criação usam `Idempotency-Key` para permitir retries seguros.

Cadastro de usuário:

```http
POST /v1/users
Idempotency-Key: cadastro-ana-20261030
```

Criação de pedido:

```http
POST /v1/orders
Idempotency-Key: pedido-ana-20261030-01
```

Repetir a mesma chave com o mesmo conteúdo deve retornar o resultado original.
Reutilizar a chave com conteúdo diferente deve retornar `409 Conflict`.

## Validação do contrato

A sintaxe YAML foi validada localmente com sucesso. Para uma validação
semântica específica de OpenAPI, pode ser usado o Redocly CLI:

```bash
npx @redocly/cli lint openapi.yaml
```

Também é possível importar o arquivo no Swagger Editor para visualizar a
documentação e identificar referências ou estruturas inválidas.

## Próximos passos

1. Revisar o contrato OpenAPI completo.
2. Implementar os módulos no NestJS.
3. Configurar autenticação e autorização.
4. Implementar persistência e transações de estoque.
5. Criar o seed inicial do administrador.
6. Adicionar testes unitários e de integração.
7. Comparar o comportamento real da API com o contrato OpenAPI.

