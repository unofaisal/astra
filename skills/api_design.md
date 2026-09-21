# For designing REST and GraphQL APIs — structure, endpoints, and conventions.

# API Design

A guide for designing clean, consistent, and usable APIs.

## REST API Principles

### Resource-Oriented Design
- Model resources (users, orders, products) as nouns.
- Use HTTP methods to express actions:
  - `GET` — Retrieve
  - `POST` — Create
  - `PUT` — Update (full)
  - `PATCH` — Partial update
  - `DELETE` — Remove

### URL Structure
```
GET    /users          # List users
GET    /users/{id}     # Get one user
POST   /users          # Create user
PUT    /users/{id}     # Update user
DELETE /users/{id}     # Delete user
```

### Status Codes
- `200 OK` — Success
- `201 Created` — Resource created
- `204 No Content` — Success, no body
- `400 Bad Request` — Invalid input
- `401 Unauthorized` — Not authenticated
- `403 Forbidden` — Not allowed
- `404 Not Found` — Resource doesn't exist
- `500 Internal Server Error` — Server issue

### Consistency
- Use plural nouns in URLs: `/users`, not `/user`.
- Use kebab-case: `/order-items`, not `/orderItems`.
- Keep filtering, sorting, and pagination consistent across endpoints.

### Error Response Format
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Email is required",
    "details": [...]
  }
}
```

## GraphQL Principles
- Define a schema first.
- Queries are client-defined — the client asks for exactly what it needs.
- Use mutations for writes.
- Avoid exposing internals — only expose what's needed.

## Authentication
- Use API keys for server-to-server.
- Use OAuth 2.0 / OpenID Connect for user-facing apps.
- Use Bearer tokens in the Authorization header.

## Versioning
- Use URL path versioning: `/v1/users`, `/v2/users`.
- Or use headers: `Accept: application/vnd.api.v1+json`.
- Deprecate old versions with clear timelines.

## Documentation
- Use OpenAPI (Swagger) for REST.
- Use GraphQL schema introspection for GraphQL.
- Include examples and error cases.
